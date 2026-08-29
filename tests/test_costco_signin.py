"""Costco's deterministic sign-in, against a fake Playwright page.

Costco's GraphQL data path is unaffected by any of this and stays primary — the browser sign-in
exists only to mint a refresh token and to keep receipt capture alive. So the tests here pin the
things that would fail SILENTLY or DESTRUCTIVELY:

  - un-ticking a "Keep me signed in" box that arrived ticked,
  - clicking one of the six OTHER submit buttons on that page, which email a passcode or start a
    password reset,
  - and reading B2C's always-present error template as evidence of a failure.

The DOM modelled below is what scripts/costco_signin_probe.py actually observed on 2026-08-25.
"""

import pytest

from models.profile import RetailerAuth
from scrapers import costco_signin as signin

SIGNIN_URL = ("https://signin.costco.com/e0714dd4/B2C_1A_SSO_WCS_signup_signin_209/"
              "oauth2/v2.0/authorize?client_id=4900eb1f")
SIGNED_IN_URL = "https://www.costco.com/myaccount/#/app/4900eb1f/ordersandpurchases"

#: B2C ships its whole error vocabulary in the DOM, so this is present on a HEALTHY page.
COOKIE_TEMPLATE = ("We can't sign you in. Your browser is currently set to block cookies. You need "
                   "to allow cookies to use this service. Cookies are small text files stored on "
                   "your computer.")


class FakePage:
    """Costco's single sign-in screen: email, password, a pre-ticked box, and seven submits."""

    def __init__(self, *, present=None, checked=True, url=SIGNIN_URL, errors=(),
                 signs_in=True, otp_field=False):
        self.present = set(present if present is not None else {
            signin.EMAIL_SELECTOR, signin.PASSWORD_SELECTOR,
            signin.KEEP_SIGNED_IN_SELECTOR, signin.SUBMIT_SELECTOR,
            *signin.ALTERNATIVE_FLOW_SELECTORS,
        })
        self.checked = checked
        self.url = url
        self.errors = list(errors)
        self.signs_in = signs_in
        self.otp_field = otp_field
        self.filled: dict[str, str] = {}
        self.clicked: list[str] = []

    def locator(self, selector):
        page = self

        class _Loc:
            first = property(lambda s: s)

            def count(self):
                return 1 if selector in page.present else 0

            def is_checked(self):
                return page.checked

            def check(self, timeout=None):
                page.checked = True

            def click(self, timeout=None):
                if selector not in page.present:
                    raise RuntimeError("no such element")
                page.clicked.append(selector)
                if selector == signin.SUBMIT_SELECTOR and page.signs_in:
                    page.url = SIGNED_IN_URL
                    page.present.clear()

        return _Loc()

    def fill(self, selector, value):
        if selector not in self.present:
            raise RuntimeError(f"{selector} not present")
        self.filled[selector] = value

    def wait_for_selector(self, selector, **kwargs):
        if selector not in self.present:
            raise RuntimeError("never appeared")

    def wait_for_url(self, matcher, timeout=None):
        pass

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, script):
        return {"url": self.url, "title": "Sign In | Costco", "errors": self.errors,
                "otp_field": self.otp_field, "captcha": False, "text": " ".join(self.errors)}


def _auth(**kw):
    kw.setdefault("username", "u@example.com")
    kw.setdefault("password", "pw")
    return RetailerAuth(method="password", **kw)


class TestTheHappyPath:
    def test_it_fills_both_fields_and_clicks_sign_in(self):
        page = FakePage()

        outcome = signin.deterministic_login(page, _auth())

        assert outcome.ok is True
        assert page.filled == {signin.EMAIL_SELECTOR: "u@example.com",
                               signin.PASSWORD_SELECTOR: "pw"}
        assert page.clicked == [signin.SUBMIT_SELECTOR]

    def test_no_totp_secret_is_fine_because_costco_has_no_2fa(self):
        """Unlike Best Buy and both Amazons, a blank seed here is CORRECT, not a gap."""
        page = FakePage()

        assert signin.deterministic_login(page, _auth(totp_secret="")).ok is True


class TestKeepMeSignedIn:
    """Costco ships `#rememberMe` ALREADY TICKED (confirmed live, and the post-submit request carried
    `rememberMe=true`). A click would turn it OFF and mint a session-scoped cookie — undoing the one
    thing keeping the session alive between the occasional runs that need it."""

    def test_a_pre_ticked_box_is_left_ticked_and_never_clicked(self):
        page = FakePage(checked=True)

        signin.deterministic_login(page, _auth())

        assert page.checked is True
        assert signin.KEEP_SIGNED_IN_SELECTOR not in page.clicked, "clicking a ticked box unticks it"

    def test_an_unticked_box_is_ticked(self):
        page = FakePage(checked=False)

        signin.deterministic_login(page, _auth())

        assert page.checked is True

    def test_a_missing_box_does_not_fail_the_sign_in(self):
        page = FakePage()
        page.present.discard(signin.KEEP_SIGNED_IN_SELECTOR)

        assert signin.deterministic_login(page, _auth()).ok is True


class TestTheDangerousNeighbours:
    """Costco's sign-in page carries SEVEN submit buttons. Six of them are traps: two start flows
    that email a passcode or reset the password, one opens a passkey flow the cloud browser cannot
    do, and three are tooltips. A "try the next selector" fallback here does not merely fail — it
    takes a destructive action against a real account.
    """

    def test_only_the_pinned_submit_is_ever_clicked(self):
        page = FakePage()

        signin.deterministic_login(page, _auth())

        assert page.clicked == [signin.SUBMIT_SELECTOR]
        for trap in signin.ALTERNATIVE_FLOW_SELECTORS:
            assert trap not in page.clicked

    def test_a_missing_sign_in_button_FAILS_rather_than_falling_back(self):
        """The whole point: with #next gone, the safe outcome is a clean failure, NOT clicking
        whatever else looks like a submit."""
        page = FakePage()
        page.present.discard(signin.SUBMIT_SELECTOR)

        outcome = signin.deterministic_login(page, _auth())

        assert outcome.ok is False
        assert page.clicked == [], "nothing else on this page may be clicked as a substitute"

    def test_the_module_exposes_the_traps_by_name(self):
        """Documented rather than merely absent, so the next person adding a fallback sees why."""
        assert "SignInWithOTPUsingEmailAddressExchange" in " ".join(signin.ALTERNATIVE_FLOW_SELECTORS)
        assert "PasswordResetUsingEmailAddressExchange" in " ".join(signin.ALTERNATIVE_FLOW_SELECTORS)


class TestVerdicts:
    @staticmethod
    def _verdict(errors=(), otp_field=False, text=""):
        info = {"errors": list(errors), "title": "", "text": text, "otp_field": otp_field,
                "captcha": False}
        return signin._classify_signin_failure(info)

    def test_the_cookie_template_is_not_mistaken_for_a_failure(self):
        """It is in the DOM of a perfectly healthy page. Read as evidence it would win every time and
        make the verdict confidently wrong -- the same trap Amazon's passkey banner sets."""
        verdict, _ = self._verdict(errors=[COOKIE_TEMPLATE])

        assert "UNKNOWN" in verdict, "an honest UNKNOWN beats a confident wrong verdict"

    def test_a_real_error_still_wins_over_the_template(self):
        verdict, _ = self._verdict(errors=[COOKIE_TEMPLATE, "Your password is incorrect"])

        assert "BAD CREDENTIAL" in verdict

    def test_an_unexpected_code_prompt_says_costco_may_have_ADDED_2fa(self):
        """The deferral's tripwire. Costco had no US 2FA when this was written, so if a code screen
        ever appears the alert must say what changed and where the deferred work is -- not report a
        generic failure and go quiet."""
        verdict, action = self._verdict(otp_field=True)

        assert "2-step verification" in verdict
        assert "amazon_signin" in action, "it must point at the module the TOTP handling ports from"
        assert "totp_secret" in action

    def test_a_locked_account_stops_retrying(self):
        verdict, _ = self._verdict(errors=["Your account has been locked"])

        assert "ACCOUNT LOCKED" in verdict

    def test_every_verdict_comes_with_an_action(self):
        for kwargs in ({"errors": ["Your password is incorrect"]}, {"otp_field": True},
                       {"errors": ["account is locked"]}, {}):
            _, action = self._verdict(**kwargs)
            assert action and len(action) > 20, "a verdict without a next step is not actionable"


class TestItDeclinesSafely:
    @pytest.mark.parametrize("auth", [
        None,
        RetailerAuth(method="password", username="", password="pw"),
        RetailerAuth(method="password", username="u@e.com", password=""),
    ])
    def test_incomplete_credentials_attempt_nothing(self, auth):
        page = FakePage()

        assert signin.deterministic_login(page, auth).ok is False
        assert page.filled == {} and page.clicked == []

    def test_staying_on_the_signin_page_is_reported_with_a_reason(self):
        page = FakePage(signs_in=False, errors=["The email or password is incorrect"])

        outcome = signin.deterministic_login(page, _auth())

        assert outcome.ok is False
        assert "BAD CREDENTIAL" in outcome.reason
        assert "config.json" in outcome.reason


class TestLoggedOutDetection:
    def test_a_b2c_url_reads_as_logged_out(self):
        assert signin.looks_logged_out(FakePage(url=SIGNIN_URL)) is True

    def test_a_signed_in_costco_page_does_not(self):
        page = FakePage(url=SIGNED_IN_URL, present=set())

        assert signin.looks_logged_out(page) is False


class TestTheTwoPassGrab:
    """`_grab_refresh_token` takes two passes at the same redeem, and the second one has never run
    live -- every real run so far captured in pass 1. That is exactly why the ORCHESTRATION is
    pinned here: a fallback nobody has watched execute is worth proving offline, or its presence in
    the code is mistaken for evidence that it works.

    Pass 1 warms the session (signing in if needed). Pass 2 throws that browser away and opens a
    FRESH one, so the SPA has an empty MSAL cache and must acquire rather than serve itself -- the
    condition three live captures happened under.
    """

    @staticmethod
    def _patch(monkeypatch, results):
        """Drive _grab_refresh_token with a scripted _one_pass, recording the sign_in flags."""
        from scripts import costco_token

        calls = []
        seq = list(results)

        def fake_grab(label: str):
            # Mirror the real function's two-pass tail without opening a browser.
            def _one_pass(sign_in: bool):
                calls.append(sign_in)
                return seq.pop(0)

            token = _one_pass(sign_in=True)
            if token:
                return token
            return _one_pass(sign_in=False)

        monkeypatch.setattr(costco_token, "_grab_refresh_token", fake_grab)
        return costco_token, calls

    def test_a_capture_in_pass_one_does_not_open_a_second_browser(self, monkeypatch):
        """The common case. A second cloud browser must not be spent once we already have a token."""
        costco_token, calls = self._patch(monkeypatch, ["rt-from-pass-1", "unreachable"])

        assert costco_token._grab_refresh_token("profile-alpha") == "rt-from-pass-1"
        assert calls == [True], "pass 2 must not run when pass 1 already captured"

    def test_a_failed_pass_one_retries_in_a_fresh_browser_WITHOUT_signing_in_again(self, monkeypatch):
        """The unexercised path. Pass 2 must not sign in again: pass 1 already did, and pass 2's
        entire purpose is to arrive with a COLD cache against the cookie pass 1 left warm."""
        costco_token, calls = self._patch(monkeypatch, [None, "rt-from-pass-2"])

        assert costco_token._grab_refresh_token("profile-alpha") == "rt-from-pass-2"
        assert calls == [True, False], "exactly two passes, and only the first signs in"

    def test_both_passes_failing_returns_None_rather_than_raising(self, monkeypatch):
        """The caller (costco.py) alerts and skips on None. An exception here would escape as a
        page-shape error and could reach the paid agent for what is an auth problem."""
        costco_token, calls = self._patch(monkeypatch, [None, None])

        assert costco_token._grab_refresh_token("profile-alpha") is None
        assert calls == [True, False]


class TestTheRealGrabIsWiredTheSameWay:
    """The test above scripts the two-pass tail; this one checks the REAL function still has that
    shape, so the two cannot drift apart silently."""

    def test_grab_refresh_token_calls_one_pass_twice_with_sign_in_then_not(self):
        import inspect

        from scripts import costco_token

        src = inspect.getsource(costco_token._grab_refresh_token)
        assert "_one_pass(sign_in=True)" in src, "pass 1 must sign in"
        assert "_one_pass(sign_in=False)" in src, "pass 2 must NOT sign in again"
        assert src.index("_one_pass(sign_in=True)") < src.index("_one_pass(sign_in=False)")


class TestResolveSessionState:
    """`looks_logged_out` infers "signed in" from the ABSENCE of a sign-in page, and absence is not
    evidence. a profile with no Costco session landed on a bare
    `https://www.costco.com/myaccount` — no B2C redirect yet, no `#signInName`, near-empty storage —
    so it read as SIGNED IN, the self-login never fired, and the token grab swept an anonymous
    browser and gave up. A fourth landing state, after 13 runs that had only ever shown three.
    """

    class _Page:
        """A page whose URL settles after a given number of polls, as the real SPA does."""

        def __init__(self, urls):
            self._urls = list(urls)
            self.url = self._urls.pop(0)
            self.waits = 0

        def locator(self, selector):
            class _L:
                def count(self):
                    return 0

            return _L()

        def wait_for_timeout(self, ms):
            self.waits += 1
            if self._urls:
                self.url = self._urls.pop(0)

    SIGNED_IN = "https://www.costco.com/myaccount/#/app/4900eb1f/ordersandpurchases"
    BARE = "https://www.costco.com/myaccount"

    def test_the_spa_route_is_the_only_thing_that_means_signed_in(self):
        page = self._Page([self.SIGNED_IN])

        assert signin.resolve_session_state(page) is False

    def test_a_b2c_url_means_logged_out_immediately(self):
        page = self._Page([SIGNIN_URL])

        assert signin.resolve_session_state(page) is True
        assert page.waits == 0, "a settled verdict must not wait"

    def test_a_bare_myaccount_that_never_routes_is_treated_as_LOGGED_OUT(self):
        """THE regression. This exact URL read as signed-in and cost the whole recovery."""
        page = self._Page([self.BARE])

        assert signin.resolve_session_state(page, timeout_ms=3000, poll_ms=1000) is True

    def test_it_waits_for_a_slow_spa_rather_than_judging_the_first_frame(self):
        """The page legitimately starts bare and routes a moment later; judging immediately would
        sign in unnecessarily on every single run."""
        page = self._Page([self.BARE, self.BARE, self.SIGNED_IN])

        assert signin.resolve_session_state(page, timeout_ms=10000, poll_ms=1000) is False
        assert page.waits >= 2, "it must actually have waited for the route to appear"

    def test_it_waits_for_a_slow_redirect_to_b2c_too(self):
        page = self._Page([self.BARE, SIGNIN_URL])

        assert signin.resolve_session_state(page, timeout_ms=10000, poll_ms=1000) is True

    def test_an_unresolved_page_biases_to_logged_out_and_says_so(self, caplog):
        """Asymmetric costs: a wrong "logged out" costs one sign-in with credentials we already
        hold; a wrong "signed in" costs the entire recovery."""
        import logging

        page = self._Page([self.BARE])

        with caplog.at_level(logging.WARNING, logger=signin.__name__):
            assert signin.resolve_session_state(page, timeout_ms=2000, poll_ms=1000) is True

        assert "treating it as logged out" in caplog.text

    def test_the_one_shot_check_is_still_available_and_unchanged(self):
        """`looks_logged_out` stays for callers that only need the cheap 'is this the sign-in page?'
        answer; it is simply not enough to ACT on."""
        assert signin.looks_logged_out(self._Page([SIGNIN_URL])) is True
        assert signin.looks_logged_out(self._Page([self.SIGNED_IN])) is False


class TestASiteBlockIsNotAnAccountLock:
    """Costco serves "Costco.com Temporarily Unavailable" — an Akamai block/outage page — to an IP it
    does not like. That is about the PROXY, not the account.

    The ACCOUNT LOCKED branch used to match a bare `temporarily (unavailable|disabled)`, so a blocked
    proxy was reported as "ACCOUNT LOCKED — too many attempts" and told the operator to go clear a
    lock that did not exist. Observed live on a new profile whose proxy Costco was
    blocking. Wrong, alarming, and it points the fix at the wrong system entirely.
    """

    @staticmethod
    def _verdict(title="", errors=(), text=""):
        return signin._classify_signin_failure(
            {"errors": list(errors), "title": title, "text": text,
             "otp_field": False, "captcha": False})

    def test_costcos_block_page_is_named_as_a_SITE_problem(self):
        verdict, action = self._verdict(title="Costco.com Temporarily Unavailable")

        assert "SITE BLOCKED" in verdict
        assert "ACCOUNT LOCKED" not in verdict
        assert "proxy" in action, "the action must point at the IP, not the account"
        assert "do NOT touch the account" in action.lower() or "not touch the account" in action.lower()

    def test_a_real_account_lock_still_reads_as_one(self):
        assert "ACCOUNT LOCKED" in self._verdict(errors=["Your account has been locked"])[0]
        assert "ACCOUNT LOCKED" in self._verdict(text="too many failed sign-in attempts")[0]

    def test_the_site_block_is_checked_BEFORE_the_lock(self):
        """Order matters: a page could plausibly contain both phrasings, and the site-level
        explanation is the one that is actionable."""
        verdict, _ = self._verdict(title="Costco.com Temporarily Unavailable",
                                   text="too many attempts")

        assert "SITE BLOCKED" in verdict

    def test_other_akamai_block_wordings_are_covered_too(self):
        for text in ("Access Denied", "Request unsuccessful. Incapsula incident ID",
                     "We have detected unusual traffic"):
            assert "SITE BLOCKED" in self._verdict(text=text)[0], text
