"""The pt-page reader that watches a shipment from 'ordered' to 'delivered'.

`AmazonScraper.read_tracking_page` is the deterministic reader `amazon_api._read_tracking_numbers`
injects as `tracking_reader` (Amazon Business shares the identical logic): one read per non-terminal
shipment, straight off Amazon's package-tracking ("pt") page. Returning None means "this page could
not be read" — the API client records it as a dossier problem, so a stale selector alerts instead of
leaving a shipped order looking unshipped forever.
"""

from models.profile import ProfileConfig
from scrapers.amazon import AmazonScraper


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
    """read_tracking_page's shipped/ordered/unreadable logic, validated against the live pt page.
    The carrier number sits in a "Delivery Info" card that renders only after shipping, so the
    card's presence is the "has shipped" signal: no card = not shipped = 'ordered'; card + number =
    'shipped'; card present but no extractable number = stale selector = unreadable (None), which
    the caller reports as a dossier problem."""

    scraper = AmazonScraper(ProfileConfig(label="p1", profile_id="x", retailers=["amazon"]))
    P = AmazonScraper.promise_selector
    C = AmazonScraper.delivery_card_selector
    T = AmazonScraper.tracking_number_selector

    def read(self, elements):
        return AmazonScraper.read_tracking_page(self.scraper, SelectorPage(elements))

    def test_no_promise_element_is_unreadable(self):
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

    def test_shipped_card_but_no_extractable_number_is_unreadable(self):
        # Card present (shipped) but the number selector is stale/empty -> None, a dossier problem.
        assert self.read({self.P: "Arriving tomorrow", self.C: "Delivery Info"}) is None

    def test_delivered_is_terminal_even_without_a_number(self):
        info = self.read({self.P: "Delivered Aug 5"})
        assert info["status"] == "delivered"
        assert info["tracking_number"] == ""

    def test_delivered_keeps_its_number_when_present(self):
        info = self.read({self.P: "Delivered", self.C: "x", self.T: "Tracking ID: TBA303999"})
        assert info["status"] == "delivered"
        assert info["tracking_number"] == "TBA303999"


class TestPreEstimateState:
    """The pt page BEFORE Amazon has a delivery estimate:
    no pt-* elements at all — just `.promise-container-inner` reading "Order received" (the page's
    state JSON showed shortStatus ORDER_PLACED, trackingId ""). A legitimate 'ordered', not a stale
    selector — but ONLY on that exact wording, so a truly unknown layout still fails loudly."""

    scraper = AmazonScraper(ProfileConfig(label="p1", profile_id="x", retailers=["amazon"]))
    E = AmazonScraper.preship_promise_selector

    def read(self, elements):
        return AmazonScraper.read_tracking_page(self.scraper, SelectorPage(elements))

    def test_order_received_is_a_legitimate_ordered(self):
        info = self.read({self.E: "Order received"})
        assert info is not None
        assert info["status"] == "ordered"
        assert info["tracking_number"] == ""

    def test_any_other_wording_in_that_container_is_still_unreadable(self):
        assert self.read({self.E: "Something unrecognized"}) is None

    def test_the_normal_promise_headline_still_wins_when_both_render(self):
        info = self.read({AmazonScraper.promise_selector: "Arriving tomorrow",
                          self.E: "Order received"})
        assert info["status"] == "ordered"


def test_business_scraper_shares_the_pre_estimate_reader():
    from models.profile import ProfileConfig as PC
    from scrapers.amazon_business import AmazonBusinessScraper
    s = AmazonBusinessScraper(PC(label="p2", profile_id="y", retailers=["amazon-business"]))
    info = AmazonBusinessScraper.read_tracking_page(
        s, SelectorPage({AmazonBusinessScraper.preship_promise_selector: "Order received"}))
    assert info is not None and info["status"] == "ordered"
    assert AmazonBusinessScraper.read_tracking_page(s, SelectorPage({})) is None
