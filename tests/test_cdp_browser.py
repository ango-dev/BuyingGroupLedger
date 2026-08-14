"""The cloud browser's create request — what we send to POST /browsers, and what we deliberately don't.

These are wire-format details. A wrong key name is accepted silently by our code and ignored by the
API, so the browser quietly keeps a default and nothing says so. Field names come from the SDK's
generated model and the v4 docs (profileId, customProxy, browserScreenWidth/Height, allowResizing).
"""

import pytest

from models.profile import ProfileConfig, ProxyConfig
from scrapers import cdp


class FakeHttp:
    def __init__(self):
        self.calls = []

    def request(self, method, path, json=None):
        self.calls.append((method, path, json))
        return {"id": "browser-1", "cdpUrl": "ws://fake"}


class FakePage:
    pass


class FakeContext:
    pages = [FakePage()]

    def route(self, pattern, handler):
        pass


class FakeBrowser:
    contexts = [FakeContext()]

    def close(self):
        pass


class FakePlaywright:
    class chromium:
        @staticmethod
        def connect_over_cdp(url):
            return FakeBrowser()

    def stop(self):
        pass


@pytest.fixture
def created(monkeypatch):
    """Open a real CdpBrowser against fakes and return the body it POSTed to /browsers.

    Drives the ACTUAL __enter__ rather than re-deriving the body here — a test that rebuilt the
    request the same way the code does would pass even if __enter__ stopped sending it.
    """
    http = FakeHttp()

    class FakeClient:
        _http = http

        def close(self):
            pass

    monkeypatch.setattr(cdp, "BrowserUseV2", lambda: FakeClient())
    monkeypatch.setattr(
        cdp, "sync_playwright",
        lambda: type("F", (), {"start": staticmethod(FakePlaywright)})(),
    )

    def open_browser(profile):
        with cdp.CdpBrowser(profile):
            pass
        method, path, body = http.calls[0]
        assert (method, path) == ("POST", "/browsers")
        return body

    return open_browser


def _profile(**kw):
    return ProfileConfig(label="p", profile_id="pid", retailers=["bestbuy"], **kw)


class TestProfileAndProxy:
    def test_the_profile_id_is_sent(self, created):
        assert created(_profile())["profileId"] == "pid"

    def test_the_proxy_is_passed_through(self, created):
        proxy = ProxyConfig(host="1.2.3.4", port=8080, username="u", password="p")

        assert created(_profile(proxy=proxy))["customProxy"] == {
            "host": "1.2.3.4", "port": 8080, "username": "u", "password": "p",
        }

    def test_no_proxy_key_when_none_configured(self, created):
        """An empty customProxy is not the same as omitting it — omitting keeps the account default."""
        assert "customProxy" not in created(_profile())

    def test_a_blank_profile_id_is_omitted_rather_than_sent_empty(self, created):
        body = created(ProfileConfig(label="p", profile_id="", retailers=["bestbuy"]))

        assert "profileId" not in body


class TestScreenSizeIsDeliberatelyUnset:
    """Measured live, and worth pinning so nobody 'fixes' it back.

    browserScreenWidth/Height are real, documented parameters (320-6144 x 320-3456) — but a
    profileId OVERRIDES them. Profile-less + 1024x768 gives screen 1024x768; profile + 1024x768,
    1366x768 and 1920x1080 ALL give 1536x864. Every session here uses a profile, so sending them
    would be config that silently does nothing, which is worse than no config at all.

    The default is also the right answer on its own terms: 1536x864 at devicePixelRatio 1.25 is
    exactly what a 1920x1080 display at 125% scaling reports — the commonest desktop setup there is,
    and these sessions sign into retailers that fingerprint aggressively.
    """

    def test_screen_size_is_not_sent(self, created):
        body = created(_profile())

        assert "browserScreenWidth" not in body
        assert "browserScreenHeight" not in body

    def test_allow_resizing_is_not_sent(self, created):
        """The v4 docs say enabling it 'reduces stealthiness', and we never resize mid-session."""
        assert "allowResizing" not in created(_profile())


class TestConnectFailureDoesNotLeakAPaidBrowser:
    """POST /browsers starts a cloud browser that BILLS. If connecting afterwards raises, `with` never
    opens, so __exit__ never runs and that browser is left running in the cloud — plus a started
    Playwright driver subprocess, which is the usual source of the "Task was destroyed but it is
    pending" records seen at interpreter shutdown on 2026-08-14.
    """

    @staticmethod
    def _wire(monkeypatch, fail_at):
        http = FakeHttp()
        closed = {"client": False}

        class FakeClient:
            _http = http

            def close(self):
                closed["client"] = True

        class Exploding:
            class chromium:
                @staticmethod
                def connect_over_cdp(url):
                    raise RuntimeError("cdp refused")

            def stop(self):
                closed["playwright"] = True

        monkeypatch.setattr(cdp, "BrowserUseV2", lambda: FakeClient())
        if fail_at == "start":
            def _boom():
                raise RuntimeError("driver would not start")
            monkeypatch.setattr(cdp, "sync_playwright",
                                lambda: type("F", (), {"start": staticmethod(_boom)})())
        else:
            monkeypatch.setattr(cdp, "sync_playwright",
                                lambda: type("F", (), {"start": staticmethod(Exploding)})())
        return http, closed

    @pytest.mark.parametrize("fail_at", ["start", "connect"])
    def test_the_cloud_browser_is_stopped_when_connecting_fails(self, monkeypatch, fail_at):
        http, closed = self._wire(monkeypatch, fail_at)

        with pytest.raises(RuntimeError):
            with cdp.CdpBrowser(_profile()):
                pytest.fail("the context body must never run")

        stops = [c for c in http.calls if c[0] == "PATCH" and c[2] == {"action": "stop"}]
        assert stops, "the paid cloud browser must be handed back, not left running"
        assert closed["client"], "the SDK client must be closed too"

    def test_the_original_error_still_propagates(self, monkeypatch):
        """Cleaning up must not swallow the failure — the caller has to see why it could not connect."""
        self._wire(monkeypatch, "connect")
        with pytest.raises(RuntimeError, match="cdp refused"):
            with cdp.CdpBrowser(_profile()):
                pass
