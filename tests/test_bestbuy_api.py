"""Offline tests for the pure flight-data parsing in scrapers/bestbuy_api.py (no browser).

Guards the reassembly + order-id/date extraction from Best Buy's Next.js purchase-history page, which
is the fragile part of the deterministic discovery step. The live browser mechanics are proven by a
real run instead.
"""

import json

import pytest

from models.profile import ProfileConfig, ProxyConfig, RetailerAuth
from scrapers import bestbuy_api
from scrapers.base import ApiLoginError
from scrapers.bestbuy_api import (
    PURCHASE_HISTORY_URL,
    _order_ids_and_dates,
    _reassemble_flight,
)


def _flight_html(*chunks: str) -> str:
    """Wrap raw flight-text chunks the way Next.js embeds them in the page."""
    return "".join(f"<script>self.__next_f.push([1,{json.dumps(c)}])</script>" for c in chunks)


def test_reassembles_chunks_in_order():
    html = _flight_html("hello ", "world")
    assert _reassemble_flight(html) == "hello world"


def test_extracts_order_ids_and_dates_from_flight():
    orders = {
        "purchaseHistoryOrdersExperience": {
            "openOrders": {
                "entries": [
                    # open entries are OpenOrderEntry objects keyed by "…-group_N" (native shipment)
                    [{"id": "BBY01-809900000006-group_1", "created": "2026-08-10T14:27:15-05:00"}],
                    [
                        {"id": "BBY01-809900000005-group_1", "created": "2026-08-10T10:29:13-05:00"},
                        {"id": "BBY01-809900000005-group_2", "created": "2026-08-10T10:29:13-05:00"},
                    ],
                ]
            },
            "closedOrdersAndTransactions": {
                "entries": [{"id": "BBY01-809900000001", "created": "2026-08-05T23:29:08-05:00"}]
            },
        }
    }
    html = _flight_html("2:", json.dumps(orders))
    dates = _order_ids_and_dates(html)
    assert dates == {
        "BBY01-809900000006": "2026-08-10",
        "BBY01-809900000005": "2026-08-10",  # group suffix stripped, one entry per bare order id
        "BBY01-809900000001": "2026-08-05",
    }


def test_falls_back_to_bare_ids_when_flight_key_absent():
    # No purchaseHistoryOrdersExperience object -> still discover ids from the raw HTML (dateless).
    html = "<div>order BBY01-999 and BBY01-888 and BBY01-999 again</div>"
    dates = _order_ids_and_dates(html)
    assert dates == {"BBY01-999": "", "BBY01-888": ""}


class _FakeHistoryPage:
    """A purchase-history page that can be logged in, logged out, or genuinely shape-changed.

    `signin_selectors` is the set of sign-in CTAs the page shows -- the thing that distinguishes a
    silently-expired session from a markup change once discovery has come back empty.
    """

    def __init__(self, html="", signin_selectors=(), url=PURCHASE_HISTORY_URL, on_goto=None):
        self.html = html
        self.signin_selectors = set(signin_selectors)
        self.url = url
        self._on_goto = on_goto
        self.goto_count = 0

    def goto(self, *a, **k):
        self.goto_count += 1
        if self._on_goto:
            self._on_goto(self)

    def wait_for_timeout(self, *a, **k):
        pass

    def evaluate(self, *a, **k):
        pass

    def content(self):
        return self.html

    def locator(self, selector):
        page = self

        class _Loc:
            def count(self):
                # Mirrors a real CSS list: any one of the comma-joined parts matching is a hit.
                return int(any(part.strip() in page.signin_selectors
                               for part in selector.split(",")))

        return _Loc()


def _client_with_page(page, monkeypatch, auth=True):
    """A BestBuyApiClient whose CDP session always yields `page`."""
    kwargs = {}
    if auth:
        kwargs["auth"] = {"bestbuy": RetailerAuth(method="password", username="u@e.com", password="pw")}
    profile = ProfileConfig(label="p", profile_id="x", retailers=["bestbuy"], **kwargs)

    class _FakeCdp:
        def __init__(self, _profile):
            pass

        def __enter__(self):
            return page

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(bestbuy_api, "CdpBrowser", _FakeCdp)
    return bestbuy_api.BestBuyApiClient(profile)


class TestEmptyDiscoveryIsNotAlwaysAShapeChange:
    """A silently-expired session and a real markup change look IDENTICAL once discovery returns
    nothing, and they have OPPOSITE correct responses -- which the old "(shape changed?)" hedge in the
    error message was quietly admitting.

    Live the logged-out purchase-history page tripped neither the URL markers nor the
    sign-in form selectors, so the `not dates` branch fired, the paid agent ran ($0.02), and the run
    still ended "session is logged out; skipping". That is money spent rediscovering what the
    deterministic path already had in hand -- and CLAUDE.md's rule is that the agent is never run for
    an auth failure, because it cannot fix one.
    """

    def test_a_logged_out_page_raises_a_login_error_not_a_shape_change(self, monkeypatch):
        page = _FakeHistoryPage(html="<html>Sign in to see your orders</html>",
                                signin_selectors={'a[href*="identity/signin"]'})
        client = _client_with_page(page, monkeypatch)
        # The session died without tripping the primary check, and signing in doesn't recover it.
        monkeypatch.setattr(bestbuy_api, "_looks_logged_out", lambda p: False)
        monkeypatch.setattr(bestbuy_api, "_deterministic_login",
                            lambda p, a: bestbuy_api.LoginOutcome(False))

        with pytest.raises(ApiLoginError):
            client.fetch_order_payloads("2026-08-01", set(), set())

    def test_a_genuinely_shape_changed_page_still_reaches_the_agent(self, monkeypatch):
        """The other half: no sign-in affordance means the markup really did change, and that IS what
        the paid agent is for. Narrowing the agent must not close this door."""
        page = _FakeHistoryPage(html="<html><div>totally new markup, no order ids</div></html>",
                                signin_selectors=())
        client = _client_with_page(page, monkeypatch)
        monkeypatch.setattr(bestbuy_api, "_looks_logged_out", lambda p: False)

        with pytest.raises(bestbuy_api.BestBuyApiError):
            client.fetch_order_payloads("2026-08-01", set(), set())

    def test_a_late_detected_logout_signs_in_and_recovers_the_run(self, monkeypatch):
        """Better than skipping: if the session lapsed without tripping the primary check, sign in
        HERE and re-discover, turning a lost run into a normal one."""
        orders = {"purchaseHistoryOrdersExperience": {"closedOrdersAndTransactions": {
            "entries": [{"id": "BBY01-809900000006", "created": "2026-08-12T10:00:00-05:00"}]}}}
        signed_in_html = _flight_html("2:", json.dumps(orders))

        page = _FakeHistoryPage(html="<html>Sign in</html>",
                                signin_selectors={'a[href*="/login"]'})
        client = _client_with_page(page, monkeypatch)
        monkeypatch.setattr(bestbuy_api, "_looks_logged_out", lambda p: False)

        def _login(p, a):
            # Signing in replaces the page: the orders render and the CTA disappears.
            p.html = signed_in_html
            p.signin_selectors = set()
            return bestbuy_api.LoginOutcome(True)

        monkeypatch.setattr(bestbuy_api, "_deterministic_login", _login)

        # No exception = the run recovered instead of skipping. (The in-page detail fetch returns
        # nothing from the fake, which the caller already tolerates.)
        client.fetch_order_payloads("2026-08-01", {"BBY01-809900000006"}, set())
        assert page.goto_count >= 2, "must re-load and re-parse after signing in, not reuse the empty read"

    def test_the_affordance_probe_ignores_a_dead_selector_engine(self):
        """A locator hiccup must not be read as 'no sign-in CTA' and send a logout to the agent."""
        class _Exploding:
            def locator(self, selector):
                raise RuntimeError("page closed")

        assert bestbuy_api._signin_affordances(_Exploding()) == []


class TestOffProxyLoginFallback:
    """The profile's static ISP proxy intermittently breaks HTTP/2 requests that carry a BODY, so the
    sign-in POSTs die (ERR_HTTP2_PROTOCOL_ERROR) while every GET — and the whole scrape — works.
    Diagnosed live by an A/B on the same flow: proxy on = still logged out; proxy off = 200
    and signed in. So a NETWORK-layer sign-in failure retries the sign-in off-proxy, then scrapes
    through the proxy as normal. See reference-isp-proxy-breaks-post.
    """

    @staticmethod
    def _client(with_proxy=True):
        auth = RetailerAuth(method="password", username="u@example.com", password="pw")
        profile = ProfileConfig(
            label="p", profile_id="x", retailers=["bestbuy"], auth={"bestbuy": auth},
            proxy=ProxyConfig(host="203.0.113.10", port=50100) if with_proxy else None,
        )
        return bestbuy_api.BestBuyApiClient(profile)

    def test_transport_failure_retries_the_login_off_proxy_then_scrapes(self, monkeypatch):
        client = self._client()
        calls = []

        def _fetch_once(profile, since, open_ids, terminal_ids, allow_login):
            calls.append(allow_login)
            if len(calls) == 1:
                raise bestbuy_api._AuthTransportError()
            return ["ROWS"]

        monkeypatch.setattr(client, "_fetch_once", _fetch_once)
        monkeypatch.setattr(client, "_login_without_proxy", lambda: True)

        assert client.fetch_order_payloads("2026-08-01", set(), set()) == ["ROWS"]
        # The retry must NOT try to log in again through the proxy — that is what just failed.
        assert calls == [True, False]

    def test_the_off_proxy_login_actually_strips_the_proxy(self, monkeypatch):
        """The crux of the fix: the retry session must carry no proxy, while the profile itself (and
        therefore every later scrape) keeps it."""
        client = self._client()
        seen = {}

        class _FakePage:
            def goto(self, *a, **k):
                pass

            def wait_for_timeout(self, *a, **k):
                pass

        class _FakeCdp:
            def __init__(self, profile):
                seen["profile"] = profile

            def __enter__(self):
                return _FakePage()

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(bestbuy_api, "CdpBrowser", _FakeCdp)
        monkeypatch.setattr(bestbuy_api, "_looks_logged_out", lambda page: False)  # already signed in

        assert client._login_without_proxy() is True
        assert seen["profile"].proxy is None, "the sign-in retry must bypass the broken proxy"
        assert client.profile.proxy is not None, "the profile keeps its proxy for the scrape"

    def test_a_page_shape_failure_does_not_spend_a_second_browser(self, monkeypatch):
        # transport_failed=False -> retrying off-proxy would fail identically, so don't try.
        client = self._client()
        monkeypatch.setattr(
            client, "_fetch_once",
            lambda *a, **k: (_ for _ in ()).throw(ApiLoginError("selector changed")))
        monkeypatch.setattr(
            client, "_login_without_proxy",
            lambda: pytest.fail("must not retry off-proxy for a non-transport failure"))

        with pytest.raises(ApiLoginError):
            client.fetch_order_payloads("2026-08-01", set(), set())

    def test_a_failed_off_proxy_login_reports_a_login_error(self, monkeypatch):
        # Still ApiLoginError, so scrape() alerts and skips WITHOUT running the paid agent.
        client = self._client()
        monkeypatch.setattr(
            client, "_fetch_once",
            lambda *a, **k: (_ for _ in ()).throw(bestbuy_api._AuthTransportError()))
        monkeypatch.setattr(client, "_login_without_proxy", lambda: False)

        with pytest.raises(ApiLoginError):
            client.fetch_order_payloads("2026-08-01", set(), set())

    def test_no_proxy_profile_has_nothing_to_bypass(self, monkeypatch):
        client = self._client(with_proxy=False)
        monkeypatch.setattr(
            bestbuy_api, "CdpBrowser",
            lambda profile: pytest.fail("must not open a browser when there is no proxy to bypass"))

        assert client._login_without_proxy() is False
