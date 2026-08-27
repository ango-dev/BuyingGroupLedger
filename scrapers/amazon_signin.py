"""Amazon's sign-in, driven deterministically — so a lapsed session heals itself.

SHARED BY BOTH AMAZON ACCOUNTS (consumer `amazon_api` and `amazon_business_api`).
The the design notes standalone rule keeps their ORDER parsers apart, because the
order-details DOM genuinely diverges between consumer and Business. The SIGN-IN page
does not: amazon.com has one identity system, and a live probe of BOTH accounts on
2026-08-25 found the same four screen shapes and the same element ids. Sharing it means
one place to fix when Amazon serves a fifth shape, instead of two that drift.

Amazon Business used to be the ONE retailer here that could not recover from a logged-out session:
`amazon_business_api` raised on sight of a sign-in page, the scraper alerted and skipped, and nothing
was recorded until a human re-ran `scripts.create_profile`. the design notes watched that cost two
consecutive runs. The stated reason was "Amazon has OTP/2FA", and Best Buy overturned exactly that
premise on 2026-08-25: the answer is not to avoid 2FA but to enrol the ONE challenge a script can
answer unattended — an authenticator code — and generate it locally from the seed. This module is
that, for Amazon Business.

WHICH ERROR IS RAISED DECIDES WHETHER MONEY IS SPENT, and that rule is unchanged here: a failed
sign-in ends as `ApiLoginError` in the caller, which alerts and SKIPS. The paid agent is never run
for an auth failure — it cannot fix one, and it would spend real money rediscovering the logout.

WHAT THE LIVE PROBE ESTABLISHED (2026-08-25, profile-alpha, scripts/amazon_business_signin_probe.py):

  - A logged-out `/your-orders/orders` lands on `/ap/signin?...openid.return_to=…%2Fab%2Fyour-orders…`
    — note the `/ab/` path, this is the Business order history.
  - THE ACCOUNT GETS THE REMEMBERED-USER VARIANT: there is NO visible email field. The address sits
    in a HIDDEN `input[name=email]` (`#ap-claim`) and `#ap_password` + `#signInSubmit` are already on
    the landing page. So the flow is ONE screen, not the documented email → Continue → password
    three-step. Both shapes are handled, chosen by "is a password field visible?" rather than by URL,
    because Amazon serves whichever it likes and the fresh variant returns after a password reset.
  - `input[name=email]` IS NOT THE EMAIL FIELD on that variant — it is the hidden claim. Nor is
    `#ap-credential-autofill-hint`, which is a VISIBLE text input sitting right next to the password
    box. Both are why every field here is matched by id and checked for VISIBILITY before being
    typed into.
  - **`#auth-remember-me` ("Keep me signed in") is present and ARRIVES ALREADY TICKED.** So it is
    asserted with `check()`, never `click()` — clicking a ticked box turns it OFF, which would mint a
    session-scoped cookie and put us back where we started.
  - The page carried a stale **passkey error banner** ("Sorry, your passkey isn't working…") while
    being perfectly usable. See `_PASSKEY_NOISE`: read naively, that banner makes every future
    sign-in look like a failure.

PROVEN IN PRODUCTION 2026-08-25, from a genuine hand-made sign-out — `main.py amazon-business` logged
itself in unattended (`session logged out -> ... -> deterministic self-login succeeded`), answered
`/ap/mfa` with a generated code, then discovered 24 orders and built 4 rows with no agent fallback.
The signed-in session was verified to be the BUSINESS account, not the personal one sharing its
display name (`/ab/` paths, Amazon Business nav, PO/requisition chrome).

**AMAZON SERVES AT LEAST FOUR DIFFERENT SIGN-IN SHAPES TO THIS ONE ACCOUNT**, which is why this is a
state machine rather than a script — it answers whichever screen it is given, each at most once:

  1. the REMEMBERED variant (hidden `#ap-claim`, password already on the page),
  2. the FRESH variant (`#ap_email_login` -> Continue -> password) after a full sign-out,
  3. the "Switch accounts" page (`handle_account_switcher`), and
  4. `/ap/mfa`, the authenticator challenge.

They do not even agree on element ids — `#ap_email` versus `#ap_email_login` — and `#auth-remember-me`
is present on some screens and absent on others, which is why ticking it is best-effort. Each field
logs WHICH selector took it, so the first question after a future failure ("which variant was it
on?") is answered by the run log rather than by another paid probe.
"""

import logging
import re
from typing import NamedTuple

from scrapers.totp import TotpError, seconds_remaining, totp

log = logging.getLogger(__name__)

#: URL fragments that mean "this is Amazon's auth flow, not a signed-in page".
SIGNIN_MARKERS = ("/ap/signin", "/ap/mfa", "/ap/cvf", "/ap/challenge", "/ap/forgotpassword")

#: CONFIRMED LIVE. `input[name=email]` is deliberately NOT here — on the remembered
#: variant that name belongs to the hidden `#ap-claim`, and typing into `#ap-credential-autofill-hint`
#: (a visible text box beside the password) would fill the wrong element while looking successful.
#: `#ap_email_login` is not a typo of `#ap_email` — the fresh sign-in page (observed 2026-08-25 after
#: a full sign-out) uses `#ap_email_login`, while other variants use `#ap_email`. Both are kept.
EMAIL_SELECTORS = ("#ap_email", "#ap_email_login", "input[type=email]")
#: **`#continue` IS A `<span>`, NOT THE BUTTON.** Amazon's `a-button` widget wraps an id-less
#: `input.a-button-input[type=submit]` inside `<span id="continue">`, with the visible caption in a
#: separate `<span id="continue-announce" aria-hidden="true">`. Clicking the wrapper usually lands on
#: the input, but "usually" is not a thing to build a hands-off login on, so the real control is tried
#: FIRST and the wrapper is kept as the fallback for the variants that do expose an id.
CONTINUE_SELECTORS = (
    "#continue input[type=submit]",
    "input.a-button-input[aria-labelledby='continue-announce']",
    "#continue",
    "input#continue",
    "#continue-announce",
)
PASSWORD_SELECTORS = ("#ap_password", "input[type=password]")
SIGNIN_SUBMIT_SELECTORS = ("#signInSubmit", "#auth-signin-button", "input[type=submit]")
#: CONFIRMED LIVE: present and pre-ticked. Asserted anyway — a default can flip, and an untrusted
#: session is the whole problem this module exists to stop recurring.
KEEP_SIGNED_IN_SELECTORS = ("#auth-remember-me", "input[name='rememberMe']",
                            "input[type=checkbox][name*='remember' i]")

#: CONFIRMED LIVE (second probe run). Amazon sometimes answers a logged-out order-history
#: request with the "Switch accounts" page instead of a sign-in form: no email box, no password box,
#: just one tile per remembered account. `switchableAccounts` holds the tiles; each is a form posting
#: to `/ap/switchaccount` whose anchor carries the account token, and the tile's own text holds the
#: account's email (its "claim") plus, for a Business account, a `data-test-id="businessName"` row.
ACCOUNT_SWITCHER_CONTAINER = "#ap-account-switcher-container"
SWITCH_ACCOUNT_LINK = 'a[data-name="switch_account_request"]'
ACCOUNT_TILE_ANCESTOR = ".cvf-widget-form-account-switcher"
ADD_ACCOUNT_LINK = "#cvf-account-switcher-add-accounts-link"

#: CONFIRMED LIVE — and this is the answer the whole feature depended on. Amazon served
#: `/ap/mfa` ("Two-Step Verification"), the authenticator challenge we CAN answer, rather than
#: `/ap/cvf` (a code texted or mailed to a human), which nothing here could. A generated code was
#: accepted and the run landed on Your Orders. Several spellings are kept so a rename does not cost
#: the run.
OTP_MARKER = "/ap/mfa"
OTP_SELECTORS = ("#auth-mfa-otpcode", "input[name='otpCode']", "input[name='code']")
OTP_SUBMIT_SELECTORS = ("#auth-signin-button", "#signInSubmit", "input[type=submit]")
#: The box that turns 2FA from a per-run tax into a one-off. **CONFIRMED LIVE: Amazon's arrives
#: UNTICKED** (`was_checked=False`) — the opposite of Best Buy's, which is pre-ticked. Assuming
#: Best Buy's behaviour here would have left the device untrusted and demanded a fresh code on every
#: single lapse, while every log line still read "signed in".
REMEMBER_DEVICE_SELECTORS = ("#auth-mfa-remember-device", "input[name='rememberDevice']",
                             "input[type=checkbox][name*='remember' i]")

#: Don't submit a code that expires mid-flight. A code is valid for its 30s window and the submit plus
#: Amazon's round trip can outlive the tail of one — which comes back as "invalid code" and reads
#: exactly like a wrong seed, i.e. it sends you to reset a password that was never the problem.
_MIN_CODE_LIFE_SECONDS = 3

#: Alert text that is NOT evidence of anything. Observed live: the sign-in page carried a
#: passkey failure banner ("Sorry, your passkey isn't working… Sign in with your password") while the
#: password form beneath it worked fine. Amazon offers passkeys, Browser-Use's cloud browser has no
#: WebAuthn support, so this banner is expected to be PERMANENT here. Left in the
#: evidence it would make `_classify_signin_failure` report a confident wrong verdict on every single
#: failure, which is worse than the UNKNOWN it would otherwise admit to.
_PASSKEY_NOISE = re.compile(r"passkey", re.I)


class LoginOutcome(NamedTuple):
    """Result of a sign-in attempt.

    `reason` is the classified verdict plus what to do about it, and it exists because it is what
    reaches a HUMAN: it rides into `ApiLoginError` and out through the alert. Without it an expired
    password, an unanswerable SMS challenge and an anti-bot rejection all arrive as "login failed,
    check the logs" — three problems with three different fixes and one useless message.

    `transport_failed` separates a page/selector change from the auth requests dying at the NETWORK
    layer. It is reported, never retried on: the off-proxy retry built for Best Buy was falsified
    live, and Amazon punishes repeated automated sign-ins harder than Best Buy does.
    """

    ok: bool
    transport_failed: bool = False
    reason: str = ""


def looks_logged_out(page) -> bool:
    """Is this page Amazon's auth flow rather than a signed-in one?

    Kept here rather than in the API client so the login module owns the whole question of "are we
    signed in", and the client has one place to ask.
    """
    url = (page.url or "").lower()
    if any(marker in url for marker in SIGNIN_MARKERS):
        return True
    try:
        return page.locator("#ap_email, #ap_password, #ap-claim").count() > 0
    except Exception:
        return False


def _visible(page, selectors) -> str:
    """The first selector matching a VISIBLE element ('' if none do).

    Presence is not enough on an Amazon sign-in page. The remembered variant carries the account
    email in a hidden `input[name=email]`, so a `count()`-based check reports an email field that
    cannot be typed into — and the flow would take the wrong branch while every log line looked fine.
    """
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() and loc.is_visible():
                return selector
        except Exception:  # noqa: BLE001 — a selector engine hiccup must not decide the branch
            continue
    return ""


def _fill_first(page, selectors, value: str, what: str = "field") -> str:
    """Type `value` into the first VISIBLE match, returning which selector took it ('' if none).

    WHICH selector took it is logged, never the value. Amazon serves at least four different sign-in
    shapes to this one account, and they do not agree on element ids (`#ap_email` vs
    `#ap_email_login`) — so when a future run fails, the first question is always "which variant was
    it on?", and this line is the cheapest possible answer.
    """
    for selector in selectors:
        if not _visible(page, (selector,)):
            continue
        try:
            page.fill(selector, value)
            log.info("Amazon sign-in: filled the %s via %s.", what, selector)
            return selector
        except Exception:  # noqa: BLE001 — try the next spelling
            log.debug("Amazon sign-in: fill via %s failed", selector, exc_info=True)
    return ""


def _click_first(page, selectors, what: str) -> bool:
    """Click the first selector that works. The last resort dispatches the click on the element
    directly, which bypasses hit-testing — worth having, because the alternative is losing the run to
    something invisible sitting over the button."""
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() == 0:
                continue
            loc.click(timeout=8000)
            log.info("Amazon sign-in: clicked %s via %s.", what, selector)
            return True
        except Exception:  # noqa: BLE001 — try the next strategy, report only if all fail
            log.debug("Amazon sign-in: click %s via %s failed", what, selector, exc_info=True)
    for selector in selectors:
        try:
            page.eval_on_selector(selector, "el => el.click()")
            log.info("Amazon sign-in: clicked %s via a dispatched el.click() on %s.",
                     what, selector)
            return True
        except Exception:  # noqa: BLE001
            continue
    log.warning("Amazon sign-in: could not click %s.", what)
    return False


def _keep_signed_in(page) -> bool:
    """Assert "Keep me signed in" before submitting.

    `check()` rather than `click()`, and the distinction is the whole point: Amazon ships this box
    ALREADY TICKED (confirmed live, `#auth-remember-me` `checked: true`), so a click would
    turn it OFF and mint the short-lived session cookie instead of the persistent token. That is the
    exact mistake the Best Buy deterministic path made for ~16 consecutive runs before anyone noticed,
   and it is invisible: every log line still says "signed in".

    Best-effort — a missing box is not a failure, since Amazon has historically defaulted it on.
    """
    for selector in KEEP_SIGNED_IN_SELECTORS:
        try:
            box = page.locator(selector).first
            if box.count() == 0:
                continue
            if box.is_checked():
                log.debug("Amazon sign-in: 'Keep me signed in' already ticked (%s).", selector)
                return True
            box.check(timeout=4000)
            log.info("Amazon sign-in: ticked 'Keep me signed in' via %s.", selector)
            return True
        except Exception:  # noqa: BLE001 — never let an optional nicety break the sign-in
            continue
    log.debug("Amazon sign-in: no 'Keep me signed in' control found; continuing.")
    return False


#: Hosts whose failure EXPLAINS an auth rejection rather than merely accompanying it. Best Buy taught
#: this the expensive way: every click and fill worked and sign-in still failed,
#: because the auth POST died where no selector can see it.
_AUTH_CRITICAL_HOSTS = ("/ap/signin", "/ap/mfa", "/ap/cvf", "/errors/validateCaptcha")


def _watch_failed_requests(page) -> list:
    """Collect network-level request failures during sign-in, so a transport problem is not mistaken
    for a selector one. Telemetry must never be what breaks a login, hence the blanket guards."""
    failed: list = []

    def record(request):
        try:
            if len(failed) < 20:
                failed.append({"url": request.url[:120],
                               "type": request.resource_type,
                               "error": request.failure or ""})
        except Exception:  # noqa: BLE001
            pass

    try:
        page.on("requestfailed", record)
    except Exception:  # noqa: BLE001 — a page without event support still logs DOM diagnostics
        pass
    return failed


def _auth_critical(failed_requests: list | None) -> list:
    """The subset of failed requests that explain an auth rejection (see _AUTH_CRITICAL_HOSTS)."""
    return [f for f in (failed_requests or [])
            if any(host in f.get("url", "") for host in _AUTH_CRITICAL_HOSTS)]


def _classify_signin_failure(info: dict, critical: list) -> tuple[str, str]:
    """(verdict, what to do about it) for a stalled sign-in.

    Every one of these failures ends identically — "submitted the password and we are still on an
    auth page" — and their fixes are opposite: a stale password is a config edit, an SMS challenge
    needs a human, and an anti-bot rejection needs backing OFF because retrying deepens it. Best Buy
    spent days in 2026-08-23 reading a simply-wrong password as an anti-bot failure for want of this.
    Amazon states its reason on the page; read it.
    """
    errors = " ".join(str(e) for e in (info.get("errors") or [])
                      if not _PASSKEY_NOISE.search(str(e)))
    haystack = f"{errors} {info.get('title') or ''} {info.get('text') or ''}"
    url = str(info.get("url") or "").lower()

    if re.search(r"password is incorrect|your password is wrong|cannot find an account|"
                 r"there was a problem.{0,40}password", haystack, re.I):
        return ("BAD CREDENTIAL — Amazon says the password is wrong",
                "Update the profile's auth['amazon-business'].password in config.json, and STOP "
                "retrying: repeated failed sign-ins are what escalate an Amazon account to a forced "
                "reset or a lock.")
    if re.search(r"account has been locked|too many (failed )?attempts|temporarily locked", haystack, re.I):
        return ("ACCOUNT LOCKED — too many attempts",
                "Stop all automated sign-ins and unlock the account with Amazon before retrying. Do "
                "not re-run the scrape until it is cleared by hand.")
    if info.get("captcha") or re.search(r"enter the characters|type the characters|solve this puzzle",
                                        haystack, re.I):
        return ("CAPTCHA / BOT CHALLENGE",
                "Back off; do not retry in a loop. The paid agent cannot solve it either, so it is "
                "deliberately not run. Sign in by hand via scripts/create_profile if it persists.")
    if "/ap/cvf" in url or re.search(r"one time password|otp.{0,20}(sent|mobile|email)|"
                                     r"we (?:have )?sent (?:you )?a code|verify your identity",
                                     haystack, re.I):
        return ("OTP TO PHONE/EMAIL — Amazon wants a code it sent to a human",
                "Nothing here can receive that code. Enrol an AUTHENTICATOR APP on the Amazon "
                "Business account (Login & Security -> 2-step verification) and put its base32 seed "
                "in auth['amazon-business'].totp_secret — an authenticator code is the one challenge "
                "this can answer unattended. Until then, sign in by hand via scripts/create_profile.")
    if OTP_MARKER in url or re.search(r"invalid.{0,20}code|code.{0,20}(is )?(incorrect|invalid)",
                                      haystack, re.I):
        return ("2FA CODE REJECTED — the authenticator code was not accepted",
                "Check auth['amazon-business'].totp_secret matches the seed enrolled on the account, "
                "and that this machine's clock is correct — TOTP is time-based, so a drifting clock "
                "produces valid-looking codes that are always wrong.")
    if critical:
        return ("ANTI-BOT / TRANSPORT — auth requests died at the network layer",
                "Not a page-shape problem, so the agent cannot fix it. Back off and retry later; see "
                "reference-isp-proxy-breaks-post.")
    return ("UNKNOWN — no usable error text on the page and no auth-critical request failures",
            "Inspect the page state logged below, and re-run scripts/amazon_business_signin_probe to "
            "dump the live DOM; the sign-in markup may have changed.")


_DIAGNOSTIC_JS = r"""() => {
    const text = (document.body && document.body.innerText) || '';
    return {
        url: location.href,
        title: document.title,
        // Amazon's own copy names the cause outright. Without it a wrong password is
        // indistinguishable from an anti-bot rejection.
        errors: Array.from(document.querySelectorAll(
                '#auth-error-message-box, #auth-warning-message-box, [role=alert], .a-alert-content'))
            .map(e => (e.innerText || '').trim()).filter(Boolean).slice(0, 4),
        password_field: !!document.querySelector('#ap_password'),
        otp_field: !!document.querySelector('#auth-mfa-otpcode'),
        captcha: !!document.querySelector('#auth-captcha-image, img[src*=captcha]'),
        text: text.replace(/\s+/g, ' ').slice(0, 400),
    };
}"""


def _log_signin_diagnostics(page, what_failed: str, failed_requests: list | None = None):
    """Say WHY sign-in stalled, since the caller can only return False.

    A bare failure costs the whole retailer for that run and tells you nothing — a changed DOM, a
    rejected password and a CAPTCHA interstitial look identical from here and have entirely different
    fixes. Returns (verdict, action) so the reason can travel out to the alert.
    """
    info: dict = {}
    try:
        info = page.evaluate(_DIAGNOSTIC_JS)
    except Exception:  # noqa: BLE001 — diagnostics must never mask the original failure
        log.warning("Amazon sign-in: %s (page state unreadable).", what_failed, exc_info=True)

    critical = _auth_critical(failed_requests)

    verdict = action = ""
    if info:
        verdict, action = _classify_signin_failure(info, critical)
        # Lead with the verdict: this is the line a human reads first in a wall of scheduled-run logs.
        log.warning("Amazon sign-in FAILED — %s. WHAT TO DO: %s", verdict, action)
        if info.get("errors"):
            log.warning("Amazon sign-in: the page says: %s", info["errors"])
        log.warning("Amazon sign-in: %s. Page state: %s", what_failed,
                    {k: v for k, v in info.items() if k != "text"})

    if failed_requests:
        if critical:
            log.warning(
                "Amazon sign-in: %d auth-critical request(s) FAILED at the network layer — "
                "a connectivity/anti-bot problem, NOT a page-shape one, so the agent fallback cannot "
                "fix it either: %s", len(critical), critical[:6],
            )
        else:
            log.warning("Amazon sign-in: %d request(s) failed (none auth-critical): %s",
                        len(failed_requests), failed_requests[:6])
    return verdict, action


def _signin_reason(verdict: str, action: str) -> str:
    return f"{verdict}. WHAT TO DO: {action}" if verdict else ""


def on_account_switcher(page) -> bool:
    """Is Amazon showing "Switch accounts" rather than a sign-in form?"""
    try:
        return page.locator(ACCOUNT_SWITCHER_CONTAINER).count() > 0
    except Exception:  # noqa: BLE001
        return False


def handle_account_switcher(page, auth) -> str:
    """Pick this profile's account off the "Switch accounts" page.

    **PICKING THE WRONG TILE IS THE WORST OUTCOME AVAILABLE HERE**, and it would not look like a
    failure: the run would sign in perfectly, scrape a different Amazon account's order history, and
    write it to the ledger under this profile. This account genuinely has a personal and a business
    identity under the SAME display name ("Test Buyer" twice, observed live), which is exactly the
    linking the separate-profile rule exists to prevent. So the tile is matched on the configured
    USERNAME — the one thing that differs — never on the name and never on position.

    Returns 'switched' | 'add_account' | '' (could not act). Anything ambiguous returns '' rather
    than guessing: a skipped run is recoverable, a ledger full of the wrong account's orders is not.
    """
    try:
        tiles = page.locator(SWITCH_ACCOUNT_LINK)
        total = tiles.count()
    except Exception:  # noqa: BLE001
        return ""

    wanted = (auth.username or "").strip().lower()
    matched = []
    for i in range(total):
        try:
            tile = tiles.nth(i)
            text = tile.evaluate(
                "el => ((el.closest('%s') || el.parentElement || {}).innerText || '')"
                % ACCOUNT_TILE_ANCESTOR
            )
        except Exception:  # noqa: BLE001 — an unreadable tile is simply not a match
            continue
        if wanted and wanted in (text or "").lower():
            matched.append(i)

    if len(matched) == 1:
        try:
            tiles.nth(matched[0]).click(timeout=10000)
            log.info("Amazon sign-in: switched into the account matching the configured "
                     "username (%d account tile(s) offered).", total)
            page.wait_for_timeout(4000)
            return "switched"
        except Exception:  # noqa: BLE001
            log.warning("Amazon sign-in: could not click the matching account tile.",
                        exc_info=True)
            return ""

    if len(matched) > 1:
        # Never guess between two accounts. Whichever is picked, the ledger silently fills with
        # someone else's orders and nothing about the run looks wrong.
        log.warning("Amazon sign-in: %d account tiles match the configured username — "
                    "refusing to guess which account to sign into.", len(matched))
        return ""

    # No tile for this account. "Add account" is the deterministic escape hatch: it leads to a fresh
    # email + password form for the account we actually want, rather than into someone else's.
    log.info("Amazon sign-in: none of the %d offered account(s) match the configured "
             "username; using 'Add account' to sign in fresh.", total)
    if _click_first(page, (ADD_ACCOUNT_LINK,), "Add account"):
        page.wait_for_timeout(4000)
        return "add_account"
    return ""


def _on_otp(page) -> bool:
    try:
        if OTP_MARKER in (page.url or "").lower():
            return True
        return bool(_visible(page, OTP_SELECTORS))
    except Exception:  # noqa: BLE001
        return False


def _trust_this_device(page) -> bool:
    """Tick "Don't ask for codes on this device" before submitting the code.

    This is what decides whether 2FA is a one-off or a per-run tax, so unlike Best Buy's equivalent
    it is not assumed to arrive pre-ticked — it is read and set. Best-effort: failing to find it
    costs a code next run, whereas refusing to sign in would cost the whole run.
    """
    for selector in REMEMBER_DEVICE_SELECTORS:
        try:
            box = page.locator(selector).first
            if box.count() == 0:
                continue
            was_checked = box.is_checked()
            if not was_checked:
                box.check(timeout=5000)
            # Say which it was. "Already ticked" and "we ticked it" are different facts about the
            # account, and only one of them is evidence that this run is what trusted the device.
            log.info("Amazon 2FA: trusting this device via %s (%s).", selector,
                     "was already ticked" if was_checked else "was UNTICKED, ticked it now")
            return True
        except Exception:  # noqa: BLE001
            continue
    log.warning("Amazon 2FA: could not find the 'don't ask for codes on this device' box; "
                "continuing without it — expect a code on the next lapse too.")
    return False


def _answer_otp(page, auth) -> bool:
    """Answer Amazon's authenticator challenge with a code generated on this host.

    Returns False rather than raising when no usable secret is configured, so the caller reports the
    usual "a human is needed" verdict instead of crashing — a crash here would escape as a
    page-shape error and spend the PAID agent on an auth problem it cannot fix.
    """
    secret = getattr(auth, "totp_secret", "")
    if not secret:
        log.warning("Amazon 2-step verification is required but no totp_secret is configured "
                    "for this profile — add auth['amazon-business'].totp_secret to config.json.")
        return False
    # Validate the seed NOW, before touching the page: a malformed secret must decline without
    # submitting anything. The code generated here is deliberately discarded -- see below.
    try:
        totp(secret)
    except TotpError:
        log.warning("Amazon 2-step verification: the configured totp_secret is not valid "
                    "base32.", exc_info=True)
        return False

    try:
        page.wait_for_selector(OTP_SELECTORS[0], state="visible", timeout=20000)
    except Exception:  # noqa: BLE001 — the id may have changed; _fill_first still tries the others
        log.debug("Amazon 2FA: %s never appeared; trying the other spellings.",
                  OTP_SELECTORS[0], exc_info=True)

    # Trust the device BEFORE submitting — afterwards the screen is gone and the box with it.
    _trust_this_device(page)

    # MINT THE CODE HERE, AS LATE AS POSSIBLE, AND NOT ONE LINE EARLIER.
    #
    # This ordering is the whole fix for a real production failure (2026-08-27): a code was
    # generated at the TOP of this function and only typed after `wait_for_selector` (up to 20s) and
    # the trusted-device tick. A TOTP window is 30 seconds, so on a slower host the code had rolled
    # over by the time it was submitted and Amazon answered "The code you entered is not valid" --
    # which is indistinguishable from a wrong seed, and sent the diagnosis after the seed and the
    # clock, both of which were provably fine. The freshness guard was checked at the top too, so it
    # guarded the one moment that did not matter.
    #
    # Waiting out the tail of a window costs at most a few seconds; submitting a stale code costs
    # the run and looks like a credential problem.
    if seconds_remaining() < _MIN_CODE_LIFE_SECONDS:
        page.wait_for_timeout(int((seconds_remaining() + 0.5) * 1000))
    code = totp(secret)

    filled = _fill_first(page, OTP_SELECTORS, code, "2FA code")
    if not filled:
        log.warning("Amazon 2FA: the code field never appeared.")
        return False
    log.info("Amazon: answering 2-step verification with a generated authenticator code.")

    if not _click_first(page, OTP_SUBMIT_SELECTORS, "the 2FA submit"):
        try:
            page.press(filled, "Enter")
        except Exception:  # noqa: BLE001
            log.debug("Amazon 2FA: pressing Enter failed too.", exc_info=True)

    try:
        page.wait_for_url(lambda u: OTP_MARKER not in u.lower(), timeout=45000)
    except Exception:  # noqa: BLE001 — the check below is what actually decides
        pass
    return not _on_otp(page)


def deterministic_login(page, auth) -> LoginOutcome:
    """Sign an Amazon account back in, agent-free, from a page already sitting on the auth flow.

    Shared by BOTH Amazon clients (consumer `amazon_api` and `amazon_business_api`) -- which is why
    nothing here names a retailer. The CALLER's own log line identifies which account is signing in;
    a retailer name in these messages would be wrong half the time, and the line a human reads first
    when diagnosing is the worst possible place to be wrong.

    Handles both shapes Amazon serves: the REMEMBERED variant (email prefilled in a hidden input, the
    password field already on the landing page — what this account got on 2026-08-25) and the fresh
    email → Continue → password variant. The branch is decided by whether a password field is
    VISIBLE, not by the URL, because both live at `/ap/signin` and Amazon picks between them.

    ONE ATTEMPT, NO RETRY. Not an oversight: Best Buy's off-proxy retry was falsified live and repeated automated sign-ins are precisely what escalates an Amazon account to a forced
    password reset. A skipped run is recoverable; a locked account is not.
    """
    if auth is None or auth.method != "password" or not auth.username or not auth.password:
        return LoginOutcome(False)

    failed_requests = _watch_failed_requests(page)

    def _fail(what_failed: str, reason: str = "") -> LoginOutcome:
        verdict, action = _log_signin_diagnostics(page, what_failed, failed_requests) or ("", "")
        return LoginOutcome(False, bool(_auth_critical(failed_requests)),
                            reason or _signin_reason(verdict, action))

    # Each screen is handled AT MOST ONCE. Amazon served two different landing screens on two
    # consecutive probe runs (the switcher, then the password form), so the order cannot be assumed —
    # but a step that repeats means it did not take, and re-submitting a password in a loop is how an
    # account gets locked. `seen` is what turns "keep going until signed in" into a bounded walk.
    seen: set[str] = set()
    for _ in range(len(_SCREENS) + 1):
        if not looks_logged_out(page) and not on_account_switcher(page):
            return LoginOutcome(True)

        if on_account_switcher(page) and "switcher" not in seen:
            seen.add("switcher")
            if not handle_account_switcher(page, auth):
                return _fail(
                    "could not pick this profile's account off the 'Switch accounts' page",
                    "ACCOUNT SWITCHER — Amazon offered a choice of remembered accounts and none "
                    "unambiguously matched auth['amazon-business'].username. WHAT TO DO: check that "
                    "username is the Business account's own email. Nothing was clicked on purpose — "
                    "signing into the WRONG account would scrape another account's orders into this "
                    "ledger and look like a completely successful run.")
            continue

        if _on_otp(page) and "otp" not in seen:
            # The password was ACCEPTED and Amazon wants a code. This is the EXPECTED path, not an
            # exception — the authenticator is enrolled precisely because it is answerable unattended.
            seen.add("otp")
            if not _answer_otp(page, auth):
                return _fail(
                    "could not answer 2-step verification",
                    "2-STEP VERIFICATION — Amazon asked for an authenticator code and it could not "
                    "be supplied. WHAT TO DO: set auth['amazon-business'].totp_secret in config.json "
                    "to the base32 key from the account's authenticator enrolment (Login & Security "
                    "-> 2-step verification -> Authenticator App -> \"Can't scan the barcode?\").")
            continue

        if _visible(page, PASSWORD_SELECTORS) and "password" not in seen:
            seen.add("password")
            if not _fill_first(page, PASSWORD_SELECTORS, auth.password, "password"):
                return _fail("the password field never appeared")
            # Amazon ships this ticked; assert it without un-ticking it. See _keep_signed_in.
            _keep_signed_in(page)
            if not _click_first(page, SIGNIN_SUBMIT_SELECTORS, "Sign in"):
                return _fail("could not submit the password")
            _settle(page)
            continue

        if _visible(page, EMAIL_SELECTORS) and "email" not in seen:
            seen.add("email")
            if not _fill_first(page, EMAIL_SELECTORS, auth.username, "email"):
                return _fail("the email field could not be filled")
            _keep_signed_in(page)
            if not _click_first(page, CONTINUE_SELECTORS, "Continue"):
                return _fail("could not click Continue on the email screen")
            _settle(page)
            continue

        return _fail("no sign-in step left to take and the session is still logged out")

    return _fail("the sign-in flow did not settle within the expected number of screens")


#: The screens this login knows how to answer. Only its LENGTH is used — it bounds the walk above.
_SCREENS = ("switcher", "email", "password", "otp")


def _settle(page) -> None:
    """Wait for a submitted screen to become the next one.

    A challenge legitimately keeps us on an auth URL, so this cannot insist on leaving `/ap/`; it
    just gives the navigation time to land before the loop re-reads what is on screen.
    """
    try:
        page.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:  # noqa: BLE001 — not every fake/driver has it, and the wait below still helps
        pass
    try:
        page.wait_for_timeout(4000)
    except Exception:  # noqa: BLE001
        pass
