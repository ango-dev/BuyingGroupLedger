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
    assert bestbuy_api._deterministic_login(FakePage(), auth) is False
