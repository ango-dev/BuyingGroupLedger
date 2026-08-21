"""Auditing what is ALREADY stored — the half the capture-time guard cannot cover.

Capture checks a document on the way in, but it fails OPEN (an unreadable PDF is stored anyway) and
it obviously cannot reach anything uploaded before the guard existed. Since a receipt is written once
and never refreshed, and these substantiate COGS at tax time, a bad one stays bad forever unless
something goes looking.
"""

import pytest

from scripts import receipt_verify as rv

FINAL = "Final Details for Order #A1 Shipped on August 20, 2026 Grand Total $10 Payment Method Visa"
INTERIM = "Details for Order #A1 Not Yet Shipped Order Total $10 Payment Method Visa"


def _body(text):
    """Bytes whose extracted text is `text` — _text is monkeypatched, so content is irrelevant."""
    return b"%PDF-1.4 " + text.encode()


@pytest.fixture(autouse=True)
def _text_is_the_body(monkeypatch):
    monkeypatch.setattr(rv, "_text", lambda b: b[len(b"%PDF-1.4 "):].decode())


class TestProblems:
    def test_a_final_invoice_passes(self):
        assert rv._problems("receipts/amazon-business/2026-08/A1.pdf", "A1", "amazon-business",
                            _body(FINAL)) == []

    def test_a_pre_shipment_invoice_fails(self):
        """The live case: the ledger said `shipped` because a TBA tracking number existed
        (Amazon assigns those at LABEL CREATION), while Amazon's own invoice said otherwise."""
        problems = rv._problems("receipts/amazon-business/2026-08/A1.pdf", "A1", "amazon-business",
                                _body(INTERIM))

        assert len(problems) == 1
        assert "PRE-SHIPMENT" in problems[0]

    def test_the_same_wording_is_not_a_failure_for_a_retailer_with_no_marker(self):
        """Only Amazon Business's invoice is Amazon-generated and explicitly labelled. Consumer
        Amazon's rendered page has no finality wording at all, so it must not be judged on one."""
        assert rv._problems("receipts/amazon/2026-08/A1.pdf", "A1", "amazon", _body(INTERIM)) == []

    def test_a_document_for_the_wrong_order_fails(self):
        """The cross-contamination check. Costco's page is a hash-route SPA where navigating A -> B
        can leave A's render up, and Amazon Business invoices for same-priced orders are nearly
        identical in size — either would store a perfect-looking duplicate under the wrong key."""
        problems = rv._problems("receipts/costco/2026-08/999.pdf", "999", "costco", _body(FINAL))

        assert any("its own order id" in p for p in problems)

    def test_a_receipt_with_no_total_fails(self):
        """Costco hides its whole Order Summary behind a toggle; that is exactly how receipts with
        the item and none of the money got stored once."""
        problems = rv._problems("receipts/costco/2026-08/A1.pdf", "A1", "costco",
                                _body("Order Details A1 iPad Payment Method Visa"))

        assert any("no total" in p for p in problems)

    def test_a_receipt_with_no_payment_method_fails(self):
        problems = rv._problems("receipts/bestbuy/2026-08/A1.pdf", "A1", "bestbuy",
                                _body("Order A1 Grand Total $10"))

        assert any("no payment method" in p for p in problems)

    def test_an_empty_object_fails(self):
        assert rv._problems("receipts/amazon/2026-08/A1.pdf", "A1", "amazon", b"") == ["empty object"]

    def test_unextractable_text_is_reported_rather_than_passed(self):
        """Silently passing something unreadable would be the worst outcome: it would read as
        verified when nothing was verified."""
        problems = rv._problems("receipts/amazon/2026-08/A1.pdf", "A1", "amazon", _body("   "))

        assert problems and "cannot be verified" in problems[0]

    def test_a_png_fallback_is_not_a_failure(self):
        """A screenshot has no text to read. It is a real receipt, just not a verifiable one, and
        failing it would only train people to ignore the output."""
        assert rv._problems("receipts/amazon/2026-08/A1.png", "A1", "amazon", b"\x89PNG") == []


class TestRowsByOrder:
    HEADER_ROW = ["Order ID", "Receipt Link"]

    def test_only_rows_that_actually_have_a_link_are_collected(self, monkeypatch):
        grid = [self.HEADER_ROW, ["A1", "https://par/x"], ["A1", "https://par/x"], ["A2", ""]]

        assert rv._rows_by_order(grid) == {"A1": [2, 3]}

    def test_a_short_row_does_not_raise(self):
        """get_all_values truncates trailing empties, so rows are routinely shorter than the header."""
        assert rv._rows_by_order([self.HEADER_ROW, ["A1"], []]) == {}


class TestPurge:
    """A plain delete on a versioned bucket is NOT a delete — it writes a marker and keeps the
    object recoverable and billable. Purging has to name every VersionId."""

    class FakeS3:
        def __init__(self):
            self.deleted = []

        def list_object_versions(self, Bucket, Prefix):  # noqa: N803 — boto3's kwarg spelling
            return {
                "Versions": [
                    {"Key": Prefix, "VersionId": "v1"},
                    {"Key": Prefix, "VersionId": "v2"},
                    {"Key": Prefix + ".bak", "VersionId": "other"},   # Prefix is a PREFIX
                ],
                "DeleteMarkers": [{"Key": Prefix, "VersionId": "m1"}],
            }

        def delete_object(self, Bucket, Key, VersionId=None):  # noqa: N803
            self.deleted.append((Key, VersionId))

    @staticmethod
    def _wire(monkeypatch):
        """Settings is a FROZEN dataclass (deliberately — a credential must not be mutable
        mid-run), so swap the whole object rather than one field."""
        import dataclasses

        fake = TestPurge.FakeS3()
        monkeypatch.setattr(rv.store, "_s3", lambda: fake)
        monkeypatch.setattr(rv.store, "settings",
                            dataclasses.replace(rv.store.settings, oci_bucket="b"))
        return fake

    def test_every_version_and_marker_is_removed_by_id(self, monkeypatch):
        fake = self._wire(monkeypatch)

        removed = rv._purge("receipts/amazon/2026-08/A1.pdf")

        assert removed == 3
        assert [v for _, v in fake.deleted] == ["v1", "v2", "m1"]
        assert all(v is not None for _, v in fake.deleted), "a delete without a VersionId is a marker"

    def test_a_similarly_named_object_is_left_alone(self, monkeypatch):
        fake = self._wire(monkeypatch)

        rv._purge("receipts/amazon/2026-08/A1.pdf")

        assert all(k == "receipts/amazon/2026-08/A1.pdf" for k, _ in fake.deleted)


class TestReadOnlyByDefault:
    def test_purge_is_off_unless_asked(self):
        """The default has to be safe: this deletes documents that cannot be re-fetched once a
        retailer ages the order out of the account."""
        import inspect

        assert inspect.signature(rv.run).parameters["purge"].default is False
