"""Amazon Business's deterministic self-login, against a fake Playwright page.

Amazon Business was the one retailer that could not heal a lapsed session: it raised on sight of a
sign-in page and recorded nothing until a human re-authenticated. This module answers Amazon's authenticator challenge with a code generated on this
host, so the tests here pin the parts that fail SILENTLY or EXPENSIVELY:

  - typing into the wrong element while every log line still says "filled the email field",
  - un-ticking a box that arrived ticked,
  - submitting a code that is wrong (indistinguishable from a wrong password at the sign-in screen),
  - and handing the permanent TOTP seed to the paid agent, which puts credentials in its prompt.

The DOM modelled below is the one the live probe actually observed on 2026-08-25
(scripts/amazon_business_signin_probe.py), not the documented one — they differ, and the difference
is the whole reason the probe was run first.
"""

import pytest

from models.profile import RetailerAuth
from scrapers import amazon_signin as signin
from scrapers.totp import totp

SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"

SIGNIN_URL = "https://www.amazon.com/ap/signin?openid.return_to=%2Fab%2Fyour-orders%2Forders"
OTP_URL = "https://www.amazon.com/ap/mfa?arb=abc"
SIGNED_IN_URL = "https://www.amazon.com/ab/your-orders/orders"


class FakePage:
    """A page modelled as a set of present/visible selectors plus checkbox states.

    Selectors are matched as exact strings against the module's own constants, so a test that stops
    matching is telling you a selector changed — which is the point.
    """

    def __init__(self, *, visible=(), present=(), checked=None, url=SIGNIN_URL, on_submit=None,
                 errors=()):
        self.visible = set(visible)
        self.present = set(present) | set(visible)
        self.checked = dict(checked or {})
        self.url = url
        self.filled: dict[str, str] = {}
        self.clicked: list[str] = []
        self.checks: list[str] = []
        self.errors = list(errors)
        self._on_submit = on_submit

    # --- Playwright surface ---------------------------------------------------------------------
    def locator(self, selector):
        page = self

        class _Loc:
            first = property(lambda s: s)

            def count(self):
                return 1 if selector in page.present else 0

            def is_visible(self):
                return selector in page.visible

            def is_checked(self):
                return page.checked.get(selector, False)

            def check(self, timeout=None):
                page.checked[selector] = True
                page.checks.append(selector)

            def click(self, timeout=None):
                if selector not in page.present:
                    raise RuntimeError("no such element")
                page.clicked.append(selector)
                if page._on_submit:
                    page._on_submit(page, selector)

        return _Loc()

    def fill(self, selector, value):
        if selector not in self.visible:
            raise RuntimeError(f"{selector} is not visible")
        self.filled[selector] = value

    def press(self, selector, key):
        self.clicked.append(f"press:{selector}:{key}")

    def eval_on_selector(self, selector, expression):
        raise RuntimeError("dispatch not available in this fake")

    def wait_for_selector(self, selector, **kwargs):
        if selector not in self.present:
            raise RuntimeError("never appeared")

    def wait_for_url(self, matcher, timeout=None):
        pass

    def wait_for_timeout(self, ms):
        pass

    def on(self, event, handler):
        pass

    def evaluate(self, script):
        return {"url": self.url, "title": "Amazon Sign-In", "errors": self.errors,
                "password_field": signin.PASSWORD_SELECTORS[0] in self.present,
                "otp_field": signin.OTP_SELECTORS[0] in self.present,
                "captcha": False, "text": " ".join(self.errors)}


def _auth(secret=SECRET, **kw):
    return RetailerAuth(method="password", username="u@example.com", password="pw",
                        totp_secret=secret, **kw)


def _remembered_page(**kw):
    """The variant this account actually gets: no email box, password already on the landing page.

    `#ap-claim` is PRESENT but hidden — it holds the account email — and `#auth-remember-me` arrives
    ALREADY TICKED. Both were confirmed live.
    """
    return FakePage(
        visible={"#ap_password", "#signInSubmit", "#auth-remember-me"},
        present={"#ap-claim", "#ap_password", "#signInSubmit", "#auth-remember-me"},
        checked={"#auth-remember-me": True},
        **kw,
    )


class TestTheRememberedVariant:
    """Amazon served a ONE-SCREEN sign-in here, not the documented email -> Continue -> password
    flow. A login written only for the documented shape would fail on this account every time."""

    def test_the_password_is_filled_and_submitted_with_no_email_step(self):
        page = _remembered_page(on_submit=lambda p, sel: setattr(p, "url", SIGNED_IN_URL))

        outcome = signin.deterministic_login(page, _auth())

        assert outcome.ok is True
        assert page.filled == {"#ap_password": "pw"}
        assert "#signInSubmit" in page.clicked

    def test_the_hidden_email_input_is_never_typed_into(self):
        """`input[name=email]` on this page is the HIDDEN `#ap-claim`, and
        `#ap-credential-autofill-hint` is a VISIBLE text box that is not the email field either.
        Filling either one would look successful and sign nothing in."""
        page = _remembered_page(on_submit=lambda p, sel: setattr(p, "url", SIGNED_IN_URL))
        page.present.add("#ap-credential-autofill-hint")
        page.visible.add("#ap-credential-autofill-hint")

        signin.deterministic_login(page, _auth())

        assert "#ap-claim" not in page.filled
        assert "#ap-credential-autofill-hint" not in page.filled
        assert set(page.filled) == {"#ap_password"}


class TestTheFreshVariant:
    """Amazon reverts to asking for the email after a password reset, so both shapes must work."""

    def test_the_email_is_typed_then_continue_then_the_password(self):
        def reveal_password(page, selector):
            if selector in signin.CONTINUE_SELECTORS:
                page.visible.add("#ap_password")
                page.present.add("#ap_password")
            else:
                page.url = SIGNED_IN_URL

        page = FakePage(visible={"#ap_email", "#continue"},
                        present={"#ap_email", "#continue", "#signInSubmit"},
                        on_submit=reveal_password)
        page.visible.add("#signInSubmit")

        outcome = signin.deterministic_login(page, _auth())

        assert outcome.ok is True
        assert page.filled["#ap_email"] == "u@example.com"
        assert page.filled["#ap_password"] == "pw"


class TestKeepMeSignedIn:
    """Amazon ships "Keep me signed in" ALREADY TICKED (confirmed live). `click()` would turn it OFF
    and mint a session-scoped cookie — the exact mistake Best Buy's deterministic path made for ~16
    consecutive runs, and one that leaves every log line saying "signed in"."""

    def test_a_pre_ticked_box_is_left_ticked_and_never_clicked(self):
        page = _remembered_page(on_submit=lambda p, sel: setattr(p, "url", SIGNED_IN_URL))

        signin.deterministic_login(page, _auth())

        assert page.checked["#auth-remember-me"] is True
        assert "#auth-remember-me" not in page.clicked, "clicking a ticked box UN-ticks it"

    def test_an_unticked_box_is_ticked(self):
        page = _remembered_page(on_submit=lambda p, sel: setattr(p, "url", SIGNED_IN_URL))
        page.checked["#auth-remember-me"] = False

        signin.deterministic_login(page, _auth())

        assert page.checked["#auth-remember-me"] is True

    def test_a_missing_box_is_not_a_failure(self):
        """Best-effort: sign-in must proceed whether or not the control exists."""
        page = _remembered_page(on_submit=lambda p, sel: setattr(p, "url", SIGNED_IN_URL))
        page.present.discard("#auth-remember-me")
        page.visible.discard("#auth-remember-me")

        assert signin.deterministic_login(page, _auth()).ok is True


class TestTwoStepVerification:
    """The authenticator challenge is the EXPECTED path, not an exception — it is enrolled precisely
    because it is the one challenge answerable unattended."""

    @staticmethod
    def _otp_page(*, remember_present=True, remember_checked=False, has_field=True):
        present = {"#auth-signin-button"}
        visible = {"#auth-signin-button"}
        if has_field:
            present.add("#auth-mfa-otpcode")
            visible.add("#auth-mfa-otpcode")
        if remember_present:
            present.add("#auth-mfa-remember-device")
            visible.add("#auth-mfa-remember-device")

        def submit(page, selector):
            page.url = SIGNED_IN_URL
            page.present.discard("#auth-mfa-otpcode")
            page.visible.discard("#auth-mfa-otpcode")

        return FakePage(visible=visible, present=present,
                        checked={"#auth-mfa-remember-device": remember_checked},
                        url=OTP_URL, on_submit=submit)

    def test_the_generated_code_is_entered(self):
        page = self._otp_page()

        assert signin._answer_otp(page, _auth()) is True

        entered = page.filled["#auth-mfa-otpcode"]
        assert entered == totp(SECRET), "the code must come from the enrolled secret"
        assert len(entered) == 6 and entered.isdigit()

    def test_the_device_is_trusted_before_the_code_is_submitted(self):
        """Afterwards the screen is gone and the box with it, so order is the whole point: an
        untrusted device means a fresh code on every lapse."""
        page = self._otp_page(remember_checked=False)

        signin._answer_otp(page, _auth())

        assert page.checked["#auth-mfa-remember-device"] is True
        assert page.checks.index("#auth-mfa-remember-device") == 0
        assert page.clicked, "the code was still submitted"

    def test_an_already_ticked_trust_box_is_not_un_ticked(self):
        page = self._otp_page(remember_checked=True)

        signin._answer_otp(page, _auth())

        assert page.checked["#auth-mfa-remember-device"] is True

    def test_a_missing_trust_box_still_lets_the_code_through(self):
        """Losing the box costs a code next run; refusing to sign in costs the whole run."""
        page = self._otp_page(remember_present=False)

        assert signin._answer_otp(page, _auth()) is True

    def test_no_configured_secret_declines_instead_of_crashing(self):
        """The caller turns this into the usual "a human is needed" verdict. An exception here would
        escape as a page-shape error and spend the PAID agent on an auth failure."""
        page = self._otp_page()

        assert signin._answer_otp(page, _auth(secret="")) is False
        assert page.filled == {}, "nothing was submitted"

    def test_an_invalid_secret_declines_rather_than_sending_a_wrong_code(self):
        """A wrong code reads exactly like a wrong password at the sign-in screen, which is how a
        config typo turns into a password reset."""
        page = self._otp_page()

        assert signin._answer_otp(page, _auth(secret="not base32 !!")) is False
        assert page.filled == {}

    def test_a_missing_code_field_declines(self):
        assert signin._answer_otp(self._otp_page(has_field=False), _auth()) is False

    def test_a_stale_code_is_waited_out_rather_than_submitted(self, monkeypatch):
        """A code that expires mid-flight comes back as "invalid code" — indistinguishable from a
        wrong seed, and it sends you to reset a password that was never the problem.

        The wait covers the REMAINDER of the window (plus a little), rather than a fixed few
        seconds: what matters is landing in the next window, not how long we sit there.
        """
        waits = []
        monkeypatch.setattr(signin, "seconds_remaining", lambda: 1.0)
        page = self._otp_page()
        page.wait_for_timeout = lambda ms: waits.append(ms)

        signin._answer_otp(page, _auth())

        assert waits, "a code with almost no life left must not be submitted as-is"
        assert max(waits) >= 1000, "the wait must at least outlast the remaining window"


class TestTheLoginDeclinesSafely:
    def test_no_auth_block_attempts_nothing(self):
        page = _remembered_page()

        outcome = signin.deterministic_login(page, None)

        assert outcome.ok is False
        assert page.filled == {} and page.clicked == []
        # Nothing was attempted, so this is NOT a transport problem — mislabelling it would point
        # the alert at the network when the real answer is "no credentials are configured".
        assert outcome.transport_failed is False

    @pytest.mark.parametrize("auth", [
        RetailerAuth(method="password", username="", password="pw"),
        RetailerAuth(method="password", username="u@e.com", password=""),
    ])
    def test_incomplete_credentials_attempt_nothing(self, auth):
        page = _remembered_page()

        assert signin.deterministic_login(page, auth).ok is False
        assert page.filled == {}

    def test_staying_on_an_auth_url_after_submitting_is_reported_as_a_failure(self):
        """Everything clicked and filled and we are still on /ap/signin — the silent case."""
        page = _remembered_page(errors=["Your password is incorrect"])

        outcome = signin.deterministic_login(page, _auth())

        assert outcome.ok is False
        assert "BAD CREDENTIAL" in outcome.reason
        assert "config.json" in outcome.reason, "the action must travel with the verdict"


class TestSigninFailureVerdict:
    """Every failure ends as the same "submitted the password and we are still on an auth page", yet
    the fixes are opposite: a stale password is a config edit, an SMS challenge needs a human, and an
    anti-bot rejection needs backing OFF because retrying deepens it."""

    @staticmethod
    def _verdict(info, critical=()):
        return signin._classify_signin_failure(info, list(critical))[0]

    def test_a_wrong_password_is_named(self):
        assert "BAD CREDENTIAL" in self._verdict(
            {"errors": ["Your password is incorrect"], "url": SIGNIN_URL})

    def test_the_cvf_challenge_is_named_as_needing_a_human(self):
        verdict, action = signin._classify_signin_failure(
            {"errors": [], "url": "https://www.amazon.com/ap/cvf/verify?arb=1", "text": ""}, [])
        assert "OTP TO PHONE/EMAIL" in verdict
        assert "authenticator" in action.lower(), "the fix — enrol an app — must be offered"

    def test_a_locked_account_stops_retrying(self):
        assert "ACCOUNT LOCKED" in self._verdict(
            {"errors": ["Your account has been locked"], "url": SIGNIN_URL})

    def test_a_captcha_is_named(self):
        assert "CAPTCHA" in self._verdict(
            {"errors": [], "url": SIGNIN_URL, "text": "Enter the characters you see below"})

    def test_a_rejected_code_points_at_the_seed_and_the_clock(self):
        verdict, action = signin._classify_signin_failure(
            {"errors": ["Invalid code. Please try again."], "url": OTP_URL}, [])
        assert "2FA CODE REJECTED" in verdict
        assert "clock" in action, "TOTP is time-based; a drifting clock is the non-obvious cause"

    def test_network_failures_with_no_page_copy_read_as_anti_bot(self):
        critical = [{"url": "https://www.amazon.com/ap/signin", "error": "net::ERR_HTTP2_PROTOCOL_ERROR"}]
        assert "ANTI-BOT" in self._verdict({"errors": [], "url": SIGNIN_URL, "text": ""}, critical)

    def test_a_passkey_banner_is_not_mistaken_for_a_failure(self):
        """Observed live: the sign-in page carries a permanent passkey error banner while
        the password form beneath it works fine (Browser-Use's cloud browser has no WebAuthn). Read
        as evidence it would produce a confident WRONG verdict on every future failure."""
        info = {"errors": ["Passkey error. Sorry, your passkey isn't working. There might be a "
                           "problem with the server."],
                "url": SIGNIN_URL, "text": ""}

        assert "UNKNOWN" in self._verdict(info), "an honest UNKNOWN beats a confident wrong verdict"

    def test_a_real_error_still_wins_over_passkey_noise(self):
        info = {"errors": ["Sorry, your passkey isn't working.", "Your password is incorrect"],
                "url": SIGNIN_URL, "text": ""}

        assert "BAD CREDENTIAL" in self._verdict(info)

    def test_every_verdict_comes_with_an_action(self):
        for info, critical in (
            ({"errors": ["Your password is incorrect"], "url": SIGNIN_URL}, []),
            ({"errors": [], "url": "https://www.amazon.com/ap/cvf/verify"}, []),
            ({"errors": ["Your account has been locked"], "url": SIGNIN_URL}, []),
            ({"errors": [], "url": SIGNIN_URL, "text": "Enter the characters you see"}, []),
            ({"errors": [], "url": OTP_URL, "text": ""}, []),
            ({"errors": [], "url": SIGNIN_URL, "text": ""},
             [{"url": "/ap/signin", "error": "x"}]),
            ({"errors": [], "url": SIGNIN_URL, "text": ""}, []),
        ):
            _, action = signin._classify_signin_failure(info, critical)
            assert action and len(action) > 20, "a verdict without a next step is not actionable"


class TestTheSeedNeverReachesTheAgent:
    """A one-time code is derivable only from the seed, so handing the seed to an LLM and its cloud
    run history trades a 30-second secret for a permanent one. Browser-Use v4 has no secret-injection
    channel, so anything the agent is told IS in the prompt."""

    def test_neither_the_password_nor_the_seed_appears_in_the_agent_prompt(self):
        from models.profile import ProfileConfig
        from scrapers.amazon_business import AmazonBusinessScraper

        profile = ProfileConfig(label="profile-alpha", profile_id="x",
                                retailers=["amazon-business"],
                                auth={"amazon-business": _auth()})
        prompt = AmazonBusinessScraper(profile).task_prompt([], [])

        assert "pw" not in prompt.split(), "the password must not reach the task prompt"
        assert SECRET not in prompt, "the TOTP SEED is a permanent key — never give it to the agent"
        assert "do not attempt to log in" in prompt.lower(), (
            "the agent must still be told not to try signing in: it cannot pass 2FA without the "
            "seed, and it must not burn steps discovering that"
        )


class TestTheAccountSwitcher:
    """Amazon answers a logged-out order-history request with "Switch accounts" some of the time —
    no email box, no password box, one tile per remembered account (confirmed live).

    **Picking the wrong tile is the worst outcome available here, and it does not look like a
    failure**: the run signs in cleanly, scrapes a DIFFERENT Amazon account's orders, and writes them
    to the ledger under this profile. This account really does have a personal and a business
    identity under the same display name, which is the linking the separate-profile rule exists to
    prevent — so the match is on the configured username and nothing else.
    """

    class _SwitcherPage(FakePage):
        def __init__(self, tile_texts, *, add_account=True):
            present = {signin.ACCOUNT_SWITCHER_CONTAINER, signin.SWITCH_ACCOUNT_LINK}
            if add_account:
                present.add(signin.ADD_ACCOUNT_LINK)
            super().__init__(visible=set(present), present=present, url=SIGNIN_URL)
            self.tile_texts = list(tile_texts)
            self.switched_to = None

        def locator(self, selector):
            page = self
            if selector != signin.SWITCH_ACCOUNT_LINK:
                return super().locator(selector)

            class _Tiles:
                def count(self):
                    return len(page.tile_texts)

                def nth(self, i):
                    class _Tile:
                        def evaluate(self, script):
                            return page.tile_texts[i]

                        def click(self, timeout=None):
                            page.switched_to = i
                            page.clicked.append(f"tile:{i}")

                    return _Tile()

            return _Tiles()

    def test_the_tile_matching_the_configured_username_is_chosen(self):
        page = self._SwitcherPage([
            "Test Buyer\nsomeone.else@example.com",
            "Test Buyer\nTest Buyer\nu@example.com",   # same NAME, different email
        ])

        assert signin.handle_account_switcher(page, _auth()) == "switched"
        assert page.switched_to == 1, "matched on the email, not the display name or the position"

    def test_two_accounts_sharing_a_display_name_are_not_guessed_between(self):
        """The failure this guards is silent: either choice signs in fine and the ledger fills with
        the wrong account's orders."""
        page = self._SwitcherPage(["Test Buyer\nu@example.com", "Test Buyer\nu@example.com (alt)"])

        assert signin.handle_account_switcher(page, _auth()) == ""
        assert page.switched_to is None, "nothing may be clicked when the match is ambiguous"

    def test_no_matching_account_falls_back_to_add_account_not_to_a_stranger(self):
        """'Add account' leads to a fresh sign-in for the account we actually want. Clicking the only
        tile on offer would sign into someone else's."""
        page = self._SwitcherPage(["Someone Else\nsomeone.else@example.com"])

        assert signin.handle_account_switcher(page, _auth()) == "add_account"
        assert page.switched_to is None
        assert signin.ADD_ACCOUNT_LINK in page.clicked

    def test_an_ambiguous_switcher_fails_the_login_with_a_reason_that_explains_the_stakes(self):
        page = self._SwitcherPage(["Test Buyer\nu@example.com", "Test Buyer\nu@example.com (alt)"])

        outcome = signin.deterministic_login(page, _auth())

        assert outcome.ok is False
        assert "ACCOUNT SWITCHER" in outcome.reason
        assert "WRONG account" in outcome.reason, "the alert must say why nothing was clicked"

    def test_the_switcher_is_handled_then_the_flow_continues_to_the_password(self):
        """Amazon served the switcher on one run and the password form on the next, so the login
        walks whatever screens it is given rather than assuming an order."""
        page = self._SwitcherPage(["Test Buyer\nu@example.com"])

        def switch(i):
            # After switching, Amazon shows the password form for the chosen account.
            page.present.discard(signin.ACCOUNT_SWITCHER_CONTAINER)
            page.visible.discard(signin.ACCOUNT_SWITCHER_CONTAINER)
            page.present.update({"#ap_password", "#signInSubmit"})
            page.visible.update({"#ap_password", "#signInSubmit"})

        original = page.locator

        def locator(selector):
            loc = original(selector)
            if selector == signin.SWITCH_ACCOUNT_LINK:
                class _Wrapped:
                    def count(self):
                        return loc.count()

                    def nth(self, i):
                        tile = loc.nth(i)
                        real_click = tile.click

                        def click(timeout=None):
                            real_click(timeout=timeout)
                            switch(i)

                        tile.click = click
                        return tile

                return _Wrapped()
            return loc

        page.locator = locator
        page._on_submit = lambda p, sel: setattr(p, "url", SIGNED_IN_URL)

        outcome = signin.deterministic_login(page, _auth())

        assert outcome.ok is True
        assert page.filled == {"#ap_password": "pw"}, "it went on to sign in, not just switch"

    def test_a_step_is_never_repeated(self):
        """A screen that does not take must not be re-submitted in a loop — repeated password
        attempts are exactly what escalates an Amazon account to a lock."""
        page = self._SwitcherPage(["Test Buyer\nu@example.com"])

        outcome = signin.deterministic_login(page, _auth())

        assert outcome.ok is False
        assert page.clicked.count("tile:0") == 1, "the switcher was tried once, not in a loop"


class TestTheFreshSignInPageAmazonServesAfterAFullSignOut:
    """A FOURTH screen shape, observed 2026-08-25 after the account was signed out by hand.

    Two details here would each have failed the login on their own, and neither is guessable:
    the email box is `#ap_email_login` (not `#ap_email`), and `#continue` is a `<span>` WRAPPER
    around an id-less `input.a-button-input[type=submit]`.
    """

    @staticmethod
    def _fresh_page():
        def submit(page, selector):
            # Continue reveals the password screen; the password submit signs in.
            if selector in signin.CONTINUE_SELECTORS:
                page.present.update({"#ap_password", "#signInSubmit"})
                page.visible.update({"#ap_password", "#signInSubmit"})
            else:
                page.url = SIGNED_IN_URL

        # Only the real control exists — no `#continue` id anywhere, as observed live.
        return FakePage(visible={"#ap_email_login", "#continue input[type=submit]"},
                        present={"#ap_email_login", "#continue input[type=submit]"},
                        on_submit=submit)

    def test_the_email_field_is_found_under_its_other_id(self):
        page = self._fresh_page()

        assert signin.deterministic_login(page, _auth()).ok is True
        assert page.filled["#ap_email_login"] == "u@example.com"

    def test_continue_is_clicked_on_the_real_input_not_the_span_wrapper(self):
        page = self._fresh_page()

        signin.deterministic_login(page, _auth())

        assert "#continue input[type=submit]" in page.clicked
        assert page.filled["#ap_password"] == "pw", "it went on to the password screen"

    def test_a_hidden_password_input_does_not_short_circuit_the_email_step(self):
        """The fresh page pre-renders a hidden password input, so a presence-based check would skip
        straight to typing the password into a box nobody can see — and the email would never be
        entered at all."""
        page = self._fresh_page()
        page.present.add("#ap_password")          # present but NOT visible

        signin.deterministic_login(page, _auth())

        assert page.filled.get("#ap_email_login") == "u@example.com", "the email step must still run"


class TestTheCodeIsMintedLast:
    """REGRESSION, caught in production 2026-08-27 on the live server.

    A code was generated at the TOP of `_answer_otp` and only typed after `wait_for_selector` (up to
    20s) and the trusted-device tick. A TOTP window is 30 seconds, so on a slower host the code had
    rolled over before submission and Amazon answered "The code you entered is not valid" — which
    looks exactly like a wrong seed. It cost a diagnosis that ruled out the seed (hash-identical to a
    working machine) and the clock (+0.4s) before the ordering was suspected.

    So the invariant is ordering, and ordering is what these pin: the code must be minted AFTER every
    slow page interaction and immediately BEFORE it is typed.
    """

    class _RecordingPage(FakePage):
        """Records the order of page interactions and of code generation."""

        def __init__(self):
            super().__init__(
                visible={"#auth-mfa-otpcode", "#auth-mfa-remember-device", "#auth-signin-button"},
                present={"#auth-mfa-otpcode", "#auth-mfa-remember-device", "#auth-signin-button"},
                checked={"#auth-mfa-remember-device": False}, url=OTP_URL)
            self.events: list[str] = []
            # Submitting leaves the 2-step screen, as the real one does — otherwise _answer_otp
            # correctly reports failure and the ordering assertions never get reached.
            self._on_submit = lambda page, selector: (
                setattr(page, "url", SIGNED_IN_URL),
                page.present.discard("#auth-mfa-otpcode"),
                page.visible.discard("#auth-mfa-otpcode"),
            )

        def wait_for_selector(self, selector, **kwargs):
            self.events.append("wait_for_selector")

        def locator(self, selector):
            if selector == "#auth-mfa-remember-device":
                self.events.append("trust_box")
            return super().locator(selector)

        def fill(self, selector, value):
            self.events.append("fill")
            super().fill(selector, value)

    def test_the_code_is_generated_after_the_waiting_and_just_before_the_fill(self, monkeypatch):
        page = self._RecordingPage()
        real_totp = signin.totp

        def spy(secret, **kw):
            page.events.append("totp")
            return real_totp(secret, **kw)

        monkeypatch.setattr(signin, "totp", spy)

        assert signin._answer_otp(page, _auth()) is True

        # The seed is validated up front (one throwaway generation), so ignore that first call and
        # look at the one that produces the submitted code.
        assert page.events[-2:] == ["totp", "fill"], (
            f"the submitted code must be minted immediately before it is typed; got {page.events}")
        assert "wait_for_selector" in page.events[:-2]
        assert "trust_box" in page.events[:-2], "the slow steps must happen BEFORE the code exists"

    def test_an_invalid_seed_still_declines_before_touching_the_page(self):
        """Moving generation later must not lose the early bail-out: a malformed seed has to be
        caught before anything is typed, or a config typo becomes a submitted wrong code."""
        page = self._RecordingPage()

        assert signin._answer_otp(page, _auth(secret="not base32 !!")) is False
        assert page.filled == {} and "fill" not in page.events


def test_the_shared_module_never_names_one_retailer_in_a_log_line():
    """`amazon_signin` serves BOTH Amazon accounts, so a retailer name in its own logging is wrong
    half the time — and the worst offender was the VERDICT line, which is the first thing a human
    reads when diagnosing a failed sign-in. Live logs on 2026-08-25 show consumer `profile-bravo`
    runs announcing themselves as "Amazon Business", which would send a diagnosis to the wrong
    account. The caller's own line names the retailer; these must not.
    """
    import inspect
    import re

    source = inspect.getsource(signin)
    offenders = [
        line.strip()
        for line in source.splitlines()
        if re.search(r"log\.(info|warning|error)\(", line) and "Amazon Business" in line
    ]

    assert not offenders, f"shared logging must stay retailer-neutral; found: {offenders}"


class TestTheAlertsNameTheRightConfigBlock:
    """`amazon_signin` is shared by BOTH Amazon accounts, and its verdicts are ACTIONABLE — they tell
    a human which config key to edit. Six of them hardcoded `auth['amazon-business']`, so a consumer
    `profile-bravo` failure instructed the user to fix the Business profile: the wrong account, the
    wrong file section, and a change that could not possibly help.

    A log prefix naming the wrong retailer is confusing; instructions naming the wrong retailer are
    actively harmful, which is why this is pinned by behaviour rather than by a source grep.
    """

    CASES = (
        ({"errors": ["Your password is incorrect"], "url": SIGNIN_URL}, ()),
        ({"errors": [], "url": "https://www.amazon.com/ap/cvf/verify", "text": ""}, ()),
        ({"errors": ["Invalid code. Please try again."], "url": OTP_URL}, ()),
    )

    def test_consumer_verdicts_never_mention_the_business_config_key(self):
        for info, critical in self.CASES:
            _, action = signin._classify_signin_failure(info, list(critical), "amazon")
            assert "amazon-business" not in action, f"consumer verdict names the wrong block: {action}"

    def test_business_verdicts_name_the_business_config_key(self):
        for info, critical in self.CASES:
            _, action = signin._classify_signin_failure(info, list(critical), "amazon-business")
            if "auth[" in action:
                assert "auth['amazon-business']" in action, (
                    f"business verdict must name its own block: {action}")

    def test_the_two_step_failure_reason_names_the_callers_block(self):
        """The reason travels into ApiLoginError and out through the alert, so it is the line a human
        acts on when 2FA cannot be answered."""
        for key in ("amazon", "amazon-business"):
            page = FakePage(visible={"#ap_password", "#signInSubmit"},
                            present={"#ap_password", "#signInSubmit", "#auth-mfa-otpcode"})
            page._on_submit = lambda p, sel: setattr(p, "url", OTP_URL)

            outcome = signin.deterministic_login(page, _auth(secret=""), key)

            assert outcome.ok is False
            assert f"auth['{key}'].totp_secret" in outcome.reason, (
                f"the alert must name {key}'s own config block; got: {outcome.reason}")

    def test_a_missing_secret_warning_names_the_callers_block(self, caplog):
        import logging

        page = FakePage(visible={"#auth-mfa-otpcode", "#auth-signin-button"},
                        present={"#auth-mfa-otpcode", "#auth-signin-button"}, url=OTP_URL)

        with caplog.at_level(logging.WARNING, logger=signin.__name__):
            signin._answer_otp(page, _auth(secret=""), "amazon")

        assert "auth['amazon'].totp_secret" in caplog.text
        assert "amazon-business" not in caplog.text
