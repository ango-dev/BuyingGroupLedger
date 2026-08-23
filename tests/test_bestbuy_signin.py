"""Best Buy's Continue click, against a fake Playwright page.

Best Buy sessions die in ~20 minutes, so `_deterministic_login` runs on most scheduled runs. When it
fails the whole retailer is skipped for that run — and it can only signal that by returning False, so
what it LOGS on the way out is the only thing standing between a failure and a week of guessing.

The specific failure this covers was seen live (2026-08-14): the run reported "deterministic login
did not succeed" with no further detail, and the survey modal is the documented suspect — it renders
lazily, so dismissing it once at page load doesn't stop it appearing before the click.
"""

import logging

import pytest

from models.profile import RetailerAuth
from scrapers import bestbuy_api


class FakePage:
    """Minimal stand-in for a Playwright page.

    `covered` models the survey overlay intercepting clicks: while it is set, the two hit-tested
    strategies raise the way Playwright's actionability timeout does, but a dispatched `el.click()`
    still lands — which is exactly the real behaviour the third strategy exists for.
    """

    def __init__(self, *, covered=False, button=True, survey=False, dispatch_works=True):
        self.covered = covered
        self.button = button
        self.survey = survey
        self.dispatch_works = dispatch_works
        self.clicks: list[str] = []
        self.survey_removals = 0
        self.evaluated: list[str] = []
        self.handlers: dict[str, list] = {}

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    # --- the bits _dismiss_survey / diagnostics use ---
    def evaluate(self, script):
        self.evaluated.append(script)
        if "survey_window" in script and "remove" in script:
            self.survey_removals += 1
            self.survey = False
            self.covered = False       # removing the overlay un-blocks the button
            return None
        return {"url": "https://www.bestbuy.com/identity/signin", "title": "Sign in"}

    # --- click strategies ---
    def _hit_test(self, label):
        if not self.button:
            raise RuntimeError("no such element")
        if self.covered:
            raise TimeoutError("element intercepts pointer events")
        self.clicks.append(label)

    def locator(self, selector):
        page = self

        class _Loc:
            first = property(lambda s: s)

            def click(self, timeout=None):
                page._hit_test(f"css:{selector}")

            def scroll_into_view_if_needed(self, timeout=None):
                pass

        return _Loc()

    def get_by_role(self, role, name=None, exact=None):
        page = self

        class _Loc:
            first = property(lambda s: s)

            def click(self, timeout=None):
                page._hit_test(f"role:{name}")

        return _Loc()

    def eval_on_selector(self, selector, expression):
        if not self.button or not self.dispatch_works:
            raise RuntimeError("no such element")
        self.clicks.append(f"dispatch:{selector}")


class TestClickContinue:
    def test_the_plain_css_click_is_tried_first(self):
        page = FakePage()

        assert bestbuy_api._click_continue(page) is True
        assert page.clicks == ["css:button.cia-form__controls__submit"]

    def test_the_survey_is_dismissed_before_every_attempt_not_just_at_page_load(self):
        """The regression that matters: dismissing once on load is not enough.

        The modal renders lazily, so it can appear AFTER the page-load dismissal and overlay the
        button. Re-dismissing immediately before the click is what recovers it.
        """
        page = FakePage(covered=True, survey=True)

        assert bestbuy_api._click_continue(page) is True
        assert page.survey_removals >= 1
        # Dismissing cleared the overlay, so the very first strategy then succeeded.
        assert page.clicks == ["css:button.cia-form__controls__submit"]

    def test_a_dispatched_click_is_the_last_resort_when_something_still_covers_the_button(self):
        """Something we don't know about can cover the button; dispatching bypasses hit-testing."""
        page = FakePage(covered=True)
        page.evaluate = lambda script: None      # dismissal no longer clears the overlay

        assert bestbuy_api._click_continue(page) is True
        assert page.clicks == ["dispatch:button.cia-form__controls__submit"]

    def test_returns_false_when_the_button_is_simply_not_there(self):
        assert bestbuy_api._click_continue(FakePage(button=False)) is False

    def test_every_strategy_is_tried_before_giving_up(self):
        page = FakePage(button=False)
        bestbuy_api._click_continue(page)
        # One survey dismissal per attempt — three strategies.
        assert page.survey_removals == 3


class TestSigninDiagnostics:
    def test_it_logs_the_page_state_so_the_failure_is_diagnosable(self, caplog):
        page = FakePage()

        with caplog.at_level(logging.WARNING, logger=bestbuy_api.__name__):
            bestbuy_api._log_signin_diagnostics(page, "could not click Continue")

        assert "could not click Continue" in caplog.text
        assert "bestbuy.com/identity/signin" in caplog.text

    def test_unreadable_page_still_reports_the_original_failure(self, caplog):
        class Broken(FakePage):
            def evaluate(self, script):
                raise RuntimeError("page closed")

        with caplog.at_level(logging.WARNING, logger=bestbuy_api.__name__):
            bestbuy_api._log_signin_diagnostics(Broken(), "could not click Continue")

        # Diagnostics must never mask what actually went wrong.
        assert "could not click Continue" in caplog.text


@pytest.mark.parametrize("auth", [None])
def test_login_declines_without_password_auth(auth):
    outcome = bestbuy_api._deterministic_login(FakePage(), auth)
    assert outcome.ok is False
    # Nothing was attempted, so this is NOT a transport problem — it must not trigger the off-proxy
    # retry, which would spend a second cloud browser to fail the same way.
    assert outcome.transport_failed is False


class TestFailedRequestReporting:
    """Diagnosed live: every click and fill worked and sign-in STILL failed.

    `POST /identity/authenticate` died with ERR_HTTP2_PROTOCOL_ERROR while Best Buy's ThreatMetrix
    fingerprint script failed to tunnel. None of that is visible from the DOM — the page only says
    "Failed to fetch" — so a run reporting just "login did not succeed" sends you hunting through
    selectors for a problem that lives in the network.
    """

    class _Req:
        def __init__(self, url, resource_type, failure):
            self.url = url
            self.resource_type = resource_type
            self.failure = failure

    def test_auth_critical_failures_are_called_out_as_not_a_page_shape_problem(self, caplog):
        page = FakePage()
        failed = bestbuy_api._watch_failed_requests(page)
        for handler in page.handlers["requestfailed"]:
            handler(self._Req("https://www.bestbuy.com/identity/authenticate", "fetch",
                              "net::ERR_HTTP2_PROTOCOL_ERROR"))
            handler(self._Req("https://tmx.bestbuy.com/abc.js", "script",
                              "net::ERR_TUNNEL_CONNECTION_FAILED"))

        with caplog.at_level(logging.WARNING, logger=bestbuy_api.__name__):
            bestbuy_api._log_signin_diagnostics(page, "stayed logged out", failed)

        assert "auth-critical" in caplog.text
        assert "identity/authenticate" in caplog.text
        # The operative conclusion: don't go looking at selectors, and the agent won't help either.
        assert "NOT a page-shape one" in caplog.text

    def test_incidental_failures_are_not_dressed_up_as_auth_failures(self, caplog):
        page = FakePage()
        failed = bestbuy_api._watch_failed_requests(page)
        for handler in page.handlers["requestfailed"]:
            handler(self._Req("https://www.googletagmanager.com/gtag/js", "script",
                              "net::ERR_TUNNEL_CONNECTION_FAILED"))

        with caplog.at_level(logging.WARNING, logger=bestbuy_api.__name__):
            bestbuy_api._log_signin_diagnostics(page, "stayed logged out", failed)

        assert "none auth-critical" in caplog.text

    def test_a_page_without_event_support_still_works(self):
        class NoEvents(FakePage):
            def on(self, event, handler):
                raise RuntimeError("unsupported")

        # Telemetry must never be what breaks a login.
        assert bestbuy_api._watch_failed_requests(NoEvents()) == []


class TestKeepMeSignedIn:
    """Best Buy was logged out on ~16 consecutive scheduled runs. The accepted explanation was that
    its sessions die in ~20-25 minutes against a 3-hourly schedule -- but the AGENT prompt has always
    said to leave "Keep me signed in" checked, while the deterministic login that replaced it as the
    primary path never touched the box. If it governs persistent-token vs session-cookie, the
    deterministic path has been minting the short-lived kind on every run.
    """

    class _Box:
        def __init__(self, present=True, checked=False):
            self.present, self.checked, self.check_calls = present, checked, 0

        def count(self):
            return 1 if self.present else 0

        def is_checked(self):
            return self.checked

        def check(self, timeout=None):
            self.check_calls += 1
            self.checked = True

    class _Page:
        def __init__(self, box):
            self._box = box

        def locator(self, selector):
            page = self

            class _L:
                first = None

                def __init__(self):
                    pass

                def count(self):
                    return page._box.count()

            loc = _L()
            loc.first = page._box
            return loc

    def test_an_unticked_box_is_ticked(self):
        box = self._Box(checked=False)
        assert bestbuy_api._keep_signed_in(self._Page(box)) is True
        assert box.checked and box.check_calls == 1

    def test_an_already_ticked_box_is_left_alone(self):
        """check() not click(), so a box Best Buy already defaults to checked is never toggled OFF."""
        box = self._Box(checked=True)
        assert bestbuy_api._keep_signed_in(self._Page(box)) is True
        assert box.check_calls == 0 and box.checked

    def test_a_missing_control_is_not_a_failure(self):
        """Best-effort: sign-in must proceed whether or not the box exists."""
        assert bestbuy_api._keep_signed_in(self._Page(self._Box(present=False))) is False

    def test_it_runs_before_continue_is_clicked(self, monkeypatch):
        """Ticking it AFTER submitting would be useless -- order is the whole point."""
        order = []
        monkeypatch.setattr(bestbuy_api, "_keep_signed_in",
                            lambda p: order.append("keep") or True)
        monkeypatch.setattr(bestbuy_api, "_click_continue",
                            lambda p: order.append("continue") or False)
        monkeypatch.setattr(bestbuy_api, "_watch_failed_requests", lambda p: [])
        monkeypatch.setattr(bestbuy_api, "_dismiss_survey", lambda p: None)
        monkeypatch.setattr(bestbuy_api, "_log_signin_diagnostics", lambda *a, **k: None)

        class _P:
            def wait_for_selector(self, *a, **k):
                pass

            def fill(self, *a, **k):
                pass

        auth = RetailerAuth(method="password", username="u@e.com", password="pw")
        bestbuy_api._deterministic_login(_P(), auth)
        assert order == ["keep", "continue"]


class TestSigninFailureVerdict:
    """Every sign-in failure ends as the same "submitted the password but stayed logged out" line, yet
    the fixes are opposite: a stale password is a config edit, an identity challenge needs a human, and
    an anti-bot rejection needs backing OFF (retrying deepens it). On 2026-08-23 a simply-wrong password
    was misread as the anti-bot transport failure for days, so the verdict is now derived from Best
    Buy's own on-page copy.
    """

    @staticmethod
    def _verdict(info, critical=()):
        return bestbuy_api._classify_signin_failure(info, list(critical))[0]

    def test_the_page_saying_the_password_is_wrong_names_a_bad_credential(self):
        info = {"errors": ["The password you've entered is incorrect."],
                "url": "https://www.bestbuy.com/identity/signin/options"}
        assert "BAD CREDENTIAL" in self._verdict(info)

    def test_bad_credential_wins_over_incidental_network_noise(self):
        """The banner is direct evidence; a failed request is circumstantial. Getting this backwards is
        exactly the misdiagnosis that cost days — tmx/analytics failures are near-permanent."""
        info = {"errors": ["The password you've entered is incorrect."], "url": "…/signin/options"}
        critical = [{"url": "https://tmx.bestbuy.com/x.js", "error": "net::ERR_TUNNEL_CONNECTION_FAILED"}]
        assert "BAD CREDENTIAL" in self._verdict(info, critical)

    def test_verify_ownership_is_named_but_NOT_treated_as_proof_the_password_is_good(self):
        """This screen has two causes with opposite fixes: genuine 2FA, OR Best Buy
        escalating after too many failed password attempts. So the verdict must name the challenge
        without certifying the credential, and must point at BOTH causes."""
        info = {"errors": [], "title": "Sign In - Verify Your Identity - Best Buy",
                "url": "https://www.bestbuy.com/identity/signin/verifyOwnership?token=tid%3Aabc"}
        verdict, action = bestbuy_api._classify_signin_failure(info, [])
        assert "IDENTITY VERIFICATION" in verdict
        assert "ACCEPTED" not in verdict, "must not claim the password was accepted"
        assert "2FA" in action, "the legitimate-2FA cause must be offered"
        assert "BAD CREDENTIAL" in action, "the failed-password escalation must be offered too"

    def test_a_one_time_code_prompt_is_not_confused_with_a_bad_password(self):
        info = {"errors": [], "url": "…/signin", "text": "We sent a code to your phone ending in 1234"}
        assert "ONE-TIME CODE" in self._verdict(info)

    def test_a_locked_account_is_called_out_so_retrying_stops(self):
        info = {"errors": ["Your account has been locked due to too many failed attempts."],
                "url": "…/signin"}
        assert "ACCOUNT LOCKED" in self._verdict(info)

    def test_auth_critical_failures_with_no_page_copy_read_as_anti_bot(self):
        info = {"errors": [], "url": "…/signin/options", "text": ""}
        critical = [{"url": "https://www.bestbuy.com/identity/authenticate",
                     "error": "net::ERR_HTTP2_PROTOCOL_ERROR"}]
        assert "ANTI-BOT" in self._verdict(info, critical)

    def test_no_evidence_at_all_admits_it_is_unknown(self):
        # Better an honest UNKNOWN pointing at the DOM than a confident wrong verdict.
        assert "UNKNOWN" in self._verdict({"errors": [], "url": "…/signin", "text": ""})

    def test_every_verdict_comes_with_an_action(self):
        for info, crit in (
            ({"errors": ["The password you've entered is incorrect."], "url": "u"}, []),
            ({"errors": [], "url": "…/verifyOwnership"}, []),
            ({"errors": [], "url": "u", "text": ""}, [{"url": "identity/authenticate", "error": "x"}]),
            ({"errors": [], "url": "u", "text": ""}, []),
        ):
            _, action = bestbuy_api._classify_signin_failure(info, crit)
            assert action and len(action) > 20, "a verdict without a next step is not actionable"
