"""
RECON tool: map Amazon Business's sign-in flow from a genuinely logged-out session.

One-off developer tool used to BUILD `scrapers/amazon_signin.py` — the deterministic,
agent-free self-login that lets a lapsed Amazon Business session heal itself, the way
`bestbuy_api._deterministic_login` does for Best Buy. It does NOT run in production.

WHY IT EXISTS. Amazon's element ids are widely documented (#ap_email, #ap_password, #auth-mfa-otpcode
...), and every one of them is a GUESS until this account is watched doing it. The two unknowns that
actually decide the design can only be answered live:

  1. WHICH CHALLENGE does this account get after the password — `/ap/mfa` (an authenticator code,
     which we CAN generate locally and answer unattended) or `/ap/cvf/verify` (a code mailed or
     texted to a human, which we cannot)? The whole feature rests on the first.
  2. Is there a "don't ask for codes on this device" box, is it pre-ticked, and what is it called?
     That box is what turns 2FA from a per-run obstacle into a one-off. Best Buy's arrives ticked;
     assuming Amazon's does would be exactly the kind of guess this tool exists to remove.

Two modes:

    python -m scripts.amazon_business_signin_probe --label profile-alpha          # INSPECT: submits nothing
    python -m scripts.amazon_business_signin_probe --label profile-alpha --login  # runs the real sign-in

CONSUMER Amazon is probed with the same tool — one identity system serves both accounts, so only the
credentials differ (`--retailer amazon` reads `auth["amazon"]` instead):

    python -m scripts.amazon_business_signin_probe --label profile-bravo --retailer amazon

INSPECT loads the order-history page, confirms the session is logged out, and dumps the sign-in DOM.
It cannot reach the password or OTP screens — those exist only after a real submission — so --login
is what settles question 1 above.

--login uses the profile's own `auth[<retailer>]` credentials from config.json and answers an
authenticator challenge with `scrapers.totp`. It stops and dumps at anything it does not recognise
rather than clicking around: a wrong guess here costs an Amazon account lock, not a retry.

Output goes to `.amazon_business_signin_capture/` (gitignored — it holds account PII). Secrets are scrubbed
from every dumped file: the password, the TOTP seed, the generated code and the username are replaced
before anything is written, and cookie header values are redacted to names + lengths.

NOTE: this spends a small Browser-Use CDP-browser fee and signs into the live Amazon Business
account. Amazon will see a NEW DEVICE — which is precisely the event the trusted-device box exists to
make a one-off. Run it ONCE and read the output; repeated automated sign-ins are what get an Amazon
account locked.
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# CdpBrowser talks to Browser-Use, which reads BROWSER_USE_API_KEY from the environment.
load_dotenv()

log = logging.getLogger("amazon_business_signin_probe")

ORDER_HISTORY_URL = "https://www.amazon.com/your-orders/orders"
SIGNIN_MARKERS = ("/ap/signin", "/ap/mfa", "/ap/cvf", "/ap/challenge", "signin")

#: Requests whose failure would EXPLAIN a rejected sign-in rather than merely accompany it. Best Buy
#: taught this one the expensive way: every click and fill worked and sign-in still
#: failed, because the auth POST died at the network layer where no selector can see it.
AUTH_CRITICAL = ("/ap/signin", "/ap/mfa", "/ap/cvf", "/ap/uedata", "/errors/validateCaptcha")


def _redact_headers(headers: dict) -> dict:
    """Keep header names/shape but never write raw session cookies / auth to disk."""
    out = {}
    for name, value in (headers or {}).items():
        lname = name.lower()
        if lname in ("cookie", "authorization", "x-amz-access-token", "set-cookie", "anti-csrftoken-a2z"):
            if lname == "cookie" and value:
                names = sorted({c.split("=", 1)[0].strip() for c in value.split(";") if c.strip()})
                out[name] = f"<redacted {len(value)} chars; cookies: {', '.join(names)}>"
            else:
                out[name] = f"<redacted {len(value)} chars>"
        else:
            out[name] = value
    return out


class Scrubber:
    """Replace every secret with a placeholder before anything reaches disk.

    A recon dump is HTML plus page text plus a screenshot, and a sign-in page holds the one set of
    strings that must never be written down. Typing into an input does not update its `value`
    ATTRIBUTE, so `page.content()` normally omits the password — "normally" being the operative word,
    since Amazon has re-rendered these forms before. Scrubbing unconditionally costs nothing and
    removes the question.
    """

    def __init__(self):
        self._pairs: list[tuple[str, str]] = []

    def add(self, secret: str, label: str) -> None:
        if secret and len(str(secret)) >= 4:
            self._pairs.append((str(secret), f"<{label} scrubbed>"))

    def __call__(self, text: str) -> str:
        for secret, placeholder in self._pairs:
            text = text.replace(secret, placeholder)
        return text


#: Everything a sign-in screen can tell us, in one round trip: which fields exist, what the
#: checkboxes are ALREADY set to (the pre-ticked question), and Amazon's own error copy — which is
#: the only thing that distinguishes a wrong password from an anti-bot rejection.
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
        // The answer to "is the box pre-ticked?" — the single fact this probe exists for.
        checked: el.type === 'checkbox' || el.type === 'radio' ? !!el.checked : null,
        visible: vis(el), label: labelFor(el),
      });
    }
    for (const el of document.querySelectorAll('button, input[type=submit], a.a-button-text')) {
      out.buttons.push({id: el.id || null, name: el.name || null,
                        text: (el.innerText || el.value || '').trim().slice(0, 60), visible: vis(el)});
    }
    for (const f of document.querySelectorAll('form')) {
      out.forms.push({name: f.name || null, id: f.id || null, action: f.action || null, method: f.method});
    }
    for (const el of document.querySelectorAll(
        '#auth-error-message-box, #auth-warning-message-box, [role=alert], .a-alert-content')) {
      const t = (el.innerText || '').trim();
      if (t) out.errors.push(t.slice(0, 300));
    }
    out.text = ((document.body && document.body.innerText) || '').replace(/\s+/g, ' ').slice(0, 1500);
    // Named markers, so a screen is classified by evidence rather than by eyeballing the dump.
    out.markers = {
      email_field: !!document.querySelector('#ap_email, #ap_email_login, input[name=email]'),
      password_field: !!document.querySelector('#ap_password, input[type=password]'),
      otp_field: !!document.querySelector('#auth-mfa-otpcode, input[name=otpCode], input[name=code]'),
      remember_device: !!document.querySelector('#auth-mfa-remember-device'),
      keep_signed_in: !!document.querySelector('#auth-remember-me, input[name=rememberMe]'),
      captcha: !!document.querySelector('#auth-captcha-image, img[src*=captcha]'),
      account_switcher: !!document.querySelector('[name=switchAccount], .cvf-account-switcher, #ap-account-switcher-container'),
    };
  } catch (e) { out.error = String(e); }
  return out;
}
"""


def _dump(page, out_dir: Path, tag: str, scrub: Scrubber) -> dict:
    """Save a screen's inventory + HTML + screenshot. Never raises — recon must not lose what it has
    already paid for to one failing dump."""
    info: dict = {}
    try:
        info = page.evaluate(_INVENTORY_JS)
    except Exception:
        log.debug("inventory failed for %s", tag, exc_info=True)
        info = {"url": getattr(page, "url", ""), "inventory_failed": True}
    try:
        (out_dir / f"{tag}.json").write_text(
            scrub(json.dumps(info, indent=2, default=str)), encoding="utf-8")
    except Exception:
        log.debug("inventory write failed for %s", tag, exc_info=True)
    try:
        (out_dir / f"{tag}.html").write_text(scrub(page.content()), encoding="utf-8")
    except Exception:
        log.debug("content() dump failed for %s", tag, exc_info=True)
    try:
        page.screenshot(path=str(out_dir / f"{tag}.png"), full_page=True)
    except Exception:
        log.debug("screenshot failed for %s", tag, exc_info=True)

    markers = info.get("markers") or {}
    log.info("[%s] %s", tag, info.get("url", "?"))
    log.info("[%s] title=%r markers=%s", tag, (info.get("title") or "")[:70],
             {k: v for k, v in markers.items() if v})
    if info.get("errors"):
        log.warning("[%s] the page says: %s", tag, info["errors"])
    return info


def _watch_failed_requests(page) -> list:
    failed: list = []

    def record(request):
        try:
            if len(failed) < 40:
                failed.append({"url": request.url[:160], "type": request.resource_type,
                               "error": request.failure or ""})
        except Exception:
            pass

    try:
        page.on("requestfailed", record)
    except Exception:
        log.debug("page has no requestfailed event", exc_info=True)
    return failed


def _looks_logged_out(page) -> bool:
    url = (page.url or "").lower()
    if any(m in url for m in SIGNIN_MARKERS):
        return True
    try:
        return page.locator("#ap_email, #ap_password, input[name='email']").count() > 0
    except Exception:
        return False


def _visible(page, selectors: tuple[str, ...]) -> str:
    """The first selector matching a VISIBLE element ('' if none do).

    Presence is not enough on an Amazon sign-in page: the remembered variant carries the account
    email in a HIDDEN `input[name=email]` (`#ap-claim`), so a count()-based check would report an
    email field that cannot be typed into and send the flow down the wrong branch.
    """
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() and loc.is_visible():
                return selector
        except Exception:
            continue
    return ""


def _click_first(page, selectors: tuple[str, ...], what: str) -> str:
    """Click the first selector that exists, returning which one worked ('' if none did)."""
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() == 0:
                continue
            loc.click(timeout=10000)
            log.info("clicked %s via %s", what, selector)
            return selector
        except Exception:
            log.debug("click %s via %s failed", what, selector, exc_info=True)
    log.warning("could not click %s (tried %s)", what, ", ".join(selectors))
    return ""


def _check_first(page, selectors: tuple[str, ...], what: str) -> dict:
    """Tick the first checkbox that exists, reporting what it was set to BEFORE we touched it.

    `check()` rather than `click()` throughout: a box Amazon already ticked must never be toggled OFF
    by the act of "ticking" it. The `was_checked` value is the finding the probe is here for.
    """
    for selector in selectors:
        try:
            box = page.locator(selector).first
            if box.count() == 0:
                continue
            was = box.is_checked()
            if not was:
                box.check(timeout=5000)
            log.info("%s: %s was_checked=%s -> now ticked", what, selector, was)
            return {"selector": selector, "was_checked": was, "ticked": True}
        except Exception:
            log.debug("%s via %s failed", what, selector, exc_info=True)
    log.warning("%s: no control found (tried %s)", what, ", ".join(selectors))
    return {"selector": None, "was_checked": None, "ticked": False}


# NB `input[name=email]` is deliberately absent: on the remembered variant that name belongs to the
# HIDDEN `#ap-claim`, and `#ap-credential-autofill-hint` is a visible text input that is NOT the email
# field — so a loose "the first text box" rule would type the username into the wrong element.
EMAIL_SELECTORS = ("#ap_email", "#ap_email_login", "input[type=email]")
CONTINUE_SELECTORS = ("#continue", "input#continue", "#continue-announce",
                      "input[type=submit][aria-labelledby*=continue]")
PASSWORD_SELECTORS = ("#ap_password", "input[type=password]")
SIGNIN_SUBMIT_SELECTORS = ("#signInSubmit", "input#signInSubmit", "#auth-signin-button")
KEEP_SIGNED_IN_SELECTORS = ("#auth-remember-me", "input[name='rememberMe']",
                            "input[type=checkbox][name*='remember' i]")
OTP_SELECTORS = ("#auth-mfa-otpcode", "input[name='otpCode']", "input[name='code']")
OTP_SUBMIT_SELECTORS = ("#auth-signin-button", "#signInSubmit", "input[type=submit]")
REMEMBER_DEVICE_SELECTORS = ("#auth-mfa-remember-device", "input[name='rememberDevice']",
                             "input[type=checkbox][name*='remember' i]")


class _Stop(Exception):
    """A deliberate early exit from the probe body, carrying the process exit code.

    A bare `return` inside the `with CdpBrowser(...)` block would skip the summary write at the end
    of run(), throwing away the screen dumps and the failed-request list that this run just paid a
    cloud browser to collect -- and the early exits are exactly the interesting outcomes (a CVF
    challenge, a CAPTCHA, a changed form). Raising instead keeps one exit path through the write.
    """

    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


def run(label: str, out_dir: Path, do_login: bool, retailer: str = "amazon-business") -> int:
    # Importing config.settings is what puts BROWSER_USE_API_KEY into os.environ, where the SDK reads
    # it. The key normally lives in config.json, and load_dotenv() alone does not find it there — so
    # without this the probe dies at "No API key provided" with a perfectly valid configuration.
    import config.settings  # noqa: F401
    from config.profiles import load_profiles
    from scrapers.cdp import CdpBrowser

    profile = next((p for p in load_profiles() if p.label == label), None)
    if profile is None:
        sys.exit(f"No profile '{label}' in config.json.")
    if not profile.profile_id:
        sys.exit(f"Profile '{label}' has no profile_id — run scripts.create_profile first.")

    auth = profile.auth.get(retailer)
    if do_login and (auth is None or not auth.username or not auth.password):
        sys.exit(
            f"--login needs credentials: add an auth block for '{retailer}' to profile "
            f"'{label}' in config.json (method/username/password/totp_secret)."
        )

    scrub = Scrubber()
    if auth is not None:
        scrub.add(auth.password, "password")
        scrub.add(auth.totp_secret, "totp-seed")
        scrub.add(auth.username, "username")

    out_dir.mkdir(parents=True, exist_ok=True)
    findings: dict = {"probed_at": datetime.now(timezone.utc).isoformat(), "profile": label,
                      "retailer": retailer, "mode": "login" if do_login else "inspect",
                      "screens": []}
    failed_requests: list = []
    exit_code = 0

    try:
        with CdpBrowser(profile) as page:
            failed_requests = _watch_failed_requests(page)

            # --- landing: is the session actually logged out? ------------------------------------
            log.info("Loading %s", ORDER_HISTORY_URL)
            page.goto(ORDER_HISTORY_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
            landing = _dump(page, out_dir, "01_landing", scrub)
            findings["screens"].append({"tag": "01_landing", "url": landing.get("url"),
                                        "markers": landing.get("markers")})

            if not _looks_logged_out(page):
                log.warning("This session is NOT logged out — the sign-in flow cannot be probed while "
                            "it is warm, and forcing a logout would also clear the device-trust token. "
                            "Nothing further to do.")
                findings["result"] = "session_warm_nothing_to_probe"
                raise _Stop(0)

            log.info("Session is logged out (%s) — this is the state we want to map.", page.url)

            if not do_login:
                log.info("INSPECT mode: the email screen is dumped and nothing is submitted. The "
                         "password and OTP screens exist only after a real submission — re-run with "
                         "--login to reach them.")
                findings["result"] = "inspect_only"
                raise _Stop(0)

            # --- screen 0: the "Switch accounts" page, IF that is what Amazon served ---------------
            #
            # Observed live on the SECOND probe run, where the first had shown a password
            # form — so Amazon alternates and the flow cannot assume an order. This calls the
            # PRODUCTION handler rather than a copy of it, so what the probe proves is the code that
            # will actually run. It refuses to guess between two accounts, which matters more here
            # than anywhere else in the flow: signing into the wrong one scrapes a different
            # account's orders and looks like a completely successful run.
            from scrapers import amazon_signin as signin

            if signin.on_account_switcher(page):
                action = signin.handle_account_switcher(page, auth)
                findings["account_switcher"] = action or "REFUSED (ambiguous or unclickable)"
                switched = _dump(page, out_dir, "01b_after_account_switcher", scrub)
                findings["screens"].append({"tag": "01b_after_account_switcher",
                                            "url": switched.get("url"),
                                            "markers": switched.get("markers")})
                if not action:
                    log.error("Refused to pick an account: none of the offered tiles unambiguously "
                              "matched the configured username. Read 01_landing.json — signing into "
                              "the WRONG account would poison the ledger silently.")
                    findings["result"] = "account_switcher_ambiguous"
                    raise _Stop(1)

            # --- screen 1: email, IF this variant asks for one -------------------------------------
            #
            # Amazon serves two shapes and the observed one (2026-08-25, profile-alpha) is the
            # REMEMBERED variant: the account email sits in a HIDDEN `#ap-claim` input and the
            # password field is right there on the landing page, so there is no email step at all.
            # The fresh variant asks for the email first and reveals the password after Continue.
            # Deciding by "is a password field already visible?" rather than by URL is what makes
            # this work on both without guessing which one Amazon feels like serving.
            if _visible(page, PASSWORD_SELECTORS):
                findings["variant"] = "remembered_email_prefilled"
                log.info("Remembered-user variant: the password field is already on the page and the "
                         "email is prefilled in a hidden input — no email step.")
            else:
                findings["variant"] = "email_first"
                filled_email = ""
                for selector in EMAIL_SELECTORS:
                    try:
                        if page.locator(selector).count() == 0:
                            continue
                        page.fill(selector, auth.username)
                        filled_email = selector
                        log.info("filled the email field via %s", selector)
                        break
                    except Exception:
                        log.debug("fill email via %s failed", selector, exc_info=True)
                findings["email_selector"] = filled_email
                if not filled_email:
                    log.error("Neither a visible password field nor a fillable email field. Amazon's "
                              "sign-in markup has changed; read 01_landing.json before writing any "
                              "selectors.")
                    findings["result"] = "no_email_or_password_field"
                    raise _Stop(1)

                # "Keep me signed in" appears on the email screen in this variant and on the password
                # screen in the other, so it is attempted at BOTH and reported separately.
                findings["keep_signed_in_email_screen"] = _check_first(
                    page, KEEP_SIGNED_IN_SELECTORS, "keep-me-signed-in (email screen)")
                findings["continue_selector"] = _click_first(page, CONTINUE_SELECTORS, "Continue")
                page.wait_for_timeout(4000)
                screen2 = _dump(page, out_dir, "02_after_email", scrub)
                findings["screens"].append({"tag": "02_after_email", "url": screen2.get("url"),
                                            "markers": screen2.get("markers")})

            # --- screen 2: password -> Sign in ----------------------------------------------------
            filled_password = ""
            for selector in PASSWORD_SELECTORS:
                try:
                    if not _visible(page, (selector,)):
                        continue
                    page.fill(selector, auth.password)
                    filled_password = selector
                    log.info("filled the password field via %s", selector)
                    break
                except Exception:
                    log.debug("fill password via %s failed", selector, exc_info=True)
            findings["password_selector"] = filled_password
            if not filled_password:
                log.error("No visible password field. Read the latest screen dump — an account "
                          "chooser or a challenge may sit in between.")
                findings["result"] = "no_password_field"
                raise _Stop(1)

            findings["keep_signed_in_password_screen"] = _check_first(
                page, KEEP_SIGNED_IN_SELECTORS, "keep-me-signed-in (password screen)")
            findings["signin_submit_selector"] = _click_first(
                page, SIGNIN_SUBMIT_SELECTORS, "Sign in")
            page.wait_for_timeout(6000)
            screen3 = _dump(page, out_dir, "03_after_password", scrub)
            findings["screens"].append({"tag": "03_after_password", "url": screen3.get("url"),
                                        "markers": screen3.get("markers")})

            # --- screen 3: THE DECISION GATE ------------------------------------------------------
            url3 = (screen3.get("url") or "").lower()
            markers3 = screen3.get("markers") or {}
            if markers3.get("captcha"):
                log.error("CAPTCHA. Automation cannot clear this and neither can the paid agent. Back "
                          "off — do NOT re-run this probe in a loop.")
                findings["challenge"] = "captcha"
                findings["result"] = "captcha"
                raise _Stop(1)
            if "/ap/cvf" in url3:
                log.error("Amazon served the CVF challenge (/ap/cvf) — a code sent to a phone or "
                          "inbox. THIS IS THE OUTCOME THAT KILLS THE FEATURE: nothing here can "
                          "receive that code. Enrol an AUTHENTICATOR APP on the account and re-probe; "
                          "if Amazon still serves CVF, unattended self-login is not achievable and the "
                          "build should stop at the classifier.")
                findings["challenge"] = "cvf_sms_or_email"
                findings["result"] = "cvf_challenge"
                raise _Stop(1)

            if markers3.get("otp_field") or "/ap/mfa" in url3:
                log.info("Amazon served the AUTHENTICATOR challenge — this is the answerable one.")
                findings["challenge"] = "otp_mfa"

                # The trusted-device box, ticked BEFORE the code is submitted. This is the finding
                # that decides whether 2FA is a one-off or a per-run tax.
                findings["remember_device"] = _check_first(
                    page, REMEMBER_DEVICE_SELECTORS, "don't-ask-for-codes-on-this-device")

                if not auth.totp_secret:
                    log.error("The OTP screen is up but no totp_secret is configured — add it to "
                              f"auth['{retailer}'].totp_secret and re-run.")
                    findings["result"] = "otp_but_no_secret"
                    raise _Stop(1)

                from scrapers.totp import TotpError, seconds_remaining, totp
                try:
                    # Never submit a code with seconds left on it: Amazon reports an expired code as
                    # "invalid", which reads exactly like a wrong seed and sends you debugging the
                    # wrong thing.
                    if seconds_remaining() < 3:
                        page.wait_for_timeout(3000)
                    code = totp(auth.totp_secret)
                except TotpError:
                    log.error("The configured totp_secret is not valid base32.", exc_info=True)
                    findings["result"] = "bad_totp_secret"
                    raise _Stop(1)
                scrub.add(code, "totp-code")

                filled_otp = ""
                for selector in OTP_SELECTORS:
                    try:
                        if page.locator(selector).count() == 0:
                            continue
                        page.fill(selector, code)
                        filled_otp = selector
                        log.info("filled the OTP field via %s", selector)
                        break
                    except Exception:
                        log.debug("fill otp via %s failed", selector, exc_info=True)
                findings["otp_selector"] = filled_otp
                findings["otp_submit_selector"] = _click_first(
                    page, OTP_SUBMIT_SELECTORS, "OTP submit")
                page.wait_for_timeout(6000)
                screen4 = _dump(page, out_dir, "04_after_otp", scrub)
                findings["screens"].append({"tag": "04_after_otp", "url": screen4.get("url"),
                                            "markers": screen4.get("markers")})
            else:
                findings["challenge"] = "none_or_unrecognised"
                log.info("No challenge recognised after the password — either the account signed "
                         "straight in, or Amazon showed something new. Read 03_after_password.json.")

            # --- did it actually work? ------------------------------------------------------------
            page.goto(ORDER_HISTORY_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
            final = _dump(page, out_dir, "05_final", scrub)
            findings["screens"].append({"tag": "05_final", "url": final.get("url"),
                                        "markers": final.get("markers")})
            findings["signed_in"] = not _looks_logged_out(page)
            findings["result"] = "signed_in" if findings["signed_in"] else "still_logged_out"
            log.info("RESULT: %s", findings["result"])
    except _Stop as stop:
        exit_code = stop.code
    except Exception:
        log.exception("Probe hit an error; writing out whatever was captured before it.")
        findings.setdefault("result", "error")
        exit_code = 1

    findings["failed_requests"] = failed_requests[:40]
    findings["auth_critical_failures"] = [
        f for f in failed_requests if any(h in f.get("url", "") for h in AUTH_CRITICAL)
    ]
    (out_dir / "findings.json").write_text(
        scrub(json.dumps(findings, indent=2, default=str)), encoding="utf-8")

    print(f"\nResult: {findings.get('result')}   ->  {out_dir / 'findings.json'}")
    if findings.get("auth_critical_failures"):
        print("\nAUTH-CRITICAL REQUESTS FAILED at the network layer — that is a transport/anti-bot "
              "problem, not a selector one:")
        for f in findings["auth_critical_failures"][:8]:
            print(f"  {f['error']}  {f['url']}")
    print("\nSelectors observed:")
    for key in ("email_selector", "continue_selector", "password_selector",
                "signin_submit_selector", "otp_selector", "otp_submit_selector"):
        if findings.get(key):
            print(f"  {key:26} {findings[key]}")
    for key in ("keep_signed_in_email_screen", "keep_signed_in_password_screen", "remember_device"):
        if findings.get(key):
            print(f"  {key:26} {findings[key]}")
    print("\nRead the per-screen *.json / *.html / *.png dumps next; they are what the real selectors "
          "get written from.")
    return exit_code


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--label", default="profile-alpha",
                        help="Profile that owns the Amazon Business login")
    parser.add_argument("--out", default=".amazon_business_signin_capture", help="Output dir (gitignored)")
    parser.add_argument("--login", action="store_true",
                        help="Actually sign in (default: inspect the sign-in page, submit nothing)")
    parser.add_argument("--retailer", default="amazon-business",
                        choices=["amazon-business", "amazon"],
                        help="Which auth block to use, and which account is being probed. Both run "
                             "on amazon.com and share one identity system, so the same probe covers "
                             "them; only the credentials differ.")
    args = parser.parse_args()
    raise SystemExit(run(args.label, Path(args.out), args.login, args.retailer))


if __name__ == "__main__":
    main()
