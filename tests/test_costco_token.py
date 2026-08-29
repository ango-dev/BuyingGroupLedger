"""The Costco token grab's failure branches, against a fake cloud browser.

Only the parts that used to leave a dossier EMPTY are pinned here. `_grab_refresh_token` runs inside
the scrape's open dossier (costco.py's ApiLoginError branch), but every failure inside `_one_pass` is
either reported (`LoginOutcome`) or swallowed (`except Exception: log.warning`), so `CdpBrowser`
always exits cleanly and its on-exception snapshot never fires. These tests assert the page is
captured anyway. Offline: no network, no credentials.
"""

import diagnostics
from models.profile import ProfileConfig, RetailerAuth
from scrapers import costco_signin
from scripts import costco_token

SIGNIN_URL = "https://signin.costco.com/e0714dd4/oauth2/v2.0/authorize?client_id=4900eb1f"


class _Context:
    def route(self, *a, **k):
        pass

    def storage_state(self):
        return {"origins": []}


class _Page:
    """Permissive: every navigation succeeds, every storage read is empty, nothing is captured."""

    def __init__(self):
        self.url = SIGNIN_URL
        self.context = _Context()

    def on(self, *a, **k):
        pass

    def goto(self, url, **k):
        self.url = url

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, js):
        return {}

    def locator(self, selector):
        class _Loc:
            def count(self):
                return 0
        return _Loc()

    def title(self):
        return "Sign In | Costco"

    def content(self):
        return "<html><body><form id='localAccountForm'></form></body></html>"

    def screenshot(self, path, **k):
        open(path, "wb").write(b"\x89PNG")


class _Browser:
    opened = 0

    def __init__(self, profile):
        self.page = _Page()

    def __enter__(self):
        _Browser.opened += 1
        return self.page

    def __exit__(self, *exc):
        return False


def _wire(monkeypatch, profile):
    import config.profiles
    import scrapers.cdp

    monkeypatch.setattr(config.profiles, "load_profiles", lambda: [profile])
    monkeypatch.setattr(scrapers.cdp, "CdpBrowser", _Browser)
    # Land on the sign-in page, settled, on every pass.
    monkeypatch.setattr(costco_signin, "resolve_session_state", lambda page, **k: True)
    _Browser.opened = 0


def test_a_logged_out_profile_with_no_auth_block_still_captures_the_page(monkeypatch, tmp_path):
    _wire(monkeypatch, ProfileConfig(label="p", profile_id="x"))

    with diagnostics.collecting("costco", "p", root=tmp_path, selectors=costco_signin.SELECTORS) as d:
        token = costco_token._grab_refresh_token("p")

    assert token is None
    assert _Browser.opened == 2, "pass 1 signs in, pass 2 opens a fresh browser"
    labels = [s["label"] for s in d.snapshots]
    assert "Costco token grab: logged out, no auth block" in labels
    assert "Costco token grab: pass 2 landed logged out" in labels
    assert any("no auth['costco'] block" in p for p in d.problems)


def test_a_self_login_that_raises_is_captured_rather_than_only_logged(monkeypatch, tmp_path):
    profile = ProfileConfig(label="p", profile_id="x",
                            auth={"costco": RetailerAuth(method="password", username="u@x.com",
                                                         password="pw")})
    _wire(monkeypatch, profile)

    def boom(page, auth):
        raise RuntimeError("#signInName detached")

    monkeypatch.setattr(costco_signin, "deterministic_login", boom)

    with diagnostics.collecting("costco", "p", root=tmp_path, selectors=costco_signin.SELECTORS) as d:
        assert costco_token._grab_refresh_token("p") is None

    labels = [s["label"] for s in d.snapshots]
    assert "Costco token grab: self-login raised RuntimeError" in labels
    assert any("self-login raised RuntimeError: #signInName detached" in p for p in d.problems)
    # The selector audit ran against the captured page, naming the sign-in selectors.
    audited = {row["name"] for row in d.snapshots[0]["audit"]}
    assert "signin_email" in audited


def test_the_grab_outside_a_dossier_is_unchanged(monkeypatch):
    _wire(monkeypatch, ProfileConfig(label="p", profile_id="x"))
    assert diagnostics.current() is None
    assert costco_token._grab_refresh_token("p") is None
