"""Routing between the cheap CDP reader and the expensive agent.

The rule: the agent re-reads an order's details page only while some shipment still lacks a
tracking number (it can still split); once everything is tracked, delivery is watched per
shipment by selector. A selector that comes back empty must escalate that order to the agent
rather than be treated as "not shipped".
"""

import pytest

from models.profile import ProfileConfig
from scrapers import base as base_mod
from scrapers.amazon import AmazonScraper
from scrapers.base import BaseRetailerScraper
from scrapers.bestbuy import BestBuyScraper


def shipment(label, *, status="shipped", number="1Z1", url="http://track/1", items=("W",)):
    return {
        "shipment": label, "status": status, "tracking_number": number,
        "tracking_url": url, "delivery_date": "", "item_names": list(items),
    }


def order(order_id="A1", *, needs_agent=False, shipments=None):
    return {
        "order_id": order_id, "order_date": "2026-08-08", "order_url": "http://order/1",
        "status": "shipped", "needs_agent": needs_agent,
        "shipments": shipments if shipments is not None else [shipment("1")],
    }


class FakePage:
    def __init__(self):
        self.visited = []

    def goto(self, url, **kwargs):
        self.visited.append(url)

    def wait_for_timeout(self, _ms):
        pass


class FakeCdpBrowser:
    """Stands in for the real CDP browser context manager."""

    last = None

    def __init__(self, _profile):
        self.page = FakePage()
        FakeCdpBrowser.last = self

    def __enter__(self):
        return self.page

    def __exit__(self, *exc):
        return False


@pytest.fixture
def cdp(monkeypatch):
    """Install the fake browser into the module scrapers.base imports it from."""
    import scrapers.cdp

    monkeypatch.setattr(scrapers.cdp, "CdpBrowser", FakeCdpBrowser)
    return FakeCdpBrowser


def amazon(monkeypatch, reader):
    """An AmazonScraper whose tracking-page reader is replaced by `reader`."""
    monkeypatch.setattr(AmazonScraper, "read_tracking_page", lambda self, page: reader(page))
    return AmazonScraper(ProfileConfig(label="p1", profile_id="x", retailers=["amazon"]))


READ_OK = lambda page: {"status": "shipped", "tracking_number": "1ZREAD", "delivery_promise": "Arriving Monday"}
READ_NONE = lambda page: None


class FakeEl:
    def __init__(self, text):
        self._text = text

    def inner_text(self):
        return self._text


class SelectorPage:
    """Fake Playwright page: a selector present in `elements` resolves to an element with that
    text (possibly ""); a selector absent from the map resolves to None (a real DOM miss)."""

    def __init__(self, elements):
        self._elements = elements

    def query_selector(self, sel):
        return FakeEl(self._elements[sel]) if sel in self._elements else None


class TestReadTrackingPage:
    """read_tracking_page's shipped/ordered/agent logic, validated against the live pt page. The
    carrier number sits in a "Delivery Info" card that renders only after shipping, so the card's
    presence is the "has shipped" signal: no card = not shipped = 'ordered'; card + number =
    'shipped'; card present but no extractable number = stale selector = agent fallback (None)."""

    scraper = AmazonScraper(ProfileConfig(label="p1", profile_id="x", retailers=["amazon"]))
    P = AmazonScraper.promise_selector
    C = AmazonScraper.delivery_card_selector
    T = AmazonScraper.tracking_number_selector

    def read(self, elements):
        return AmazonScraper.read_tracking_page(self.scraper, SelectorPage(elements))

    def test_no_promise_element_falls_back_to_agent(self):
        assert self.read({}) is None

    def test_not_shipped_has_no_card_and_is_ordered(self):
        # Only the "Arriving" estimate; the Delivery Info card hasn't rendered yet.
        info = self.read({self.P: "Arriving tomorrow"})
        assert info is not None, "no card is a real 'not shipped yet', not a selector miss"
        assert info["status"] == "ordered"
        assert info["tracking_number"] == ""

    def test_shipped_card_with_number_is_shipped(self):
        info = self.read({self.P: "Arriving tomorrow", self.C: "Delivery Info",
                          self.T: "Tracking ID: TBA999000000001"})
        assert info["status"] == "shipped"
        assert info["tracking_number"] == "TBA999000000001"

    def test_tracking_id_label_prefix_is_stripped(self):
        info = self.read({self.P: "Arriving Monday", self.C: "x", self.T: "Tracking ID:   1Z999AA  "})
        assert info["tracking_number"] == "1Z999AA"

    def test_number_without_a_label_is_kept_as_is(self):
        info = self.read({self.P: "Arriving Monday", self.C: "x", self.T: "TBA303111"})
        assert info["tracking_number"] == "TBA303111"

    def test_shipped_card_but_no_extractable_number_escalates_to_agent(self):
        # Card present (shipped) but the number selector is stale/empty -> agent.
        assert self.read({self.P: "Arriving tomorrow", self.C: "Delivery Info"}) is None

    def test_delivered_is_terminal_even_without_a_number(self):
        info = self.read({self.P: "Delivered Aug 5"})
        assert info["status"] == "delivered"
        assert info["tracking_number"] == ""

    def test_delivered_keeps_its_number_when_present(self):
        info = self.read({self.P: "Delivered", self.C: "x", self.T: "Tracking ID: TBA303999"})
        assert info["status"] == "delivered"
        assert info["tracking_number"] == "TBA303999"


class TestRouting:
    def test_every_shipment_gets_its_own_page_read(self, monkeypatch, cdp):
        """The whole point of the reshape — a split order has one tracking page per shipment."""
        scraper = amazon(monkeypatch, READ_OK)
        o = order(shipments=[
            shipment("1", url="http://track/1", items=["W"]),
            shipment("2", url="http://track/2", items=["X"]),
        ])

        items, agent_orders = scraper._recheck_via_cdp([o])

        assert cdp.last.page.visited == ["http://track/1", "http://track/2"]
        assert {(i.shipment, i.item_name) for i in items} == {("1", "W"), ("2", "X")}
        assert agent_orders == []

    def test_items_are_written_against_their_own_shipment(self, monkeypatch, cdp):
        # Previously every item in the order was written against ONE shipment label, which would
        # have corrupted the key for multi-shipment orders.
        scraper = amazon(monkeypatch, READ_OK)
        o = order(shipments=[
            shipment("1", items=["W", "X"]),
            shipment("2", url="http://track/2", items=["Y"]),
        ])

        items, _ = scraper._recheck_via_cdp([o])

        by_shipment = {}
        for i in items:
            by_shipment.setdefault(i.shipment, []).append(i.item_name)
        assert by_shipment == {"1": ["W", "X"], "2": ["Y"]}

    def test_needs_agent_order_goes_to_the_agent_and_is_still_read(self, monkeypatch, cdp):
        scraper = amazon(monkeypatch, READ_OK)
        o = order(needs_agent=True)

        items, agent_orders = scraper._recheck_via_cdp([o])

        assert [a["order_id"] for a in agent_orders] == ["A1"], "structure check still needed"
        assert items, "but the tracking page is still read cheaply in the same pass"

    def test_fully_tracked_order_never_reaches_the_agent(self, monkeypatch, cdp):
        scraper = amazon(monkeypatch, READ_OK)

        _, agent_orders = scraper._recheck_via_cdp([order(needs_agent=False)])

        assert agent_orders == []

    def test_delivered_shipments_are_not_read(self, monkeypatch, cdp):
        scraper = amazon(monkeypatch, READ_OK)
        o = order(shipments=[
            shipment("1", status="delivered", url="http://track/1"),
            shipment("2", url="http://track/2"),
        ])

        scraper._recheck_via_cdp([o])

        assert cdp.last.page.visited == ["http://track/2"]

    def test_cancelled_shipments_are_not_read(self, monkeypatch, cdp):
        scraper = amazon(monkeypatch, READ_OK)
        o = order(shipments=[
            shipment("1", status="cancelled", url="http://track/1"),
            shipment("2", url="http://track/2"),
        ])

        scraper._recheck_via_cdp([o])

        assert cdp.last.page.visited == ["http://track/2"]

    def test_shipment_without_a_link_is_skipped_not_guessed(self, monkeypatch, cdp):
        scraper = amazon(monkeypatch, READ_OK)
        o = order(needs_agent=True, shipments=[shipment("1", number="", url="")])

        items, agent_orders = scraper._recheck_via_cdp([o])

        assert items == []
        assert [a["order_id"] for a in agent_orders] == ["A1"]


class TestFallback:
    def test_empty_selector_escalates_to_the_agent(self, monkeypatch, cdp):
        """A null read must never be reported as 'not shipped' — that would lose a tracking number."""
        scraper = amazon(monkeypatch, READ_NONE)

        items, agent_orders = scraper._recheck_via_cdp([order()])

        assert items == []
        assert [a["order_id"] for a in agent_orders] == ["A1"]

    def test_order_is_not_queued_for_the_agent_twice(self, monkeypatch, cdp):
        scraper = amazon(monkeypatch, READ_NONE)
        o = order(needs_agent=True, shipments=[
            shipment("1", url="http://track/1"),
            shipment("2", url="http://track/2"),
        ])

        _, agent_orders = scraper._recheck_via_cdp([o])

        assert len(agent_orders) == 1

    def test_browser_failure_sends_everything_to_the_agent(self, monkeypatch):
        import scrapers.cdp

        class Broken:
            def __init__(self, _profile):
                raise RuntimeError("no browser")

        monkeypatch.setattr(scrapers.cdp, "CdpBrowser", Broken)
        scraper = amazon(monkeypatch, READ_OK)

        items, agent_orders = scraper._recheck_via_cdp([order()])

        assert items == []
        assert [a["order_id"] for a in agent_orders] == ["A1"]

    def test_cdp_never_writes_a_delivery_date(self, monkeypatch, cdp):
        """The tracking page states a promise in prose ("Arriving Monday"), not a date. Writing it
        would clobber the agent's YYYY-MM-DD; blank leaves the existing value alone."""
        scraper = amazon(monkeypatch, READ_OK)

        items, _ = scraper._recheck_via_cdp([order()])

        assert all(i.delivery_date == "" for i in items)


class TestRetailersWithoutAReader:
    def test_bestbuy_routes_everything_to_the_agent(self):
        """Best Buy shows its tracking number on the order-details page, so the agent already has
        it and there is nothing for a selector to add."""
        scraper = BestBuyScraper(ProfileConfig(label="p1", profile_id="x", retailers=["bestbuy"]))
        assert type(scraper).read_tracking_page is BaseRetailerScraper.read_tracking_page

        items, agent_orders = scraper._recheck_via_cdp([order()])

        assert items == []
        assert len(agent_orders) == 1

    def test_no_open_orders_does_nothing(self, monkeypatch, cdp):
        scraper = amazon(monkeypatch, READ_OK)
        assert scraper._recheck_via_cdp([]) == ([], [])


def test_amazon_has_the_cheap_path_enabled():
    assert AmazonScraper.cdp_recheck_enabled is True
    assert AmazonScraper.read_tracking_page is not BaseRetailerScraper.read_tracking_page
