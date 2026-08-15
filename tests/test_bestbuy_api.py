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


class TestNoOffProxySignInRetry:
    """An off-proxy sign-in retry was added 2026-08-14 and REMOVED 2026-08-15 after it fired for real.

    The theory was that the static ISP proxy broke the HTTP/2 auth POSTs (ERR_HTTP2_PROTOCOL_ERROR on
    /identity/authenticate while every GET sailed through). When it finally fired, it failed
    IDENTICALLY off-proxy -- same error, same endpoint. So the rejection travels with the BROWSER
    (TLS/HTTP2 fingerprint, or the Browser-Use cloud range), not the egress IP, and the retry bought
    nothing while roughly doubling sign-in wall-clock (5m26s vs a normal ~2m50s).

    These tests exist so the idea is not re-introduced from first principles: it is an appealing theory
    that the evidence refutes. The same result also rules out proxy ROTATION as a fix.
    """

    @staticmethod
    def _client():
        auth = RetailerAuth(method="password", username="u@example.com", password="pw")
        return bestbuy_api.BestBuyApiClient(ProfileConfig(
            label="p", profile_id="x", retailers=["bestbuy"], auth={"bestbuy": auth},
            proxy=ProxyConfig(host="203.0.113.10", port=50100)))

    def test_a_failed_sign_in_opens_exactly_one_browser(self, monkeypatch):
        """The retry's real cost: a second full CDP session on the runs least able to afford it."""
        opened = []

        class _FakeCdp:
            def __init__(self, profile):
                opened.append(profile)

            def __enter__(self):
                raise ApiLoginError("logged out and login failed")

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(bestbuy_api, "CdpBrowser", _FakeCdp)
        with pytest.raises(ApiLoginError):
            self._client().fetch_order_payloads("2026-08-01", set(), set())
        assert len(opened) == 1, "a failed sign-in must not open a second browser to try again"

    def test_the_off_proxy_plumbing_is_gone(self):
        """Deleted, not just unused -- dead auth plumbing invites revival."""
        assert not hasattr(bestbuy_api.BestBuyApiClient, "_login_without_proxy")
        assert not hasattr(bestbuy_api, "_AuthTransportError")

    def test_a_network_layer_failure_still_says_so(self, monkeypatch):
        """The diagnostic value of c9e7490 is KEPT: transport_failed no longer drives a retry, but it
        still tells whoever reads the alert which problem they have. A network-layer rejection is not
        fixable by changing egress; a page-flow failure is a different job entirely."""
        page = _FakeHistoryPage(html="<html>Sign in</html>", signin_selectors={"#fld-e"})
        client = _client_with_page(page, monkeypatch)
        monkeypatch.setattr(bestbuy_api, "_looks_logged_out", lambda p: True)
        monkeypatch.setattr(bestbuy_api, "_deterministic_login",
                            lambda p, a: bestbuy_api.LoginOutcome(False, transport_failed=True))

        with pytest.raises(ApiLoginError, match="NETWORK layer"):
            client.fetch_order_payloads("2026-08-01", set(), set())
