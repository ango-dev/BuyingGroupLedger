"""Costco's sign-in, driven deterministically — the last manual step in the whole ledger.

**THIS DOES NOT CHANGE HOW COSTCO IS SCRAPED.** `costco_api`'s GraphQL path stays the primary and
stays browserless: it is more reliable than any website scrape, needs no browser, and is why Costco
runs happily on a Pi. Nothing here is on that path. A browser sign-in exists for the two occasional
jobs that genuinely need a logged-in costco.com session:

  1. **Recovering the session a refresh-token grab needs.**
     `scripts/costco_token._grab_refresh_token` captures a plaintext `refresh_token` from the B2C
     token endpoint's RESPONSE, and `costco.py:_refresh_token_via_browser` calls it when the stored
     token dies. Its own docstring named the gap it could not cross: *"Reaching here means the
     profile's own Costco session is dead too, which genuinely needs a human."* **Signing in closes
     that gap** — the grab can now run at all against a lapsed profile.

     **IT DOES NOT, HOWEVER, MAKE THE CAPTURE DETERMINISTIC.** The capture needs MSAL to perform a
     real token REDEEM while the handler watches, and nothing here can command that: across six live
     runs it fired three times (including once mid-sign-in) and failed three times. TWO different
     causal stories were written into this file and both were falsified by the next run, so no third
     one is offered — see the design notes for the run-by-run table. What holds up is the mechanism, not
     any rule about when it triggers.

     `scripts/costco_token._grab_refresh_token` therefore takes TWO passes: this sign-in warms the
     session, then a FRESH browser (empty MSAL cache) gives the SPA no choice but to acquire. Two
     independent chances at the same redeem.
  2. **Receipts.** `receipts/capture.py` renders costco.com order pages in a CDP browser, and nothing
     keeps that session warm precisely because the data path opens none. A sign-in here leaves it
     warm as a side effect.

**COSTCO HAS NO 2FA IN THE US**, so unlike Best Buy and both Amazons there is no
authenticator code to generate and `auth["costco"].totp_secret` is expected to be BLANK. That is a
DEFERRAL, not an omission — Costco may add 2-step verification later, and the design notes records what to
do when it does. `_classify_signin_failure` already names an unexpected code prompt rather than
reporting a generic failure, so the day it appears the alert says so instead of going quiet.

WHAT THE LIVE PROBE ESTABLISHED (2026-08-25, profile-alpha, scripts/costco_signin_probe.py):

  - A logged-out costco.com bounces to **Azure AD B2C** at
    `signin.costco.com/<tenant>/B2C_1A_SSO_WCS_signup_signin_209/oauth2/v2.0/authorize`. A different
    identity platform from Amazon's and Best Buy's — no selector from either transfers.
  - **ONE screen holds everything**: `#signInName` (labelled "Email Address"), `#password`, and the
    submit `#next` ("Sign In"). No email-first step, no account chooser.
  - **`#rememberMe` ("Keep me signed in") ARRIVES ALREADY TICKED.** So the job is to avoid
    UN-ticking it: `check()`, never `click()`. Confirmed to take effect — the post-submit request
    carried `rememberMe=true`.
  - **THE PAGE HAS SEVEN SUBMIT BUTTONS AND MOST OF THEM ARE TRAPS.** Alongside `#next` sit
    `#SignInWithOTPUsingEmailAddressExchange` ("Receive a Passcode" — emails a code nothing here can
    read), `#NoknokExchangeSelection` ("Sign in with a passkey" — the cloud browser has no WebAuthn),
    `#PasswordResetUsingEmailAddressExchange` ("Forgot Password?" — starts a real password reset),
    and three tooltip buttons. **So there is deliberately NO generic `button[type=submit]` fallback
    here**, unlike the Amazon module: on this page a "try the next thing" fallback does not merely
    fail, it emails a passcode or begins resetting the account's password.
  - "Receive a Passcode" is a passwordless FIRST factor, not 2FA. Do not read its presence as Costco
    having added 2-step verification.
  - The page ships a hidden "your browser is blocking cookies" error template that is present in the
    DOM even on a perfectly healthy page — see `_TEMPLATE_NOISE`, or it poisons every verdict.
"""

import logging
import re
from typing import NamedTuple

log = logging.getLogger(__name__)

#: Matched against the FINAL url. `receipts/sources.py` already uses the same markers to refuse
#: storing a sign-in page as a receipt.
SIGNIN_MARKERS = ("signin.costco.com", "b2clogin.com", "/logon", "/login")

#: All CONFIRMED LIVE. Single-element tuples where the probe saw exactly one thing, and no
#: generic fallbacks — see the module docstring on why a fallback is dangerous on this page.
EMAIL_SELECTOR = "#signInName"
PASSWORD_SELECTOR = "#password"
SUBMIT_SELECTOR = "#next"
KEEP_SIGNED_IN_SELECTOR = "#rememberMe"

#: Controls that must NEVER be clicked. Kept as a named list so the danger is documented rather than
#: implied by their absence, and so a test can assert none of them is reachable from this module.
ALTERNATIVE_FLOW_SELECTORS = (
    "#SignInWithOTPUsingEmailAddressExchange",   # emails a passcode nothing here can read
    "#NoknokExchangeSelection",                  # passkey; no WebAuthn in the cloud browser
    "#PasswordResetUsingEmailAddressExchange",   # starts a real password reset
)

#: B2C ships its whole error vocabulary in the DOM and reveals items conditionally, so this text is
#: present on a HEALTHY page. Left in the evidence it makes every failure report "cookies are
#: blocked" — the same trap Amazon's permanent passkey banner sets.
_TEMPLATE_NOISE = re.compile(r"browser is currently set to block cookies|allow cookies to use this "
                             r"service|small text files stored on your computer", re.I)


class LoginOutcome(NamedTuple):
    """Result of a sign-in attempt. `reason` is what reaches a human through the alert."""

    ok: bool
    reason: str = ""


#: The account SPA routes into `#/app/<uuid>/...` once it has a session. That hash route is the only
#: POSITIVE evidence of being signed in — see `resolve_session_state` for why the absence of a
#: sign-in page is not evidence of anything.
SIGNED_IN_ROUTE_MARKER = "/myaccount/#/"


def looks_logged_out(page) -> bool:
    """Is this page Costco's B2C sign-in rather than a signed-in costco.com page?

    A cheap, ONE-SHOT check that only ever answers "yes, definitely the sign-in page". It cannot
    answer "yes, definitely signed in" — see `resolve_session_state`, which is what callers that need
    to ACT on the answer should use.
    """
    url = (page.url or "").lower()
    if any(marker in url for marker in SIGNIN_MARKERS):
        return True
    try:
        return page.locator(EMAIL_SELECTOR).count() > 0
    except Exception:  # noqa: BLE001
        return False


def resolve_session_state(page, timeout_ms: int = 20000, poll_ms: int = 1000) -> bool:
    """Wait for the account page to settle, and return True if it is LOGGED OUT.

    WHY THIS EXISTS, because `looks_logged_out` looked sufficient and was not. It infers "signed in"
    from the ABSENCE of a sign-in page, and absence is not evidence. Live a profile with
    no Costco session at all landed on a bare `https://www.costco.com/myaccount` — no B2C redirect
    yet, no `#signInName`, and near-empty storage — so it read as SIGNED IN, the self-login never
    fired, and the token grab swept an anonymous browser and gave up. A fourth landing state nobody
    had seen: across 13 earlier runs the page had only ever settled on the signed-in SPA route
    (`/myaccount/#/app/<uuid>/…`, 7x), the B2C sign-in (5x), or a mid-auth `#state=` fragment (1x).

    So this waits for one of the two SETTLED states and requires POSITIVE evidence either way:
    the SPA hash route means signed in; a B2C URL or the email field means signed out.

    **An unresolved page is treated as LOGGED OUT**, deliberately. The costs are asymmetric at every
    call site that uses this: a wrong "logged out" costs one sign-in attempt with credentials we
    already hold, while a wrong "signed in" costs the entire recovery — which is precisely the
    failure this was written for.
    """
    waited = 0
    while True:
        url = (page.url or "").lower()
        if any(marker in url for marker in SIGNIN_MARKERS):
            return True
        try:
            if page.locator(EMAIL_SELECTOR).count() > 0:
                return True
        except Exception:  # noqa: BLE001 — a selector hiccup must not decide the verdict
            pass
        if SIGNED_IN_ROUTE_MARKER in url:
            return False
        if waited >= timeout_ms:
            log.warning("Costco: %s never settled into a signed-in route or a sign-in page after "
                        "%.0fs — treating it as logged out, which costs one sign-in rather than the "
                        "whole recovery.", (page.url or "")[:90], timeout_ms / 1000)
            return True
        page.wait_for_timeout(poll_ms)
        waited += poll_ms


def _keep_signed_in(page) -> bool:
    """Assert "Keep me signed in" before submitting.

    `check()` rather than `click()`, and on this page that distinction is the whole point: Costco
    ships the box ALREADY TICKED, so a click would turn it OFF and mint a session-scoped cookie —
    undoing the one thing that keeps the session alive between the occasional runs that need it.
    Best-effort: a missing box must not fail the sign-in.
    """
    try:
        box = page.locator(KEEP_SIGNED_IN_SELECTOR).first
        if box.count() == 0:
            log.warning("Costco sign-in: no 'Keep me signed in' box found; the session will be "
                        "short-lived. Re-run scripts/costco_signin_probe if this persists.")
            return False
        was_checked = box.is_checked()
        if not was_checked:
            box.check(timeout=5000)
        log.info("Costco sign-in: 'Keep me signed in' %s.",
                 "was already ticked" if was_checked else "was UNTICKED, ticked it now")
        return True
    except Exception:  # noqa: BLE001 — never let an optional nicety break the sign-in
        log.debug("Costco sign-in: could not assert 'Keep me signed in'.", exc_info=True)
        return False


_DIAGNOSTIC_JS = r"""() => {
    const text = (document.body && document.body.innerText) || '';
    return {
        url: location.href,
        title: document.title,
        errors: Array.from(document.querySelectorAll(
                '[role=alert], .error, [class*="error"], [class*="Error"], #errormessage'))
            .map(e => (e.innerText || '').trim()).filter(Boolean).slice(0, 6),
        otp_field: !!document.querySelector(
            'input[autocomplete="one-time-code"], input[name*="otp" i], input[id*="otp" i],' +
            'input[id*="verification" i]'),
        captcha: !!document.querySelector('img[src*=captcha], iframe[src*=recaptcha], .g-recaptcha'),
        text: text.replace(/\s+/g, ' ').slice(0, 400),
    };
}"""


def _classify_signin_failure(info: dict) -> tuple[str, str]:
    """(verdict, what to do about it) for a stalled Costco sign-in.

    Costco's failures need different responses and are indistinguishable once they all end as "still
    on signin.costco.com". Template noise is stripped first — B2C ships its whole error vocabulary in
    the DOM, so the cookie warning is present on a healthy page and would otherwise win every time.
    """
    errors = " ".join(str(e) for e in (info.get("errors") or [])
                      if not _TEMPLATE_NOISE.search(str(e)))
    haystack = f"{errors} {info.get('title') or ''} {info.get('text') or ''}"

    if info.get("otp_field") or re.search(r"one[- ]time (code|passcode)|enter the code|verification "
                                         r"code", haystack, re.I):
        # Costco is believed to have no US 2FA. If this fires, that changed — and the DEFERRED work
        # in the design notes is what to pick up.
        return ("A CODE PROMPT APPEARED — Costco may have introduced 2-step verification",
                "This module deliberately has no code support, because Costco had no US 2FA when it "
                "was written. Re-run `python -m scripts.costco_signin_probe --label <profile> "
                "--login` to capture the new screen, then port the TOTP handling from "
                "scrapers/amazon_signin.py (`_answer_otp` + the trusted-device box) and add "
                "auth['costco'].totp_secret. Until then, sign in by hand.")
    if re.search(r"incorrect|does not match|invalid (email|password|sign)|we (can't|cannot) find",
                 haystack, re.I):
        return ("BAD CREDENTIAL — Costco rejected the email or password",
                "Update auth['costco'].username / .password in config.json, and stop retrying: "
                "repeated failures risk locking the membership account.")
    # SITE-LEVEL BLOCK FIRST, and the order is the whole point. Costco serves
    # "Costco.com Temporarily Unavailable" (an Akamai block/outage page) to an IP it does not like,
    # and that page has nothing to do with the account. This branch used to be folded into the one
    # below via a bare `temporarily (unavailable|disabled)`, so a blocked PROXY was reported as
    # "ACCOUNT LOCKED — too many attempts" and told the operator to go clear a lock that did not
    # exist — observed live on a new profile whose proxy Costco was blocking.
    if re.search(r"temporarily unavailable|access denied|request unsuccessful|reference #\s*\d|"
                 r"unusual (traffic|activity)|bot detection", haystack, re.I):
        return ("SITE BLOCKED OR UNAVAILABLE — Costco served an error page, not a sign-in form",
                "This is about the IP, not the account: Costco is blocking or rate-limiting this "
                "profile's proxy, or the site is down. Check the profile's `proxy` in config.json — "
                "another profile on a different proxy may sign in fine, which is itself the tell. "
                "Do NOT retry in a loop and do NOT touch the account; back off and try later.")
    # Account-specific wording only, so a site error can never land here again.
    if re.search(r"account (is |has been )?(locked|disabled|suspended)|"
                 r"too many (failed )?(sign[- ]?in |login )?attempts", haystack, re.I):
        return ("ACCOUNT LOCKED — too many attempts",
                "Stop automated sign-ins and clear it with Costco before retrying.")
    if info.get("captcha"):
        return ("CAPTCHA / BOT CHALLENGE",
                "Back off; do not retry in a loop. Sign in by hand via scripts/create_profile.")
    return ("UNKNOWN — no usable error text on the page",
            "Re-run `python -m scripts.costco_signin_probe --label <profile>` to dump the live DOM; "
            "the B2C sign-in markup may have changed.")


def _log_diagnostics(page, what_failed: str) -> tuple[str, str]:
    """Say WHY sign-in stalled. A bare failure costs the recovery and explains nothing."""
    try:
        info = page.evaluate(_DIAGNOSTIC_JS)
    except Exception:  # noqa: BLE001 — diagnostics must never mask the original failure
        log.warning("Costco sign-in: %s (page state unreadable).", what_failed, exc_info=True)
        return ("", "")
    verdict, action = _classify_signin_failure(info)
    log.warning("Costco sign-in FAILED — %s. WHAT TO DO: %s", verdict, action)
    real_errors = [e for e in (info.get("errors") or []) if not _TEMPLATE_NOISE.search(str(e))]
    if real_errors:
        log.warning("Costco sign-in: the page says: %s", real_errors[:3])
    log.warning("Costco sign-in: %s. Page state: %s", what_failed,
                {k: v for k, v in info.items() if k != "text"})
    return verdict, action


def deterministic_login(page, auth) -> LoginOutcome:
    """Sign costco.com back in, from a page already sitting on the B2C sign-in.

    One screen: email, password, keep-me-signed-in, submit. ONE ATTEMPT, NO RETRY — repeated failures
    risk locking a membership account, and a skipped recovery is cheap by comparison (the GraphQL
    path is unaffected either way; only the token mint and receipts wait for the next run).
    """
    if auth is None or auth.method != "password" or not auth.username or not auth.password:
        return LoginOutcome(False)

    try:
        page.wait_for_selector(EMAIL_SELECTOR, state="visible", timeout=20000)
    except Exception:  # noqa: BLE001 — report it properly below rather than raising
        verdict, action = _log_diagnostics(page, "the email field never appeared")
        return LoginOutcome(False, f"{verdict}. WHAT TO DO: {action}" if verdict else "")

    try:
        page.fill(EMAIL_SELECTOR, auth.username)
        page.fill(PASSWORD_SELECTOR, auth.password)
        log.info("Costco sign-in: filled the email and password.")
    except Exception:  # noqa: BLE001
        verdict, action = _log_diagnostics(page, "could not fill the credentials")
        return LoginOutcome(False, f"{verdict}. WHAT TO DO: {action}" if verdict else "")

    _keep_signed_in(page)

    try:
        # Pinned to #next on purpose. See ALTERNATIVE_FLOW_SELECTORS: the neighbouring submits email
        # a passcode, start a password reset, or open a passkey flow, so "try the next selector" is
        # not a safe fallback on this page.
        page.locator(SUBMIT_SELECTOR).first.click(timeout=10000)
        log.info("Costco sign-in: clicked Sign In via %s.", SUBMIT_SELECTOR)
    except Exception:  # noqa: BLE001
        verdict, action = _log_diagnostics(page, "could not click Sign In")
        return LoginOutcome(False, f"{verdict}. WHAT TO DO: {action}" if verdict else "")

    try:
        page.wait_for_url(lambda u: not any(m in u.lower() for m in SIGNIN_MARKERS), timeout=45000)
    except Exception:  # noqa: BLE001 — the check below is what actually decides
        pass
    page.wait_for_timeout(4000)

    if looks_logged_out(page):
        verdict, action = _log_diagnostics(page, "submitted the credentials but stayed on the "
                                                 "sign-in page")
        return LoginOutcome(False, f"{verdict}. WHAT TO DO: {action}" if verdict else "")
    log.info("Costco sign-in: signed in (%s).", (page.url or "")[:80])
    return LoginOutcome(True)
