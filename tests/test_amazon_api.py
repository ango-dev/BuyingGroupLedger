"""Offline tests for the AmazonApiClient orchestration (no real browser).

The HTML parsing is covered by tests/test_amazon_mapping.py; here a fake CDP page drives
`fetch_order_items` end-to-end to lock in the browser-independent logic: which orders get fetched
(window + open + terminal), the pt-page tracking-number hop promoting a shipment to 'shipped', and the
final date keep-filter.
"""

import scrapers.amazon_api as amazon_api
from scrapers.amazon_api import AmazonApiClient
from tests.test_amazon_mapping import _details, _item, _shipment


class _FakeLocator:
    def count(self):
        return 0


class _FakePage:
    """Serves synthetic order-history / order-details / pt HTML keyed off the URL passed to goto."""

    def __init__(self, history_html, details_by_id):
        self.url = ""
        self._history = history_html
        self._details = details_by_id
        self._current = ""

    def goto(self, url, **kwargs):
        self.url = url
        self._current = url

    def content(self):
        # Order history is paginated (?startIndex=N). Serve the cards on page 1 only; later pages are
        # empty so discovery stops (mirrors Amazon returning no more order-cards past the last page).
        if "your-orders/orders" in self._current or "order-history" in self._current:
            return self._history if ("startIndex=0" in self._current or "startIndex" not in self._current) else "<html></html>"
        for oid, html in self._details.items():
            if f"orderID={oid}" in self._current:
                return html
        return "<html></html>"

    def wait_for_timeout(self, *a):
        pass

    def evaluate(self, *a, **k):
        return None

    def locator(self, *a, **k):
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
    label = "profile-bravo"


def _history(*pairs):
    cards = "".join(
        f'<div class="order-card"><span>Order placed {date}</span>'
        f'<a href="/gp/css/order-details?orderID={oid}">Details</a></div>'
        for oid, date in pairs
    )
    return f"<html><body>{cards}</body></html>"


def _install_fake(monkeypatch, history_html, details_by_id):
    page = _FakePage(history_html, details_by_id)
    monkeypatch.setattr(amazon_api, "CdpBrowser", _FakeCdp(page))
    return page


def test_fetches_only_in_window_and_open_orders(monkeypatch):
    new_oid, old_oid, open_oid = "111-1111111-1111111", "222-2222222-2222222", "333-3333333-3333333"
    history = _history(
        (new_oid, "August 9, 2026"),   # in window -> fetch
        (old_oid, "July 1, 2026"),     # out of window, not open -> skip
        (open_oid, "July 1, 2026"),    # out of window but still open -> fetch
    )
    details = {
        oid: _details(oid, "August 9, 2026" if oid == new_oid else "July 1, 2026",
                      [_shipment(oid, 0, "Delivered August 9", [_item("Thing", "$5.00")])])
        for oid in (new_oid, old_oid, open_oid)
    }
    _install_fake(monkeypatch, history, details)

    client = AmazonApiClient(_Profile())
    rows = client.fetch_order_items("2026-08-08", open_ids={open_oid}, terminal_ids=set(), today="2026-08-10")
    got = {r.order_id for r in rows}
    assert got == {new_oid, open_oid}


def test_terminal_orders_are_never_fetched(monkeypatch):
    oid = "444-4444444-4444444"
    history = _history((oid, "August 9, 2026"))
    details = {oid: _details(oid, "August 9, 2026",
                             [_shipment(oid, 0, "Delivered August 9", [_item("Thing", "$5.00")])])}
    _install_fake(monkeypatch, history, details)

    client = AmazonApiClient(_Profile())
    rows = client.fetch_order_items("2026-08-08", open_ids=set(), terminal_ids={oid}, today="2026-08-10")
    assert rows == []


def test_tracking_hop_promotes_open_shipment_to_shipped(monkeypatch):
    oid = "555-5555555-5555555"
    history = _history((oid, "August 9, 2026"))
    details = {oid: _details(oid, "August 9, 2026",
                             [_shipment(oid, 0, "Arriving Monday", [_item("Thing", "$5.00")], shipment_id="SID")])}
    _install_fake(monkeypatch, history, details)

    # Fake pt-page reader: returns a tracking number for the shipment.
    reader = lambda page: {"status": "shipped", "tracking_number": "TBA42", "delivery_promise": "Arriving"}
    client = AmazonApiClient(_Profile(), tracking_reader=reader)
    rows = client.fetch_order_items("2026-08-08", open_ids=set(), terminal_ids=set(), today="2026-08-10")
    assert len(rows) == 1
    assert rows[0].status == "shipped"
    assert rows[0].tracking_number == "TBA42"


def test_no_tracking_reader_leaves_open_shipment_ordered(monkeypatch):
    oid = "666-6666666-6666666"
    history = _history((oid, "August 9, 2026"))
    details = {oid: _details(oid, "August 9, 2026",
                             [_shipment(oid, 0, "Arriving Monday", [_item("Thing", "$5.00")])])}
    _install_fake(monkeypatch, history, details)

    client = AmazonApiClient(_Profile())  # no tracking_reader
    rows = client.fetch_order_items("2026-08-08", open_ids=set(), terminal_ids=set(), today="2026-08-10")
    assert rows[0].status == "ordered"
    assert rows[0].tracking_number == ""


class _PagedPage(_FakePage):
    """Serves a different order-history page per startIndex so pagination can be exercised."""

    def __init__(self, pages_by_start, details_by_id):
        super().__init__("", details_by_id)
        self._pages = pages_by_start  # {startIndex: history_html}

    def content(self):
        if "your-orders/orders" in self._current:
            import re
            m = re.search(r"startIndex=(\d+)", self._current)
            start = int(m.group(1)) if m else 0
            return self._pages.get(start, "<html></html>")
        for oid, html in self._details.items():
            if f"orderID={oid}" in self._current:
                return html
        return "<html></html>"


def test_discovery_paginates_across_pages(monkeypatch):
    p1 = "111-1111111-1111111", "222-2222222-2222222"  # page 1 (startIndex=0)
    p2 = ("333-3333333-3333333",)                        # page 2 (startIndex=10)
    page1_html = _history(*[(oid, "August 9, 2026") for oid in p1])
    page2_html = _history(*[(oid, "August 8, 2026") for oid in p2])
    pages = {0: page1_html, 10: page2_html}  # startIndex=20 -> empty -> stop
    all_ids = p1 + p2
    details = {oid: _details(oid, "August 9, 2026",
                             [_shipment(oid, 0, "Delivered August 9", [_item("Thing", "$5.00")])])
               for oid in all_ids}
    page = _PagedPage(pages, details)
    monkeypatch.setattr(amazon_api, "CdpBrowser", _FakeCdp(page))

    client = AmazonApiClient(_Profile())
    rows = client.fetch_order_items("2026-08-01", open_ids=set(), terminal_ids=set(), today="2026-08-10")
    assert {r.order_id for r in rows} == set(all_ids)  # order from page 2 not missed


def test_logged_out_raises(monkeypatch):
    class _LoggedOutPage(_FakePage):
        def locator(self, *a, **k):
            class L:
                def count(self):
                    return 1
            return L()

    page = _LoggedOutPage("<html></html>", {})
    monkeypatch.setattr(amazon_api, "CdpBrowser", _FakeCdp(page))
    client = AmazonApiClient(_Profile())
    try:
        client.fetch_order_items("2026-08-08", set(), set(), today="2026-08-10")
        assert False, "expected ApiLoginError"
    except amazon_api.ApiLoginError:
        pass
