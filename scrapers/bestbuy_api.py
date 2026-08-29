"""Best Buy order data over its own private endpoints — the cheap, deterministic primary path.

No AI agent: a CDP browser (scrapers.cdp.CdpBrowser) holds the profile's logged-in cookie, and the two
data reads ride that session. Discovered live (see reference-bestbuy-order-api /
scripts/bestbuy_capture.py). Best Buy has no portable token like Costco — auth IS the web-session
cookie (Akamai-guarded) — so the reads happen INSIDE the page:

  1. DISCOVERY: load `purchasehistory/purchases`; the order list is server-rendered as Next.js flight
     data embedded in the HTML. `_order_ids_and_dates` reassembles it and pulls each order id + date.
  2. DETAILS: for each in-window / still-open order, `fetch('/profile/ss/api/v1/orders/<id>',
     {credentials:'include'})` run in-page returns the full ss-api order JSON (tracking#, per-item cost,
     card, shipment groups). scrapers/bestbuy_mapping.py turns those into ledger rows.

If the session has lapsed (Best Buy dies ~20 min), `_deterministic_login` logs back in with the
profile's `auth.bestbuy` password creds — the exact 3-screen flow proven live (prefilled email ->
Continue -> #password-radio -> password).

WHICH ERROR IS RAISED DECIDES WHETHER MONEY IS SPENT, so the two are kept strictly apart:
  - `ApiLoginError` -> scrapers/bestbuy.py alerts and SKIPS. The agent is never run for an auth
    failure, because it cannot fix one — it would just spend ~$0.02 rediscovering the logout.
  - `BestBuyApiError` -> falls back to the Browser-Use agent (like Costco). Reserved for a genuine
    page/shape change, which is the one thing the agent CAN adapt to.
The hard case is discovery coming back empty, which both causes produce identically; see
`_signin_affordances` for how they are told apart.

The flight reassembly / order-id extraction is pure and unit-tested (tests/test_bestbuy_api.py); the
mapping is pure and unit-tested (tests/test_bestbuy_mapping.py). Only the browser mechanics need a live
run to prove.
"""

import json
import logging
import re
from typing import NamedTuple

from scrapers.base import ApiLoginError
from scrapers.cdp import CdpBrowser
from scrapers.totp import TotpError, seconds_remaining, totp

log = logging.getLogger(__name__)

PURCHASE_HISTORY_URL = "https://www.bestbuy.com/purchasehistory/purchases"
ORDER_DETAIL_PATH = "/profile/ss/api/v1/orders/{}"
_SIGNIN_MARKERS = ("identity/signin", "/login", "signin/options")

# Things a page shows only when it wants you to sign in. Used ONLY as corroboration once discovery
# has already found zero orders -- deliberately NOT folded into _looks_logged_out, because a false
# positive THERE would send a perfectly good session into a doomed self-login and end in
# skip-with-alert, i.e. a silently missed run. Here the run is failing either way and the only open
# question is whether to spend money on an agent, so the safe direction is to assume logged out.
_SIGNIN_CTA_SELECTORS = (
    ".cia-signin",
    "#fld-e",
    *(f'a[href*="{marker}"]' for marker in _SIGNIN_MARKERS),
)


class BestBuyApiError(Exception):
    """The deterministic path could not run (login failed, page shape changed, network) — the caller
    should fall back to the agent."""



# --- pure flight parsing (unit-tested) ----------------------------------------------------------
def _scan_string_literal(s: str, i: int) -> tuple[str, int]:
    """Read a JS/JSON string literal beginning at s[i] == '"', returning (literal_incl_quotes, end)."""
    out = []
    j = i + 1
    while j < len(s):
        c = s[j]
        if c == "\\":
            out.append(s[j:j + 2])
            j += 2
            continue
        if c == '"':
            return '"' + "".join(out) + '"', j + 1
        out.append(c)
        j += 1
    raise ValueError("unterminated string literal")


def _match_json(s: str, start: int) -> str:
    """Return the JSON object/array substring starting at s[start] via bracket matching."""
    open_c = s[start]
    close_c = {"{": "}", "[": "]"}[open_c]
    depth = 0
    in_str = False
    esc = False
    for k in range(start, len(s)):
        c = s[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return s[start:k + 1]
    raise ValueError("unterminated json")


def _reassemble_flight(html: str) -> str:
    """Concatenate the Next.js flight chunks (`self.__next_f.push([1,"…"])`) embedded in the page into
    the raw flight payload string."""
    parts = []
    for m in re.finditer(r"self\.__next_f\.push\(\[1,", html):
        i = m.end()
        while i < len(html) and html[i] in " \t\r\n":
            i += 1
        if i < len(html) and html[i] == '"':
            lit, _ = _scan_string_literal(html, i)
            try:
                parts.append(json.loads(lit))
            except json.JSONDecodeError:
                continue
    return "".join(parts)


def _order_ids_and_dates(html: str) -> dict[str, str]:
    """Bare order id (BBY01-<digits>, group suffix stripped) -> order date (YYYY-MM-DD) for every
    order in the purchase-history flight data. Date is best-effort ('' if not found)."""
    flight = _reassemble_flight(html)
    key = '"purchaseHistoryOrdersExperience"'
    idx = flight.find(key)
    if idx < 0:
        # Fall back to any bare ids in the raw HTML (no dates) so discovery still works.
        return {oid: "" for oid in dict.fromkeys(re.findall(r"BBY01-\d+", html))}
    colon = flight.index(":", idx + len(key))
    v = colon + 1
    while v < len(flight) and flight[v] in " \t\r\n":
        v += 1
    try:
        obj = json.loads(_match_json(flight, v))
    except (ValueError, json.JSONDecodeError):
        return {oid: "" for oid in dict.fromkeys(re.findall(r"BBY01-\d+", html))}

    result: dict[str, str] = {}

    def walk(node):
        if isinstance(node, dict):
            raw_id = node.get("id")
            if isinstance(raw_id, str) and raw_id.startswith("BBY01-"):
                bare = raw_id.split("-group")[0]
                created = node.get("created")
                date = created[:10] if isinstance(created, str) and len(created) >= 10 else ""
                # Prefer a non-empty date if we see one across the order's entries.
                if bare not in result or (date and not result[bare]):
                    result[bare] = date
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(obj)
    return result


# --- browser mechanics (needs a live session) ---------------------------------------------------
def _looks_logged_out(page) -> bool:
    url = (page.url or "").lower()
    if any(m in url for m in _SIGNIN_MARKERS):
        return True
    try:
        return page.locator(".cia-signin, #fld-e").count() > 0
    except Exception:
        return False


def _discover_orders(page) -> dict:
    """Scroll the lazily-rendered purchase list, then parse order ids + dates out of the flight data.

    Extracted so it can be run a SECOND time after a late-detected logout is self-healed -- otherwise
    recovering the session would still return the empty result parsed before signing in.
    """
    for _ in range(5):
        try:
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        page.wait_for_timeout(1200)
    return _order_ids_and_dates(page.content())


def _signin_affordances(page) -> list[str]:
    """Which sign-in CTAs the page is showing, if any.

    This exists because a silently-expired session and a genuine markup change look IDENTICAL at the
    point discovery comes back empty, and they have opposite correct responses: a logout must skip
    for free (the agent cannot fix an auth failure), while a shape change is exactly what the paid
    agent is for. Live the logged-out purchase-history page tripped neither URL redirect
    nor the sign-in form selectors, so `not dates` fired, the agent ran, cost $0.02, and concluded
    "logged out" anyway -- rediscovering for money what this catches for free.
    """
    found = []
    for selector in _SIGNIN_CTA_SELECTORS:
        try:
            if page.locator(selector).count() > 0:
                found.append(selector)
        except Exception:  # noqa: BLE001 -- a selector engine hiccup must not mask the real failure
            continue
    return found


def _dismiss_survey(page) -> None:
    try:
        page.evaluate("() => { const s = document.getElementById('survey_window'); if (s) s.remove(); }")
    except Exception:
        pass


def _keep_signed_in(page) -> bool:
    """Tick "Keep me signed in" before submitting, if it is present and unticked.

    WHY THIS MATTERS MORE THAN IT LOOKS. Best Buy has been logged out on EVERY scheduled run for days
    — ~16 consecutive — and the accepted explanation was that its web sessions simply die in ~20-25
    minutes against a 3-hourly schedule. But the AGENT prompt has always said to leave this box
    checked (`scrapers/bestbuy.py`), while the deterministic login, which replaced it as the primary
    path, never touched it. That asymmetry is a candidate root cause rather than a certainty: if the
    box governs whether Best Buy mints a persistent token or a session-scoped cookie, then the
    deterministic path has been minting the short-lived kind every single run and re-signing-in
    forever, which is exactly the observed behaviour.

    Best-effort by design, like every other step in this flow: several spellings are tried, and a
    miss changes nothing (Best Buy has historically DEFAULTED it to checked, so this is insurance
    against a default flip as much as a fix). `check()` is used rather than `click()` so an
    already-ticked box is never toggled OFF.

    Not yet confirmed live — the next scheduled run either shows Best Buy arriving warm, or shows the
    box was never the reason. Either outcome is worth more than the current guess.
    """
    for selector in (
        "#cia-remember-me",
        "input[name='keepMeSignedIn']",
        "input[type=checkbox][id*='remember' i]",
        "input[type=checkbox][name*='remember' i]",
    ):
        try:
            box = page.locator(selector).first
            if box.count() == 0:
                continue
            if box.is_checked():
                log.debug("Best Buy sign-in: 'Keep me signed in' already checked (%s).", selector)
                return True
            box.check(timeout=4000)
            log.info("Best Buy sign-in: ticked 'Keep me signed in' via %s.", selector)
            return True
        except Exception:  # noqa: BLE001 — never let an optional nicety break the sign-in
            continue
    log.debug("Best Buy sign-in: no 'Keep me signed in' control found; continuing.")
    return False


def _click_continue(page) -> bool:
    """Click Continue on the email screen, re-dismissing the survey modal before each attempt.

    Dismissing the survey ONCE when the page loads is not enough. `#survey_window` renders lazily
    and intermittently, so it can appear between that call and this click; it overlays the button,
    Playwright's actionability check keeps waiting for a clear hit target, and the attempt times out.
    Re-dismissing immediately before each try is what makes this survive the modal.

    The last strategy dispatches the click on the element directly. That bypasses hit-testing
    altogether, so it still works if something we don't know about is covering the button — worth
    having as a fallback because the alternative is losing the whole run.
    """
    strategies = (
        ("css button.cia-form__controls__submit",
         lambda: page.locator("button.cia-form__controls__submit").first.click(timeout=8000)),
        ("role=button[name~=Continue]",
         lambda: page.get_by_role("button", name="Continue", exact=False).first.click(timeout=8000)),
        ("dispatched el.click()",
         lambda: page.eval_on_selector("button.cia-form__controls__submit", "el => el.click()")),
    )
    for label, attempt in strategies:
        _dismiss_survey(page)
        try:
            attempt()
        except Exception as exc:  # noqa: BLE001 — try the next strategy, report only if all fail
            log.debug("Best Buy sign-in: Continue via %s failed: %s", label, exc)
            continue
        log.info("Best Buy sign-in: clicked Continue via %s.", label)
        return True
    return False


#: Hosts whose failure explains an auth rejection. tmx.bestbuy.com is ThreatMetrix — Best Buy's
#: device-fingerprinting script. When it cannot load, the sign-in POST arrives without a valid
#: fingerprint and Best Buy rejects it, which surfaces as the page's own "Failed to fetch" rather
#: than anything our selectors can see.
_AUTH_CRITICAL_HOSTS = ("identity/authenticate", "gateway/graphql", "tmx.bestbuy.com")


def _watch_failed_requests(page) -> list:
    """Collect network-level request failures during sign-in.

    Diagnosed live: every click and fill worked, and sign-in still failed because
    `POST /identity/authenticate` died with ERR_HTTP2_PROTOCOL_ERROR while the ThreatMetrix script
    failed to tunnel at all. None of that is visible from the DOM — the page just says "Failed to
    fetch" — so without this the failure is indistinguishable from a selector problem and sends you
    hunting in the wrong place.
    """
    failed: list = []

    def record(request):
        try:
            if len(failed) < 20:
                failed.append({"url": request.url[:120],
                               "type": request.resource_type,
                               "error": request.failure or ""})
        except Exception:  # noqa: BLE001 — never let telemetry break a login
            pass

    try:
        page.on("requestfailed", record)
    except Exception:  # noqa: BLE001 — a page without event support still logs DOM diagnostics
        pass
    return failed


def _auth_critical(failed_requests: list | None) -> list:
    """The subset of failed requests that explain an auth rejection (see _AUTH_CRITICAL_HOSTS)."""
    return [f for f in (failed_requests or [])
            if any(h in f.get("url", "") for h in _AUTH_CRITICAL_HOSTS)]


class LoginOutcome(NamedTuple):
    """Result of a sign-in attempt.

    `transport_failed` separates the two failures that look identical from the DOM but have opposite
    fixes: a page/selector change (retrying elsewhere won't help) versus auth requests dying at the
    NETWORK layer, which the profile's proxy does intermittently — retrying the login off-proxy fixes
    that one. See reference-isp-proxy-breaks-post.
    """

    ok: bool
    transport_failed: bool = False
    # Verdict + action from _classify_signin_failure, propagated into ApiLoginError so the emailed
    # alert names the actual problem ("sign in manually to clear a verification code") instead of a
    # generic "login failed - check the logs".
    reason: str = ""


def _classify_signin_failure(info: dict, critical: list) -> tuple[str, str]:
    """(verdict, what to do about it) for a stalled sign-in.

    These failures look IDENTICAL from the outside — every one of them ends as "submitted the password
    but stayed logged out" — yet they have opposite fixes: a stale password is a one-line config edit,
    an identity challenge needs a human, and an anti-bot rejection needs backing off (retrying makes it
    worse). Learned 2026-08-23, when a simply-wrong password was misread for days as the anti-bot
    transport failure that produces the same line. Best Buy states the reason on the page; read it.
    """
    errors = " ".join(str(e) for e in (info.get("errors") or []))
    haystack = f"{errors} {info.get('title') or ''} {info.get('text') or ''}"
    url = str(info.get("url") or "").lower()

    if re.search(r"password.{0,25}incorrect|incorrect.{0,25}password|couldn'?t find an account", haystack, re.I):
        return ("BAD CREDENTIAL — Best Buy says the password is wrong",
                "Usually the stored password IS stale -> update the profile's auth.bestbuy.password in config.json. "
                "But do not trust this banner blindly: observed that the SAME unchanged "
                "password reached the identity-verification screen 25 minutes earlier and was then "
                "reported 'incorrect', i.e. Best Buy also says this while an account is flagged and "
                "pending a forced reset. So if that credential recently got FURTHER than this screen, "
                "treat the account as flagged and clear it by hand rather than editing the password. "
                "Either way stop retrying — repeated attempts risk a lockout.")
    if re.search(r"account.{0,30}(locked|disabled)|too many (failed )?attempts", haystack, re.I):
        return ("ACCOUNT LOCKED — too many attempts",
                "Stop all automated sign-ins and unlock the account with Best Buy before retrying.")
    if "verifyownership" in url or re.search(r"verify your identity|last four digits", haystack, re.I):
        return ("IDENTITY VERIFICATION — Best Buy is asking for a one-time code (SMS/email)",
                "Automation cannot clear this: confirmed that the screen offers only "
                "'Text message'/'Email address' -> Send code, with NO known-value field to fill. It has "
                "TWO causes and they need opposite responses, so check the preceding runs before acting: "
                "(a) legitimate 2FA on the account -> sign in manually once via scripts/create_profile; "
                "(b) an ESCALATION Best Buy imposes after too many failed password attempts (it then "
                "pushes a reset) -> if recent runs logged BAD CREDENTIAL, fix the stored password FIRST "
                "and stop retrying. Reaching this screen is NOT by itself proof the stored password is "
                "correct.")
    if re.search(r"we sent a code|enter the (6|six)[- ]digit|verification code", haystack, re.I):
        return ("ONE-TIME CODE REQUIRED (SMS/email)",
                "The automation cannot receive the code. Sign in manually, or switch auth.bestbuy to a "
                "method that needs no code.")
    if re.search(r"captcha|unusual activity|are you a human", haystack, re.I):
        return ("CAPTCHA / BOT CHALLENGE",
                "Back off; do not retry in a loop. The agent fallback cannot solve it either.")
    # "Failed to fetch" is Best Buy's OWN copy for its auth XHR dying, so it is direct evidence of a
    # transport failure whether or not we happened to capture the failed request. Observed live
    # 2026-08-29: the same profile produced ANTI-BOT/TRANSPORT on one run (auth-critical request
    # captured) and UNKNOWN — "the sign-in DOM may have changed" — on the next, with the SAME
    # 'Failed to fetch' on the page. The DOM had not changed; only our luck in catching the request
    # had. Reading the page's own words fixes that, and is the principle this whole function is
    # built on.
    if critical or re.search(r"failed to fetch", haystack, re.I):
        return ("ANTI-BOT / TRANSPORT — auth requests died at the network layer",
                "Not a page-shape problem, so the agent cannot fix it. Back off and retry later; see "
                "reference-isp-proxy-breaks-post. Note the off-proxy retry and proxy rotation are "
                "both already FALSIFIED for this failure — the rejection travels with "
                "the browser, not the egress IP, so do not spend on either.")
    return ("UNKNOWN — no error text on the page and no auth-critical request failures",
            "Inspect the saved page state below; the sign-in DOM may have changed.")


def _log_signin_diagnostics(page, what_failed: str, failed_requests: list | None = None) -> None:
    """Say WHY sign-in stalled, since the caller can only return False.

    A bare `return False` costs the whole run for that retailer and tells you nothing — you cannot
    tell a survey overlay from a disabled button from a CAPTCHA interstitial from a changed DOM, and
    those have completely different fixes. Best Buy sessions die in ~20 minutes, so this path runs on
    most scheduled runs and a silent failure is one you'd be guessing at for days.

    The single most useful thing here is Best Buy's OWN error copy (`errors`), which names the cause
    outright — without it a wrong password is indistinguishable from an anti-bot rejection.
    """
    info: dict = {}
    try:
        info = page.evaluate(
            r"""() => {
                const b = document.querySelector('button.cia-form__controls__submit');
                const text = (document.body && document.body.innerText) || '';
                return {
                    url: location.href,
                    title: document.title,
                    // Best Buy's own banner ("The password you've entered is incorrect.") — the one
                    // field that actually names the failure.
                    errors: Array.from(document.querySelectorAll(
                            '[role=alert], .c-alert, [class*="error"], [class*="Error"]'))
                        .map(e => (e.innerText || '').trim()).filter(Boolean).slice(0, 4),
                    button: b ? {
                        text: (b.innerText || '').trim().slice(0, 40),
                        disabled: !!b.disabled,
                        visible: !!(b.offsetWidth || b.offsetHeight),
                    } : null,
                    survey_present: !!document.getElementById('survey_window'),
                    password_radio: !!document.getElementById('password-radio'),
                    looks_like_challenge:
                        /captcha|unusual activity|verify it'?s you|are you a human/i.test(text),
                    text: text.replace(/\s+/g, ' ').slice(0, 400),
                };
            }"""
        )
    except Exception:  # noqa: BLE001 — diagnostics must never mask the original failure
        log.warning("Best Buy sign-in: %s (page state unreadable).", what_failed, exc_info=True)

    critical = [f for f in (failed_requests or [])
                if any(h in f.get("url", "") for h in _AUTH_CRITICAL_HOSTS)]

    verdict = action = ""
    if info:
        verdict, action = _classify_signin_failure(info, critical)
        # Lead with the verdict: this is the line a human reads first in a wall of scheduled-run logs.
        log.warning("Best Buy sign-in FAILED — %s. WHAT TO DO: %s", verdict, action)
        if info.get("errors"):
            log.warning("Best Buy sign-in: the page says: %s", info["errors"])
        log.warning("Best Buy sign-in: %s. Page state: %s", what_failed,
                    {k: v for k, v in info.items() if k != "text"})

    if not failed_requests:
        return verdict, action
    if critical:
        log.warning(
            "Best Buy sign-in: %d auth-critical request(s) FAILED at the network layer — this is a "
            "connectivity/anti-bot problem, NOT a page-shape one, so the agent fallback cannot fix "
            "it either: %s", len(critical), critical[:6],
        )
    else:
        # "none auth-critical" alone reads as reassurance — but a PILE of tunnel/connection failures
        # means the egress itself is struggling, which is context for the verdict rather than noise.
        tunnel = [f for f in failed_requests
                  if "TUNNEL" in str(f.get("error", "")).upper()
                  or "ERR_CONNECTION" in str(f.get("error", "")).upper()]
        log.warning("Best Buy sign-in: %d request(s) failed, none on an auth-critical host%s: %s",
                    len(failed_requests),
                    (f" — but {len(tunnel)} of them are tunnel/connection errors, so the egress "
                     f"itself looks degraded rather than the page" if len(tunnel) >= 3 else ""),
                    failed_requests[:6])
    return verdict, action


def _signin_reason(verdict: str, action: str) -> str:
    return f"{verdict}. WHAT TO DO: {action}" if verdict else ""


TWO_STEP_MARKER = "twostepverification"
#: Best Buy's 2-Step screen. `cia-trust-me` ("Don't ask for security codes on this device") arrives
#: ALREADY TICKED, so this only has to avoid un-ticking it — but it is asserted rather than assumed,
#: because leaving it off means a fresh code on every single run, and Best Buy sessions die in ~20
#: minutes. Ticking it is what turns 2FA from a per-run obstacle into a one-off.
TWO_STEP_CODE_INPUT = "#verificationCode"
TWO_STEP_TRUST_CHECKBOX = "#cia-trust-me"

#: How much life a code must have LEFT before it is worth submitting.
#:
#: 3 seconds was the original value and it was too small — it assumed submission is instant. It is
#: not: minting is followed by a visibility probe, a fill, and a click, and each is a round trip to a
#: CLOUD browser. On the main PC that is ~1s and 3s of headroom always sufficed; on the live Docker
#: host the same steps take longer, so a code minted with 4s left could expire in flight. Amazon then
#: says "The code you entered is not valid", which is indistinguishable from a wrong seed — and cost
#: a full diagnosis (2026-08-27) that cleared the seed (hash-identical to a working machine) and the
#: clock (+0.4s) before the margin was suspected.
#:
#: 10s is chosen to cover a slow round trip several times over. The cost is bounded and trivial: at
#: worst one wait of under 10s, on a sign-in that already takes ~20s, and only when the code happens
#: to be minted near the end of its window. Submitting a stale code costs the whole run and reads as
#: a credential problem.
_MIN_CODE_LIFE_SECONDS = 10


def _on_two_step(page) -> bool:
    try:
        if TWO_STEP_MARKER in (page.url or "").lower():
            return True
        return page.locator(TWO_STEP_CODE_INPUT).count() > 0
    except Exception:
        return False


def _answer_two_step(page, auth) -> bool:
    """Fill Best Buy's 2-Step Verification screen from the enrolled authenticator secret.

    Returns False (rather than raising) when no secret is configured, so the caller reports the usual
    "a human is needed" verdict instead of a crash.
    """
    secret = getattr(auth, "totp_secret", "")
    if not secret:
        log.warning("Best Buy 2-Step Verification is required but no totp_secret is configured for "
                    "this profile — add auth.bestbuy.totp_secret to config.json.")
        return False
    # Validate the seed NOW, before touching the page: a malformed secret must decline without
    # submitting anything. The code generated here is deliberately discarded -- see below.
    try:
        totp(secret)
    except TotpError:
        log.warning("Best Buy 2-Step Verification: the configured totp_secret is not valid base32.",
                    exc_info=True)
        return False

    try:
        page.wait_for_selector(TWO_STEP_CODE_INPUT, state="visible", timeout=20000)
        # Trust this device, so the next run does not need a code at all.
        try:
            box = page.locator(TWO_STEP_TRUST_CHECKBOX)
            if box.count() and not box.is_checked():
                box.check(timeout=5000)
        except Exception:
            log.warning("Best Buy 2-Step: could not confirm the 'don't ask again' box; continuing.",
                        exc_info=True)
        # MINT THE CODE HERE, AS LATE AS POSSIBLE, AND NOT ONE LINE EARLIER. The code used to be
        # generated at the top of this function and only typed after `wait_for_selector` (up to 20s)
        # and the trust tick -- longer than a 30s TOTP window on a slow host, so it could be stale on
        # arrival. Caught in production on Amazon 2026-08-27 (identical code path); fixed here too
        # before it bites, since Best Buy signs in far more often than either Amazon.
        if seconds_remaining() < _MIN_CODE_LIFE_SECONDS:
            page.wait_for_timeout(int((seconds_remaining() + 0.5) * 1000))
        code = totp(secret)
        page.fill(TWO_STEP_CODE_INPUT, code)
        # See the note on _MIN_CODE_LIFE_SECONDS: a rejection with a LARGE number here means a wrong
        # seed, with a SMALL number it expired in flight. Amazon/Best Buy's own message cannot tell
        # them apart.
        log.info("Best Buy 2-Step: code had %.1fs of life left when it was entered.",
                 seconds_remaining())
        log.info("Best Buy: answering 2-Step Verification with a generated authenticator code.")
    except Exception:
        log.warning("Best Buy 2-Step: the code field never appeared.", exc_info=True)
        return False

    if not _click_continue(page):
        for attempt in (
            lambda: page.get_by_role("button", name="Continue", exact=False).first.click(timeout=8000),
            lambda: page.press(TWO_STEP_CODE_INPUT, "Enter"),
        ):
            try:
                attempt()
                break
            except Exception:
                continue
    try:
        page.wait_for_url(lambda u: TWO_STEP_MARKER not in u.lower(), timeout=45000)
    except Exception:
        pass
    return not _on_two_step(page)


def _deterministic_login(page, auth) -> LoginOutcome:
    """Best Buy's 3-screen password login, agent-free (proven live). Handles the fresh
    (#fld-e editable) and remembered (email prefilled as static text, just click Continue) variants.
    Uses `button.cia-form__controls__submit` — NOT the Passkey/Apple/Google buttons — then
    `#password-radio` ("Use password", below the fold).

    Returns a LoginOutcome; `transport_failed` tells the caller the auth requests died at the network
    layer, which is recoverable by retrying off-proxy rather than a reason to give up."""
    if auth is None or auth.method != "password" or not auth.username:
        return LoginOutcome(False)
    failed_requests = _watch_failed_requests(page)
    _dismiss_survey(page)

    # Screen 1: email (typed if editable, else already prefilled) -> Continue.
    try:
        page.wait_for_selector("#fld-e", state="visible", timeout=8000)
        page.fill("#fld-e", auth.username)
    except Exception:
        if page.locator(".prefilled-value, .cia-signin__username").count() == 0:
            log.warning("Best Buy sign-in page has no email field and no prefilled email.")
            return LoginOutcome(False)
    _keep_signed_in(page)
    if not _click_continue(page):
        v, a = _log_signin_diagnostics(page, "could not click Continue on the email screen",
                                       failed_requests) or ("", "")
        return LoginOutcome(False, bool(_auth_critical(failed_requests)), _signin_reason(v, a))

    # Screen 2: method chooser -> "Use password".
    try:
        page.wait_for_selector("#password-radio", timeout=30000)
        _dismiss_survey(page)
        page.locator("#password-radio").scroll_into_view_if_needed(timeout=8000)
        page.locator("#password-radio").click(timeout=8000)
    except Exception:
        pass  # some accounts go straight to the password field

    # Screen 3: password -> submit.
    try:
        page.wait_for_selector("input[type=password]", state="visible", timeout=30000)
        page.fill("input[type=password]", auth.password)
        for attempt in (
            lambda: page.get_by_role("button", name="Sign In", exact=False).first.click(timeout=8000),
            lambda: page.get_by_role("button", name="Continue", exact=False).first.click(timeout=8000),
            lambda: page.press("input[type=password]", "Enter"),
        ):
            try:
                attempt()
                break
            except Exception:
                continue
    except Exception:
        v, a = _log_signin_diagnostics(page, "password field never appeared",
                                       failed_requests) or ("", "")
        return LoginOutcome(False, bool(_auth_critical(failed_requests)), _signin_reason(v, a))

    try:
        page.wait_for_url(lambda u: "bestbuy.com" in u and not any(m in u for m in _SIGNIN_MARKERS),
                          timeout=45000)
    except Exception:
        pass

    # 2-Step Verification: the password was ACCEPTED and Best Buy wants an authenticator code. This is
    # now the expected path, not an exception — 2FA is required on the account precisely because it is
    # the one challenge that can be answered unattended.
    if _on_two_step(page) and not _answer_two_step(page, auth):
        _log_signin_diagnostics(page, "could not answer 2-Step Verification", failed_requests)
        return LoginOutcome(False, bool(_auth_critical(failed_requests)),
                            "2-STEP VERIFICATION — Best Buy asked for an authenticator code and it "
                            "could not be supplied. WHAT TO DO: set auth.bestbuy.totp_secret in "
                            "config.json to the base32 key from the account's authenticator "
                            "enrolment (Account Settings -> Sign-in & Security).")

    if _looks_logged_out(page):
        # Everything clicked and filled, yet we are still on a sign-in URL. Live this was
        # the auth POST being rejected at the network layer, which no selector work can fix.
        v, a = _log_signin_diagnostics(page, "submitted the password but stayed logged out",
                                       failed_requests) or ("", "")
        return LoginOutcome(False, bool(_auth_critical(failed_requests)), _signin_reason(v, a))
    return LoginOutcome(True)


_INPAGE_FETCH_JS = """
async (paths) => {
  const out = {};
  for (const p of paths) {
    try {
      const r = await fetch(p, {credentials: 'include', headers: {'accept': 'application/json'}});
      let body = null;
      try { body = await r.json(); } catch (e) { body = null; }
      out[p] = {status: r.status, body: body};
    } catch (e) { out[p] = {status: 'ERR', body: null}; }
  }
  return out;
}
"""


class BestBuyApiClient:
    def __init__(self, profile):
        self.profile = profile

    def fetch_order_payloads(
        self, since_date: str, open_ids, terminal_ids
    ) -> list[dict]:
        """Return the ss-api payload for every order placed on/after `since_date` plus every still-open
        order, minus terminal ones. Discovery + details all happen in one logged-in CDP session.

        THERE IS NO OFF-PROXY SIGN-IN RETRY, and the reason is worth keeping. One was added on
        2026-08-14 on the theory that the static ISP proxy was breaking the HTTP/2 auth POSTs
        (ERR_HTTP2_PROTOCOL_ERROR on /identity/authenticate while every GET sailed through). It fired
        for real on 2026-08-15 16:00Z and failed IDENTICALLY off-proxy — same error, same endpoint. So
        the rejection travels with the BROWSER (TLS/HTTP2 fingerprint, or the Browser-Use cloud
        datacenter range), not with the egress IP, and the retry bought nothing while roughly doubling
        sign-in wall-clock on the runs that could least afford it (5m26s vs a normal ~2m50s).

        That result also rules out proxy rotation as a fix, which is why it isn't attempted here.
        `LoginOutcome.transport_failed` is kept — not to retry on, but to say plainly WHICH kind of
        failure it was, since a network-layer rejection and a page-shape change need different people
        to look at them.
        """
        return self._fetch_once(self.profile, since_date, open_ids, terminal_ids)

    def _fetch_once(
        self, profile, since_date: str, open_ids, terminal_ids
    ) -> list[dict]:
        open_ids = set(open_ids or [])
        terminal_ids = set(terminal_ids or [])

        with CdpBrowser(profile) as page:
            page.goto(PURCHASE_HISTORY_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

            def sign_in_here() -> None:
                """Self-heal a lapsed session, raising the right error if it can't."""
                auth = self.profile.auth.get("bestbuy")
                outcome = _deterministic_login(page, auth)
                if not outcome.ok:
                    log.warning("Best Buy [%s]: deterministic self-login did not succeed.",
                                self.profile.label)
                    # Both routes end the same way — skip with an alert, never the paid agent, since
                    # it cannot fix an auth failure either. The distinction is carried in the MESSAGE
                    # so whoever reads the alert knows which problem they have: a network-layer
                    # rejection is anti-bot/transport (not fixable by changing egress — proven live
                    # 2026-08-15, see fetch_order_payloads), while anything else points at the page
                    # flow or the credentials.
                    # Lead with the classified reason when the page told us one — an identity
                    # challenge, a rejected password and an anti-bot reset all end here, and the
                    # alert is useless unless it says WHICH.
                    raise ApiLoginError(
                        outcome.reason
                        or ("Best Buy session is logged out and deterministic login did not succeed; "
                            "the auth requests died at the NETWORK layer (anti-bot/transport, not the "
                            "page flow — changing egress does not help)."
                            if outcome.transport_failed else
                            "Best Buy session is logged out and deterministic login did not succeed.")
                    )
                log.info("Best Buy [%s]: deterministic self-login succeeded.", self.profile.label)
                page.goto(PURCHASE_HISTORY_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)

            attempted_login = False
            if _looks_logged_out(page):
                # Best Buy sessions die ~20-25 min, so a scheduled run routinely lands here and must
                # self-heal. Log it so the self-login is visible in run logs (and distinguishable from
                # a warm session, which skips this block entirely).
                log.info("Best Buy [%s]: session logged out; attempting deterministic self-login.",
                         self.profile.label)
                sign_in_here()
                attempted_login = True

            dates = _discover_orders(page)

            # DISCOVERY CAME BACK EMPTY. Two causes, opposite responses, and they are indistinguishable
            # from the parse alone -- which is what the old "(shape changed?)" hedge was admitting.
            # A logout must never reach the agent (it cannot fix an auth failure and costs ~$0.02 to
            # confirm what we already know); a real markup change is precisely what the agent is for.
            if not dates and (affordances := _signin_affordances(page)):
                log.info("Best Buy [%s]: no orders parsed and the page is offering to sign in (%s) -- "
                         "treating as a lapsed session, not a shape change.",
                         self.profile.label, ", ".join(affordances))
                if not attempted_login:
                    # The session expired without tripping _looks_logged_out. Try to recover it here
                    # rather than skipping: a successful login turns a lost run into a normal one.
                    sign_in_here()
                    attempted_login = True
                    dates = _discover_orders(page)
                if not dates:
                    raise ApiLoginError(
                        "Best Buy purchase history is empty and still showing a sign-in prompt; the "
                        "session is logged out. Not running the agent -- it cannot fix an auth failure."
                    )

            if not dates:
                raise BestBuyApiError("No orders found in the purchase-history page (shape changed?).")

            # Fetch details for: orders in the date window (new) + still-open orders (re-check),
            # never terminal ones. A blank discovered date can't be filtered out, so fetch it and let
            # the mapping/date filter below decide.
            to_fetch = [
                oid for oid, date in dates.items()
                if oid not in terminal_ids and (oid in open_ids or not date or date >= since_date)
            ]
            # Always include known-open orders even if they scrolled off the page.
            for oid in open_ids:
                if oid not in to_fetch and oid not in terminal_ids:
                    to_fetch.append(oid)

            log.info("Best Buy [%s]: %d order(s) on page, fetching %d detail(s) via ss-api.",
                     self.profile.label, len(dates), len(to_fetch))
            if not to_fetch:
                return []

            payloads: list[dict] = []
            results = page.evaluate(_INPAGE_FETCH_JS,
                                    [ORDER_DETAIL_PATH.format(oid) for oid in to_fetch])
            for path, res in (results or {}).items():
                oid = path.rsplit("/", 1)[-1]
                if res.get("status") == 200 and isinstance(res.get("body"), dict) and res["body"].get("order"):
                    payloads.append(res["body"])
                else:
                    log.warning("Best Buy [%s]: detail fetch for %s returned status=%s (skipped).",
                                self.profile.label, oid, res.get("status"))

        # Keep only in-window or still-open orders (using the authoritative order.created date).
        kept = []
        for payload in payloads:
            order = payload.get("order") or {}
            oid = str(order.get("userOrderId") or "")
            created = (order.get("created") or "")[:10]
            if oid in open_ids or not created or created >= since_date:
                kept.append(payload)
        return kept
