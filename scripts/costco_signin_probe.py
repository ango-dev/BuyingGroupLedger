"""
RECON tool: map Costco's sign-in flow from a genuinely logged-out session.

One-off developer tool used to BUILD `scrapers/costco_signin.py`. It does NOT run in production.

WHY COSTCO IS NOT LIKE THE OTHERS. Costco's DATA path never opens a browser: `costco_api` exchanges a
stored refresh token for a short-lived `id_token` over `curl_cffi` and calls GraphQL. That path is
more reliable than any website scrape and it STAYS the primary — nothing here changes it. A browser
sign-in exists for exactly two jobs, both occasional:

  1. **Receipts.** `receipts/capture.py` renders costco.com order pages in a CDP browser. Nothing
     keeps that session warm, precisely because the data path opens no browser — so it lapses, and
     the design notes watched it lapse. Today the fix is a human running `scripts/create_profile`.
  2. **The refresh token.** `scripts/costco_token --grab` reads a plaintext `refresh_token` off the
     B2C token endpoint's RESPONSE. It is opportunistic: it only fires if MSAL happens to refresh
     while we watch. **A deliberate sign-in forces that exchange**, so a working login would mint a
     token as a side effect and remove the last manual step Costco has (the DevTools copy).

**COSTCO HAS NO 2FA IN THE US**, so there is no authenticator challenge
to answer and `totp_secret` is expected to be blank. That is a deferral, not an omission: Costco may
add 2-step verification later, and when it does this probe is what identifies the screen. Everything
about the code challenge is therefore DEFERRED — see the design notes.

Costco runs **Azure AD B2C** on `signin.costco.com`, a different identity platform from Amazon's and
Best Buy's, so no selector from either transfers. That is what this probe is for.

    python -m scripts.costco_signin_probe --label profile-alpha            # INSPECT: submits nothing
    python -m scripts.costco_signin_probe --label profile-alpha --login    # runs the real sign-in

Output goes to `.costco_signin_capture/` (gitignored — it holds account PII). The password and
username are scrubbed from every dumped file before it is written.

NOTE: this spends a small Browser-Use CDP-browser fee and signs into the live Costco account.
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("costco_signin_probe")

#: An authenticated page: logged out, Costco bounces it to signin.costco.com.
ACCOUNT_URL = "https://www.costco.com/myaccount"
ORDERS_URL = "https://www.costco.com/myaccount/#/app/orderstatus"

#: Same markers `receipts/sources.py` already uses to refuse storing a sign-in page as a receipt.
SIGNIN_MARKERS = ("signin.costco.com", "/logon", "/login", "b2clogin.com")

#: The B2C token endpoint. Its RESPONSE carries a plaintext refresh_token — the mechanism
#: `scripts/costco_token --grab` already relies on, and the reason a deliberate sign-in is
#: interesting beyond just warming the receipt session.
TOKEN_ENDPOINT_MARKERS = ("/oauth2/v2.0/token", "/b2c_1a", "signin.costco.com")


class Scrubber:
    """Replace every secret with a placeholder before anything reaches disk."""

    def __init__(self):
        self._pairs: list[tuple[str, str]] = []

    def add(self, secret: str, label: str) -> None:
        if secret and len(str(secret)) >= 4:
            self._pairs.append((str(secret), f"<{label} scrubbed>"))

    def __call__(self, text: str) -> str:
        for secret, placeholder in self._pairs:
            text = text.replace(secret, placeholder)
        return text


#: Generic: every field, every checkbox's CURRENT state, every button, and the page's own error copy.
#: Deliberately not Costco-specific — the whole point is that we do not yet know what is there.
_INVENTORY_JS = r"""
() => {
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const labelFor = el => {
    try {
      if (el.id) {
        const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (l) return (l.innerText || '').trim().slice(0, 120);
      }
      const p = el.closest('label');
      return p ? (p.innerText || '').trim().slice(0, 120) : '';
    } catch (e) { return ''; }
  };
  const out = {url: location.href, title: document.title, inputs: [], buttons: [], forms: [],
               errors: [], text: ''};
  try {
    for (const el of document.querySelectorAll('input, select')) {
      out.inputs.push({
        tag: el.tagName.toLowerCase(), type: el.type || null, id: el.id || null,
        name: el.name || null, placeholder: el.placeholder || null,
        checked: el.type === 'checkbox' || el.type === 'radio' ? !!el.checked : null,
        visible: vis(el), label: labelFor(el),
      });
    }
    for (const el of document.querySelectorAll('button, input[type=submit], a[role=button]')) {
      out.buttons.push({id: el.id || null, name: el.name || null, type: el.type || null,
                        text: (el.innerText || el.value || '').trim().slice(0, 60), visible: vis(el)});
    }
    for (const f of document.querySelectorAll('form')) {
      out.forms.push({name: f.name || null, id: f.id || null, action: f.action || null, method: f.method});
    }
    for (const el of document.querySelectorAll(
        '[role=alert], .error, [class*="error"], [class*="Error"], #errormessage')) {
      const t = (el.innerText || '').trim();
      if (t) out.errors.push(t.slice(0, 300));
    }
    out.text = ((document.body && document.body.innerText) || '').replace(/\s+/g, ' ').slice(0, 1500);
    out.markers = {
      // Azure B2C's stock ids, plus the likeliest custom spellings. Which (if any) exist is the
      // question this probe answers.
      b2c_email: !!document.querySelector('#signInName, #email, input[name=signInName]'),
      any_password: !!document.querySelector('input[type=password]'),
      // The user asked specifically about this one: Costco's sign-in page has a "Keep me signed in".
      keep_signed_in: !!document.querySelector(
          'input[type=checkbox][id*="remember" i], input[type=checkbox][name*="remember" i],' +
          'input[type=checkbox][id*="keep" i], input[type=checkbox][name*="keep" i], #rememberMe'),
      // No 2FA in the US today; present here so the day it appears, the probe SAYS so.
      any_otp_field: !!document.querySelector(
          'input[autocomplete="one-time-code"], input[name*="otp" i], input[id*="otp" i],' +
          'input[id*="verification" i], input[name*="code" i]'),
      captcha: !!document.querySelector('img[src*=captcha], iframe[src*=recaptcha], .g-recaptcha'),
    };
  } catch (e) { out.error = String(e); }
  return out;
}
"""


def _dump(page, out_dir: Path, tag: str, scrub: Scrubber) -> dict:
    """Save a screen's inventory + HTML + screenshot. Never raises."""
    info: dict = {}
    try:
        info = page.evaluate(_INVENTORY_JS)
    except Exception:
        log.debug("inventory failed for %s", tag, exc_info=True)
        info = {"url": getattr(page, "url", ""), "inventory_failed": True}
    for name, payload in ((f"{tag}.json", lambda: json.dumps(info, indent=2, default=str)),
                          (f"{tag}.html", page.content)):
        try:
            (out_dir / name).write_text(scrub(payload()), encoding="utf-8")
        except Exception:
            log.debug("write failed for %s", name, exc_info=True)
    try:
        page.screenshot(path=str(out_dir / f"{tag}.png"), full_page=True)
    except Exception:
        log.debug("screenshot failed for %s", tag, exc_info=True)

    markers = info.get("markers") or {}
    log.info("[%s] %s", tag, info.get("url", "?"))
    log.info("[%s] title=%r markers=%s", tag, (info.get("title") or "")[:70],
             {k: v for k, v in markers.items() if v})
    if info.get("errors"):
        log.warning("[%s] the page says: %s", tag, info["errors"][:3])
    return info


def _looks_logged_out(page) -> bool:
    url = (page.url or "").lower()
    return any(marker in url for marker in SIGNIN_MARKERS)


def _watch_token_endpoint(page) -> list:
    """Record B2C token-endpoint responses.

    Not decoration: `scripts/costco_token --grab` gets its refresh token from exactly this response,
    and its known weakness is that it only fires when MSAL happens to refresh. If a deliberate
    sign-in produces one of these, the grab becomes DETERMINISTIC rather than opportunistic — which
    is the second, larger reason to give Costco a browser login at all.
    """
    seen: list = []

    def record(response):
        try:
            url = response.url or ""
            if any(m in url.lower() for m in TOKEN_ENDPOINT_MARKERS) and "token" in url.lower():
                seen.append({"url": url[:160], "status": response.status})
        except Exception:
            pass

    try:
        page.on("response", record)
    except Exception:
        log.debug("page has no response event", exc_info=True)
    return seen


def _find_first(page, selectors, what: str):
    """The first selector matching a VISIBLE element, or '' — reporting which, so the production
    module is written from what was observed rather than from what was expected."""
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() and loc.is_visible():
                log.info("found %s via %s", what, selector)
                return selector
        except Exception:
            continue
    log.warning("could not find %s (tried %s)", what, ", ".join(selectors))
    return ""


EMAIL_SELECTORS = ("#signInName", "input[name='signInName']", "#email", "input[type=email]",
                   "input[name='email']", "input[name='username']")
PASSWORD_SELECTORS = ("#password", "input[type=password]", "input[name='password']")
SUBMIT_SELECTORS = ("#next", "button[type=submit]", "input[type=submit]", "#continue",
                    "button#signInButton", "button")
KEEP_SIGNED_IN_SELECTORS = (
    "#rememberMe",
    "input[type=checkbox][id*='remember' i]",
    "input[type=checkbox][name*='remember' i]",
    "input[type=checkbox][id*='keep' i]",
    "input[type=checkbox][name*='keep' i]",
)


def run(label: str, out_dir: Path, do_login: bool) -> int:
    # Importing config.settings is what puts BROWSER_USE_API_KEY into os.environ for the SDK.
    import config.settings  # noqa: F401
    from config.profiles import load_profiles
    from scrapers.cdp import CdpBrowser

    profile = next((p for p in load_profiles() if p.label == label), None)
    if profile is None:
        sys.exit(f"No profile '{label}' in config.json.")
    if not profile.profile_id:
        sys.exit(f"Profile '{label}' has no profile_id — run scripts.create_profile first.")

    auth = profile.auth.get("costco")
    if do_login and (auth is None or not auth.username or not auth.password):
        sys.exit("--login needs credentials: add an auth block for 'costco' to profile "
                 f"'{label}' in config.json (method/username/password).")

    scrub = Scrubber()
    if auth is not None:
        scrub.add(auth.password, "password")
        scrub.add(auth.username, "username")

    out_dir.mkdir(parents=True, exist_ok=True)
    findings: dict = {"probed_at": datetime.now(timezone.utc).isoformat(), "profile": label,
                      "mode": "login" if do_login else "inspect", "screens": []}
    token_responses: list = []

    try:
        with CdpBrowser(profile) as page:
            token_responses = _watch_token_endpoint(page)

            log.info("Loading %s", ACCOUNT_URL)
            page.goto(ACCOUNT_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            landing = _dump(page, out_dir, "01_landing", scrub)
            findings["screens"].append({"tag": "01_landing", "url": landing.get("url"),
                                        "markers": landing.get("markers")})

            if not _looks_logged_out(page):
                log.warning("This session is NOT logged out — the sign-in flow cannot be probed "
                            "while it is warm. Nothing further to do.")
                findings["result"] = "session_warm_nothing_to_probe"
                return 0

            log.info("Session is logged out (%s) — this is the state we want to map.", page.url)
            if not do_login:
                log.info("INSPECT mode: the sign-in page is dumped and nothing is submitted. "
                         "Re-run with --login to walk the flow.")
                findings["result"] = "inspect_only"
                return 0

            # --- credentials ----------------------------------------------------------------------
            email_sel = _find_first(page, EMAIL_SELECTORS, "the email field")
            if not email_sel:
                log.error("No email field. Read 01_landing.json before writing any selectors.")
                findings["result"] = "no_email_field"
                return 1
            page.fill(email_sel, auth.username)
            findings["email_selector"] = email_sel

            # Costco may put the password on the same screen or behind a Next click. Fill it if it
            # is here; otherwise submit and look again.
            pwd_sel = _find_first(page, PASSWORD_SELECTORS, "the password field")
            if pwd_sel:
                page.fill(pwd_sel, auth.password)
                findings["password_selector"] = pwd_sel

            # --- "Keep me signed in" --------------------------------------------------------------
            # The user called this out explicitly. It is ticked with check(), never click(), so a box
            # that is ALREADY on is never toggled off — the mistake that silently mints a
            # session-scoped cookie and undoes the whole point of signing in.
            keep = {"selector": None, "was_checked": None, "ticked": False}
            for selector in KEEP_SIGNED_IN_SELECTORS:
                try:
                    box = page.locator(selector).first
                    if box.count() == 0:
                        continue
                    keep["selector"] = selector
                    keep["was_checked"] = box.is_checked()
                    if not keep["was_checked"]:
                        box.check(timeout=5000)
                    keep["ticked"] = True
                    log.info("'Keep me signed in': %s was_checked=%s -> ticked",
                             selector, keep["was_checked"])
                    break
                except Exception:
                    log.debug("keep-me-signed-in via %s failed", selector, exc_info=True)
            if not keep["ticked"]:
                log.warning("'Keep me signed in' NOT found — the session will be short-lived, which "
                            "is the whole thing this is meant to avoid. Check 01_landing.json for "
                            "its real selector.")
            findings["keep_signed_in"] = keep

            submit_sel = _find_first(page, SUBMIT_SELECTORS, "the submit button")
            findings["submit_selector"] = submit_sel
            if submit_sel:
                page.locator(submit_sel).first.click(timeout=10000)
            page.wait_for_timeout(8000)
            after = _dump(page, out_dir, "02_after_submit", scrub)
            findings["screens"].append({"tag": "02_after_submit", "url": after.get("url"),
                                        "markers": after.get("markers")})

            # Two-screen variant: password only appears after the email is submitted.
            if not pwd_sel:
                pwd_sel = _find_first(page, PASSWORD_SELECTORS, "the password field (2nd screen)")
                if not pwd_sel:
                    log.error("No password field on either screen. Read the dumps.")
                    findings["result"] = "no_password_field"
                    return 1
                page.fill(pwd_sel, auth.password)
                findings["password_selector"] = pwd_sel
                submit2 = _find_first(page, SUBMIT_SELECTORS, "the submit button (2nd screen)")
                if submit2:
                    page.locator(submit2).first.click(timeout=10000)
                page.wait_for_timeout(8000)
                after = _dump(page, out_dir, "03_after_password", scrub)
                findings["screens"].append({"tag": "03_after_password", "url": after.get("url"),
                                            "markers": after.get("markers")})

            # --- did anything challenge us? -------------------------------------------------------
            markers = after.get("markers") or {}
            if markers.get("any_otp_field"):
                # Costco has no US 2FA today. If this ever fires, it is the signal to un-defer the
                # 2FA work in the design notes -- and the dump names the screen.
                log.warning("AN OTP-LIKE FIELD APPEARED. Costco is believed to have no US 2FA, so "
                            "this is either new or a mis-detection — read the dump before building.")
                findings["challenge"] = "otp_like_field_present"
            if markers.get("captcha"):
                log.error("CAPTCHA — automation cannot clear this. Back off; do not retry in a loop.")
                findings["challenge"] = "captcha"

            # --- did it work? ---------------------------------------------------------------------
            page.goto(ORDERS_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(6000)
            final = _dump(page, out_dir, "04_final", scrub)
            findings["screens"].append({"tag": "04_final", "url": final.get("url"),
                                        "markers": final.get("markers")})
            findings["signed_in"] = not _looks_logged_out(page)
            findings["result"] = "signed_in" if findings["signed_in"] else "still_logged_out"
            log.info("RESULT: %s", findings["result"])
    except Exception:
        log.exception("Probe hit an error; writing out whatever was captured before it.")
        findings.setdefault("result", "error")

    findings["token_endpoint_responses"] = token_responses
    (out_dir / "findings.json").write_text(
        scrub(json.dumps(findings, indent=2, default=str)), encoding="utf-8")

    print(f"\nResult: {findings.get('result')}   ->  {out_dir / 'findings.json'}")
    print("\nSelectors observed:")
    for key in ("email_selector", "password_selector", "submit_selector", "keep_signed_in"):
        if findings.get(key):
            print(f"  {key:20} {findings[key]}")
    print(f"\nB2C token-endpoint responses seen: {len(token_responses)}")
    for t in token_responses[:6]:
        print(f"  {t['status']} {t['url']}")
    if token_responses:
        print("  -> a deliberate sign-in DOES drive the token exchange, so `costco_token --grab` "
              "could become deterministic rather than opportunistic.")
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", default="profile-alpha",
                        help="Profile that owns the Costco membership")
    parser.add_argument("--out", default=".costco_signin_capture", help="Output dir (gitignored)")
    parser.add_argument("--login", action="store_true",
                        help="Actually sign in (default: inspect the sign-in page, submit nothing)")
    args = parser.parse_args()
    raise SystemExit(run(args.label, Path(args.out), args.login))


if __name__ == "__main__":
    main()
