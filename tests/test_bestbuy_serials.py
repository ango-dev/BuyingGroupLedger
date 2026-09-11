"""The pure extractors behind the on-demand Best Buy serial hunt (scrapers/bestbuy_serials.py).

Where serials render is not yet pinned by a live capture, so these test the CONTRACT: a value is
only ever taken when the source itself labels it a serial (a /serial/i key in JSON, a "Serial
Number"-style label in markup) — never harvested by shape alone, because SKUs and order ids
share the alphabet."""

from scrapers.bestbuy_serials import (
    UNATTRIBUTED, serials_by_tracking, serials_from_html, serials_from_payload,
)


class TestPayload:
    def test_serial_keys_are_collected_wherever_they_nest(self):
        payload = [{"order": {"items": [
            {"sku": "6571043", "serialNumber": "C02XY12345"},
            {"sku": "6571043", "fulfillment": {"serial_number": "C02XY12346"}},
        ]}}]
        assert serials_from_payload(payload) == ["C02XY12345", "C02XY12346"]

    def test_serial_key_holding_a_list_yields_each(self):
        payload = {"serialNumbers": ["A11111", "B22222"]}
        assert serials_from_payload(payload) == ["A11111", "B22222"]

    def test_unlabelled_values_are_never_harvested(self):
        """A SKU, an order id, a tracking number — same alphabet, not serials."""
        payload = {"order": {"userOrderId": "BBY01-807200000000", "items": [
            {"sku": "6571043", "tracking": {"trackingNumber": "999900001111"}}]}}
        assert serials_from_payload(payload) == []

    def test_blank_and_non_string_serial_values_are_dropped(self):
        payload = {"serialNumber": "", "serial_id": 12345, "serials": [None, "  "]}
        assert serials_from_payload(payload) == []

    def test_duplicates_collapse_in_document_order(self):
        payload = [{"serialNumber": "SAME01"}, {"serial": "SAME01"}, {"serial": "NEXT02"}]
        assert serials_from_payload(payload) == ["SAME01", "NEXT02"]


class TestHtml:
    def test_labelled_serial_survives_markup_between_label_and_value(self):
        html = '<div><span class="label">Serial Number:</span> <span>C02XY12345</span></div>'
        assert serials_from_html(html) == ["C02XY12345"]

    def test_label_variants_are_recognised(self):
        html = ("<p>Serial #: AA1234567</p>"
                "<p>Serial No. BB7654321</p>"
                "<p>serial number CC1122334</p>")
        assert serials_from_html(html) == ["AA1234567", "BB7654321", "CC1122334"]

    def test_prose_mentioning_the_word_serial_yields_nothing(self):
        html = "<p>Find the serial number on the box or in your account.</p>"
        assert serials_from_html(html) == []

    def test_unlabelled_tokens_are_never_harvested(self):
        html = "<p>Order BBY01-807200000000, tracking 999900001111, SKU 6571043.</p>"
        assert serials_from_html(html) == []

    def test_value_without_a_digit_is_rejected(self):
        """'Serial Number' followed by more prose must not read the next word as the value."""
        html = "<p>Serial Number unavailable for this item.</p>"
        assert serials_from_html(html) == []


class TestSerialsByTracking:
    """The per-package correlation: an item's serials belong to the tracking number that same
    item names — the structure BOTH the 2+1 split and Package ID already rely on."""

    @staticmethod
    def _payload(items):
        return [{"order": {"userOrderId": "BBY01-1", "items": items}}]

    def test_each_items_serials_follow_its_own_tracking_number(self):
        payload = self._payload([
            {"quantity": 2, "serialNumbers": ["A1", "B2"],
             "fulfillment": {"tracking": {"trackingNumber": "999900001111"}}},
            {"quantity": 1, "serialNumber": "C3",
             "fulfillment": {"tracking": {"trackingNumber": "999900002222"}}},
        ])
        assert serials_by_tracking(payload) == {"999900001111": ["A1", "B2"],
                                                "999900002222": ["C3"]}

    def test_an_item_with_no_tracking_number_lands_unattributed(self):
        payload = self._payload([{"quantity": 1, "serialNumber": "A1", "fulfillment": {}}])
        assert serials_by_tracking(payload) == {UNATTRIBUTED: ["A1"]}

    def test_an_item_naming_two_tracking_numbers_is_never_guessed(self):
        payload = self._payload([{"serialNumber": "A1", "fulfillment": {"tracking": [
            {"trackingNumber": "999900001111"}, {"trackingNumber": "999900002222"}]}}])
        assert serials_by_tracking(payload) == {UNATTRIBUTED: ["A1"]}

    def test_order_level_serials_outside_items_land_unattributed(self):
        payload = [{"order": {"userOrderId": "BBY01-1", "serialNumbers": ["Z9"], "items": [
            {"serialNumber": "A1",
             "fulfillment": {"tracking": {"trackingNumber": "999900001111"}}}]}}]
        assert serials_by_tracking(payload) == {"999900001111": ["A1"], UNATTRIBUTED: ["Z9"]}

    def test_a_shapeless_payload_yields_nothing(self):
        assert serials_by_tracking(None) == {}
        assert serials_by_tracking({"error": "not signed in"}) == {}
