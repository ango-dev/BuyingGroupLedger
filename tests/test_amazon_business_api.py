"""Offline tests for the AmazonBusinessApiClient orchestration (no real browser).

The HTML parsing is covered by tests/test_amazon_business_mapping.py; here a fake CDP page drives
`fetch_order_items` end-to-end to lock in the browser-independent logic: which orders get fetched
(window + open + terminal), the CLICK-THROUGH pagination (business paginates client-side, not by
startIndex URL), the pt-page tracking-number hop promoting a shipment to 'shipped', and the final date
keep-filter.
"""

import pytest

import scrapers.amazon_business_api as api
import scrapers.amazon_business_signin as signin
from models.profile import RetailerAuth
from scrapers.amazon_business_api import AmazonBusinessApiClient
from tests.test_amazon_business_mapping import _details, _history, _item, _order_card, _shipment


class _FakeLocator:
    """Locator for the logged-out probe (#ap_email...) — count 0 = logged in."""

    def count(self):
        return 0


class _NextControl:
    """The pagination 'next' control (li.a-last a). Clicking it advances the fake page to the next
    history page; count() reflects whether a next page exists."""

    def __init__(self, page):
        self._page = page

    def count(self):
        return 1 if self._page._idx < len(self._page._pages) - 1 else 0

    @property
    def first(self):
        return self

    def click(self, **kwargs):
        if self._page._idx < len(self._page._pages) - 1:
            self._page._idx += 1


class _FakePage:
    """Serves paginated order-history (advanced by clicking) plus order-details / pt HTML by URL."""

    def __init__(self, pages, details_by_id, logged_out=False):
        self.url = ""
        self._pages = pages  # list of order-history HTML strings, newest-first
        self._details = details_by_id
        self._current = ""
        self._idx = 0
        self._logged_out = logged_out

    def goto(self, url, **kwargs):
        self.url = url
        self._current = url
        if "your-orders/orders" in url:
            self._idx = 0  # a fresh load starts at page 1

    def content(self):
        if "your-orders/orders" in self._current or "order-history" in self._current:
            return self._pages[self._idx]
        for oid, html in self._details.items():
            if f"orderID={oid}" in self._current:
                return html
        return "<html></html>"

    def wait_for_timeout(self, *a):
        pass

    def evaluate(self, *a, **k):
        return None

    def locator(self, selector, *a, **k):
        if "a-last" in selector:
            return _NextControl(self)
        if self._logged_out:
            class _L:
                def count(self):
                    return 1
            return _L()
        return _FakeLocator()


class _FakeCdp:
    def __init__(self, page):
        self._page = page

    def __call__(self, profile):
        return self

    def __enter__(self):
        return self._page

    def __exit__(self, *exc):
        return False


class _Profile:
    """A real ProfileConfig always has an `auth` dict (empty when nothing is configured), and the
    client reads it to decide whether a lapsed session can sign itself back in."""

    label = "profile-alpha"

    def __init__(self, auth=None):
        self.auth = auth or {}


def _install_fake(monkeypatch, pages, details_by_id, logged_out=False):
    page = _FakePage(pages, details_by_id, logged_out=logged_out)
    monkeypatch.setattr(api, "CdpBrowser", _FakeCdp(page))
    return page


def _deliv(oid, date="August 9, 2026"):
    return _details(oid, date, [_shipment(oid, 0, "Delivered August 9", [_item("Thing", "$5.00")])])


def test_fetches_only_in_window_and_open_orders(monkeypatch):
    new_oid, old_oid, open_oid = "111-1111111-1111111", "222-2222222-2222222", "333-3333333-3333333"
    page1 = _history(
        _order_card(new_oid, "August 9, 2026"),   # in window -> fetch
        _order_card(old_oid, "July 1, 2026"),      # out of window, not open -> skip
        _order_card(open_oid, "July 1, 2026"),     # out of window but still open -> fetch
    )
    details = {
        new_oid: _deliv(new_oid, "August 9, 2026"),
        old_oid: _deliv(old_oid, "July 1, 2026"),
        open_oid: _deliv(open_oid, "July 1, 2026"),
    }
    _install_fake(monkeypatch, [page1], details)

    client = AmazonBusinessApiClient(_Profile())
    rows = client.fetch_order_items("2026-08-08", open_ids={open_oid}, terminal_ids=set(), today="2026-08-10")
    assert {r.order_id for r in rows} == {new_oid, open_oid}


def test_terminal_orders_are_never_fetched(monkeypatch):
    oid = "444-4444444-4444444"
    _install_fake(monkeypatch, [_history(_order_card(oid, "August 9, 2026"))], {oid: _deliv(oid)})
    client = AmazonBusinessApiClient(_Profile())
    rows = client.fetch_order_items("2026-08-08", open_ids=set(), terminal_ids={oid}, today="2026-08-10")
    assert rows == []


def test_tracking_hop_promotes_open_shipment_to_shipped(monkeypatch):
    oid = "555-5555555-5555555"
    details = {oid: _details(oid, "August 9, 2026",
                             [_shipment(oid, 0, "Arriving Monday", [_item("Thing", "$5.00")], shipment_id="SID")])}
    _install_fake(monkeypatch, [_history(_order_card(oid, "August 9, 2026"))], details)

    reader = lambda page: {"status": "shipped", "tracking_number": "TBA42", "delivery_promise": "Arriving"}
    client = AmazonBusinessApiClient(_Profile(), tracking_reader=reader)
    rows = client.fetch_order_items("2026-08-08", open_ids=set(), terminal_ids=set(), today="2026-08-10")
    assert len(rows) == 1
    assert rows[0].status == "shipped"
    assert rows[0].tracking_number == "TBA42"


def test_no_tracking_reader_leaves_open_shipment_ordered(monkeypatch):
    oid = "666-6666666-6666666"
    details = {oid: _details(oid, "August 9, 2026",
                             [_shipment(oid, 0, "Arriving Monday", [_item("Thing", "$5.00")])])}
    _install_fake(monkeypatch, [_history(_order_card(oid, "August 9, 2026"))], details)

    client = AmazonBusinessApiClient(_Profile())  # no tracking_reader
    rows = client.fetch_order_items("2026-08-08", open_ids=set(), terminal_ids=set(), today="2026-08-10")
    assert rows[0].status == "ordered"
    assert rows[0].tracking_number == ""


def test_discovery_click_throughs_pages(monkeypatch):
    """Business paginates by clicking 'next', not by startIndex URL — page-2 orders must not be missed."""
    p1 = ("111-1111111-1111111", "222-2222222-2222222")   # page 1
    p2 = ("333-3333333-3333333",)                           # page 2 (reached by clicking next)
    page1 = _history(*[_order_card(oid, "August 9, 2026") for oid in p1])
    page2 = _history(*[_order_card(oid, "August 8, 2026") for oid in p2])
    all_ids = p1 + p2
    details = {oid: _deliv(oid) for oid in all_ids}
    _install_fake(monkeypatch, [page1, page2], details)

    client = AmazonBusinessApiClient(_Profile())
    rows = client.fetch_order_items("2026-08-01", open_ids=set(), terminal_ids=set(), today="2026-08-10")
    assert {r.order_id for r in rows} == set(all_ids)  # order from page 2 not missed


def test_discovery_stops_when_page_all_older_than_window(monkeypatch):
    """A page entirely older than the window stops pagination (newest-first), so we don't click forever
    and don't fetch out-of-window orders."""
    in_win = "111-1111111-1111111"
    old1, old2 = "222-2222222-2222222", "333-3333333-3333333"
    page1 = _history(_order_card(in_win, "August 9, 2026"), _order_card(old1, "June 1, 2026"))
    page2 = _history(_order_card(old2, "May 1, 2026"))
    details = {oid: _deliv(oid, d) for oid, d in
               [(in_win, "August 9, 2026"), (old1, "June 1, 2026"), (old2, "May 1, 2026")]}
    _install_fake(monkeypatch, [page1, page2], details)

    client = AmazonBusinessApiClient(_Profile())
    rows = client.fetch_order_items("2026-08-08", open_ids=set(), terminal_ids=set(), today="2026-08-10")
    # Only the in-window order is kept; page 1 was already all-past-the-window after the first row, but
    # even if page 2 is visited its old order is filtered out by the date keep-filter.
    assert {r.order_id for r in rows} == {in_win}


def test_logged_out_with_no_auth_block_raises_and_says_what_to_do(monkeypatch):
    """A profile that never opted in behaves exactly as it did before self-login existed.

    This is the guard that keeps the feature from changing anything for a profile with no credentials
    configured: it must still be ApiLoginError (which alerts and SKIPS), never a fall-through to the
    paid agent, and never a crash.
    """
    _install_fake(monkeypatch, ["<html></html>"], {}, logged_out=True)
    client = AmazonBusinessApiClient(_Profile())
    with pytest.raises(api.ApiLoginError, match="no auth block"):
        client.fetch_order_items("2026-08-08", set(), set(), today="2026-08-10")


def test_a_lapsed_session_signs_itself_back_in_and_the_run_continues(monkeypatch):
    """The whole point: a logged-out Amazon Business run used to record NOTHING until a human
    intervened. With credentials configured it heals itself and scrapes normally."""
    oid = "111-1111111-1111111"
    page = _install_fake(monkeypatch, [_history(_order_card(oid, "August 9, 2026"))],
                         {oid: _deliv(oid, "August 9, 2026")}, logged_out=True)

    calls = []

    def _fake_login(p, auth):
        calls.append(auth)
        p._logged_out = False           # the session is now live, as a real sign-in would leave it
        return signin.LoginOutcome(True)

    monkeypatch.setattr(api, "deterministic_login", _fake_login)

    auth = RetailerAuth(method="password", username="u@e.com", password="pw", totp_secret="")
    client = AmazonBusinessApiClient(_Profile(auth={"amazon-business": auth}))
    rows = client.fetch_order_items("2026-08-08", set(), set(), today="2026-08-10")

    assert len(calls) == 1, "exactly one sign-in attempt — retrying is what locks an Amazon account"
    assert {r.order_id for r in rows} == {oid}, "the run must continue after healing, not just log"


def test_a_failed_self_login_reports_its_reason_and_never_reaches_the_agent(monkeypatch):
    """The verdict has to travel: an SMS challenge, a stale password and an anti-bot rejection all
    end here, and the alert is useless unless it names which one. ApiLoginError (not
    AmazonBusinessApiError) is also what stops the PAID agent being spent on an auth failure."""
    _install_fake(monkeypatch, ["<html></html>"], {}, logged_out=True)
    monkeypatch.setattr(api, "deterministic_login", lambda p, a: signin.LoginOutcome(
        False, False, "OTP TO PHONE/EMAIL — Amazon wants a code it sent to a human. WHAT TO DO: "
                      "enrol an authenticator app"))

    auth = RetailerAuth(method="password", username="u@e.com", password="pw")
    client = AmazonBusinessApiClient(_Profile(auth={"amazon-business": auth}))
    with pytest.raises(api.ApiLoginError, match="OTP TO PHONE/EMAIL"):
        client.fetch_order_items("2026-08-08", set(), set(), today="2026-08-10")


def test_a_late_detected_logout_signs_in_rather_than_spending_the_agent(monkeypatch):
    """Best Buy proved this one live: a session can expire without tripping the
    logged-out check, discovery then parses zero orders, and the empty result reads as a page-shape
    change — which sends the run to the PAID agent to rediscover a logout for money. If the page is
    offering to sign in, it is a lapsed session."""
    oid = "111-1111111-1111111"

    class _LateLogout(_FakePage):
        """Looks signed in (no sign-in URL) but serves an empty history until signed in."""

        def __init__(self):
            super().__init__([_history(_order_card(oid, "August 9, 2026"))],
                             {oid: _deliv(oid, "August 9, 2026")})
            self.signed_in = False

        def content(self):
            if not self.signed_in and "your-orders/orders" in self._current:
                return "<html><body><input id='ap-claim' type='hidden'></body></html>"
            return super().content()

        def locator(self, selector, *a, **k):
            if "ap-claim" in selector or "ap_email" in selector:
                class _L:
                    def __init__(self, n):
                        self._n = n

                    def count(self):
                        return self._n
                # looks_logged_out must NOT fire (that is the premise), but the affordance check must.
                return _L(0 if "ap_password" in selector else (0 if self.signed_in else 1))
            return super().locator(selector, *a, **k)

    page = _LateLogout()
    monkeypatch.setattr(api, "CdpBrowser", _FakeCdp(page))
    monkeypatch.setattr(api, "looks_logged_out", lambda p: False)

    def _fake_login(p, auth):
        p.signed_in = True
        return signin.LoginOutcome(True)

    monkeypatch.setattr(api, "deterministic_login", _fake_login)

    auth = RetailerAuth(method="password", username="u@e.com", password="pw")
    client = AmazonBusinessApiClient(_Profile(auth={"amazon-business": auth}))
    rows = client.fetch_order_items("2026-08-08", set(), set(), today="2026-08-10")
    assert {r.order_id for r in rows} == {oid}, "re-discovery after signing in must actually re-read"
