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


# The LEGACY tracking layout with a SHIPPED package. None of the pt-* elements; the
# promise in `.promise-container-inner` reads "Estimated to arrive by September 20" and the carrier
# card is a "Shipped with Amazon" widget whose h4 reads "Tracking ID: TBA999000000011". The reader
# required the pt headline, so this shipped package read as "selectors did not match" and its number
# was NOT recorded. This is the redacted fragment of that page, in the DOM shape the dossier captured.
LEGACY_SHIPPED_FRAGMENT = """
<div class="a-row a-spacing-small" id="topContent-container"><div class="a-row a-spacing-medium max-container-x" id="topContent-inner">
<span id="productCarousel-outer"><div class="a-row widgetContainer background-normal" id="promise-background-container">
<div class="a-row a-spacing-small a-spacing-top-small widgetContainer inline-layout" id="itemImagesCarousel-container">
<div class="a-row single-item background-normal" id="promise-container">
<div class="a-row promise-container-inner">
<span class="text-emphasized size-26" id="primaryStatus">Estimated to arrive by <span class="nowrap">September 20</span></span>
</div></div></div></div></span></div></div>
<div class="a-row a-spacing-small max-container-x" id="mainContent-container">
<div class="a-row">We\u2019re very sorry your delivery is late. If you have not received your package by tomorrow, you can come back here the next day for a refund.</div>
</div>
<div class="a-row max-container-x" id="cardsContainer"><div class="a-column a-span4">
<div class="a-row a-spacing-small cardContainer-wrapper" id="carrierRelatedInfo-container"><div class="a-row cardContainer">
<div class="a-row"><div class="a-fixed-right-grid-col" style="float:left;"><h1 class="a-spacing-small widgetHeader">Shipped with Amazon</h1></div></div>
<div class="a-row"><div class="a-fixed-right-grid-col" style="float:left;"><h4 class="a-spacing-small carrierRelatedInfo-trackingId-text">Tracking ID: TBA999000000011</h4></div></div>
<div class="a-row"><span class="a-declarative" data-action="tracking-events-open-modal"><a class="a-link-normal tracking-events-modal-trigger tracker-seeDetailsLink" href="#">See all updates</a></span></div>
</div></div></div></div>
"""


class SoupPage:
    """Fake Playwright page over a real DOM fragment: the scraper's own CSS selectors run against it,
    so a selector that stops matching the captured shape fails here, not just in a SelectorPage map."""

    def __init__(self, html):
        from bs4 import BeautifulSoup
        self._soup = BeautifulSoup(html, "html.parser")

    def query_selector(self, sel):
        el = self._soup.select_one(sel)
        return FakeEl(el.get_text(" ", strip=True)) if el is not None else None


class TestLegacyShippedLayout:
    """The legacy layout once the package has shipped: the carrier card's number is the proof, and
    the promise ("Estimated to arrive by ...") is not the pre-ship wording. Without the number the
    page still fails loudly — an estimate alone is not evidence either way."""

    scraper = AmazonScraper(ProfileConfig(label="p1", profile_id="x", retailers=["amazon"]))
    E = AmazonScraper.preship_promise_selector
    L = AmazonScraper.legacy_tracking_number_selector

    def read(self, elements):
        return AmazonScraper.read_tracking_page(self.scraper, SelectorPage(elements))

    def test_the_dossier_page_reads_shipped_with_its_number(self):
        info = AmazonScraper.read_tracking_page(self.scraper, SoupPage(LEGACY_SHIPPED_FRAGMENT))
        assert info == {"status": "shipped", "tracking_number": "TBA999000000011",
                        "delivery_promise": "Estimated to arrive by September 20"}

    def test_business_twin_reads_the_same_page(self):
        from scrapers.amazon_business import AmazonBusinessScraper
        s = AmazonBusinessScraper(ProfileConfig(label="p2", profile_id="y",
                                                retailers=["amazon-business"]))
        info = AmazonBusinessScraper.read_tracking_page(s, SoupPage(LEGACY_SHIPPED_FRAGMENT))
        assert info["status"] == "shipped"
        assert info["tracking_number"] == "TBA999000000011"

    def test_an_estimate_with_no_carrier_card_is_still_unreadable(self):
        # Neither "Order received" nor a number: an unknown state, a dossier problem — not 'ordered'.
        assert self.read({self.E: "Estimated to arrive by September 20"}) is None

    def test_a_carrier_card_with_an_empty_number_is_unreadable(self):
        assert self.read({self.E: "Estimated to arrive by September 20", self.L: "Tracking ID:"}) is None

    def test_delivered_wording_on_the_legacy_layout_is_terminal(self):
        info = self.read({self.E: "Delivered September 20", self.L: "Tracking ID: TBA999000000011"})
        assert info["status"] == "delivered"
        assert info["tracking_number"] == "TBA999000000011"

    def test_order_received_still_reads_ordered_without_a_card(self):
        info = self.read({self.E: "Order received"})
        assert info["status"] == "ordered" and info["tracking_number"] == ""

    def test_the_pt_layout_is_untouched_by_the_legacy_selector(self):
        # A pt page never carries the legacy h4; the pt headline path decides as before.
        info = self.read({AmazonScraper.promise_selector: "Arriving tomorrow"})
        assert info["status"] == "ordered"

    def test_both_scrapers_declare_the_legacy_selector_for_the_dossier_audit(self):
        from scrapers.amazon_business import AmazonBusinessScraper
        for cls in (AmazonScraper, AmazonBusinessScraper):
            assert cls.diagnostic_selectors["pt_legacy_tracking_number"] == ".carrierRelatedInfo-trackingId-text"
            assert cls.legacy_tracking_number_selector == AmazonScraper.legacy_tracking_number_selector


# The legacy layout for a DELAYED order that has NOT shipped: no promise container, no carrier card, no pt-* element
# -- only the exception box below, and an "Order Info" card offering "View or Change this order" /
# "Cancel order". The reader required either the pt headline or the promise container, so Amazon's
# own "being prepared to ship" read as "selectors did not match". Redacted fragment of that DOM.
LEGACY_DELAYED_FRAGMENT = """
<div class="a-row a-spacing-small" id="topContent-container"><div class="a-row a-spacing-medium max-container-x" id="topContent-inner">
<div class="a-row widgetContainer background-normal" id="promise-background-container"></div></div></div>
<div class="a-row a-spacing-small max-container-x" id="mainContent-container"><div class="a-row" id="pageContainer-inner">
<div class="a-row a-spacing-small visible" id="split-container"><div class="a-column a-span5" id="leftColumn-container">
<div class="a-row a-spacing-small a-spacing-top-base widgetContainer" id="lexicalExceptionMessage-container">
<div class="a-row a-spacing-large lexicalExceptionMessage-container">
<h3>We're sorry your order is delayed. It's being prepared to ship and we'll notify you when it's on its way. If you need to, you can manage your order using the options below.</h3>
</div></div></div></div></div></div>
<div class="a-row max-container-x" id="cardsContainer"><div class="a-column a-span4">
<div class="a-row a-spacing-small cardContainer-wrapper" id="ordersInPackage-container"><div class="a-row cardContainer">
<div class="a-row"><h1 class="a-spacing-small widgetHeader">Order Info</h1></div>
<div class="a-row"><a class="a-link-normal" href="/gp/css/order-details?orderID=111-9990015-9990015">View or Change this order</a></div>
<div class="a-row"><a class="a-link-normal" href="/progress-tracker/package/preship/cancel-items?orderID=111-9990015-9990015">Cancel order</a></div>
</div></div></div></div>
"""


class TestLegacyDelayedPreShip:
    """A delayed, unshipped order on the legacy layout is a legitimate 'ordered' -- on Amazon's
    "being prepared to ship" wording only, and only with no number on the page."""

    scraper = AmazonScraper(ProfileConfig(label="p1", profile_id="x", retailers=["amazon"]))
    X = AmazonScraper.legacy_exception_selector
    L = AmazonScraper.legacy_tracking_number_selector
    DELAYED = ("We're sorry your order is delayed. It's being prepared to ship and we'll notify you "
               "when it's on its way.")

    def read(self, elements):
        return AmazonScraper.read_tracking_page(self.scraper, SelectorPage(elements))

    def test_the_dossier_page_reads_ordered_with_no_number(self):
        info = AmazonScraper.read_tracking_page(self.scraper, SoupPage(LEGACY_DELAYED_FRAGMENT))
        assert info is not None
        assert info["status"] == "ordered" and info["tracking_number"] == ""
        assert info["delivery_promise"].startswith("We're sorry your order is delayed")

    def test_business_twin_reads_the_same_page(self):
        from scrapers.amazon_business import AmazonBusinessScraper
        s = AmazonBusinessScraper(ProfileConfig(label="p2", profile_id="y",
                                                retailers=["amazon-business"]))
        info = AmazonBusinessScraper.read_tracking_page(s, SoupPage(LEGACY_DELAYED_FRAGMENT))
        assert info is not None and info["status"] == "ordered" and info["tracking_number"] == ""

    def test_any_other_exception_wording_is_still_unreadable(self):
        assert self.read({self.X: "We're sorry, something went wrong with your order."}) is None

    def test_an_exception_box_beside_a_carrier_number_is_unreadable(self):
        # Amazon saying "being prepared to ship" next to a tracking number is a contradiction,
        # not a state to record either way.
        assert self.read({self.X: self.DELAYED, self.L: "Tracking ID: TBA300000000001"}) is None

    def test_an_empty_page_is_still_unreadable(self):
        assert self.read({}) is None

    def test_the_promise_container_still_wins_when_both_render(self):
        info = self.read({AmazonScraper.preship_promise_selector: "Order received",
                          self.X: self.DELAYED})
        assert info["status"] == "ordered"

    def test_both_scrapers_declare_the_exception_selector_for_the_dossier_audit(self):
        from scrapers.amazon_business import AmazonBusinessScraper
        for cls in (AmazonScraper, AmazonBusinessScraper):
            assert cls.diagnostic_selectors["pt_legacy_exception"] == "#lexicalExceptionMessage-container"
            assert cls.legacy_exception_selector == AmazonScraper.legacy_exception_selector
