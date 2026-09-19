"""One retailer's failure must never take the rest of the run with it.

This is a promise `main()`'s loop makes and, until 2026-08-14, did not fully keep. A monitoring
session watched a scheduled run abort part-way through Best Buy's sign-in: Costco never ran, and
neither did the buying-group sync — from a failure that concerned only one retailer.

Two holes existed. `run_scrape` guarded `scraper.scrape()` but NOT the steps after it (classify, tag
cards, write the CSV), so a bad `warehouses.json` entry or a disk error there escaped. And the loop
itself had no handler at all, so anything raised outside `run_scrape`'s own guards — the scraper's
constructor, for instance — ended the run. The buying-group sync runs after the loop, so either hole
also silently skipped submitting tracking numbers that were already sitting on the sheet.
"""

import pytest

import main
from models.order import OrderItem


def _item(order_id="O-1"):
    return OrderItem(
        order_date="2026-08-13", status="shipped", profile_label="p", retailer="Best Buy",
        item_name="Thing", quantity=1, order_id=order_id, tracking_number="1Z1", shipment="1",
    )


class _Scraper:
    """Minimal stand-in for a retailer scraper."""

    retailer_name = "Best Buy"
    retailer_key = "bestbuy"

    def __init__(self, profile):
        self.profile = profile

    def scrape(self):
        return [_item()]


class TestRunScrapeContainsPostScrapeFailures:
    def test_a_classifier_failure_does_not_escape_run_scrape(self, monkeypatch, profile):
        monkeypatch.setattr(main, "_classify_and_drop_personal",
                            lambda items, label: (_ for _ in ()).throw(RuntimeError("bad warehouses.json")))
        alerted = []
        monkeypatch.setattr(main, "alert", lambda subject, body: alerted.append(subject))

        main.run_scrape(_Scraper(profile))  # must return, not raise

        assert any("post-scrape" in s.lower() for s in alerted), "the failure must still be reported"

    def test_a_csv_write_failure_does_not_escape_run_scrape(self, monkeypatch, profile):
        monkeypatch.setattr(main, "_classify_and_drop_personal", lambda items, label: items)
        monkeypatch.setattr(main, "_tag_cards", lambda items, label: None)
        monkeypatch.setattr(main, "write_csv",
                            lambda items: (_ for _ in ()).throw(OSError("disk full")))
        monkeypatch.setattr(main, "alert", lambda *a: None)

        main.run_scrape(_Scraper(profile))  # must return, not raise


class TestReceiptCaptureCannotCostARow:
    """Receipt capture is additive; the ledger row is not.

    A missing receipt is an inconvenience — the next run retries it. A missing ORDER is missed
    reimbursement money. So `_capture_receipts` sits between tagging and write_csv and must swallow
    everything: a storage outage, an expired browser session or a changed page cannot be allowed to
    skip the CSV write and the sheet sync three lines later.
    """

    def test_a_capture_failure_still_writes_and_syncs_the_rows(self, monkeypatch, profile):
        monkeypatch.setattr(main, "_classify_and_drop_personal", lambda items, label: items)
        monkeypatch.setattr(main, "_tag_cards", lambda items, label: None)
        written, synced = [], []
        monkeypatch.setattr(main, "write_csv",
                            lambda items: written.append(items) or __import__("pathlib").Path("x.csv"))
        monkeypatch.setattr(main, "sync_csv_to_ledger", lambda p: synced.append(p) or {"appended": 0})
        monkeypatch.setattr("receipts.capture.attach_receipts",
                            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("bucket down")))
        fired = []
        monkeypatch.setattr(main, "alert", lambda subject, body: fired.append(subject))

        main.run_scrape(_Scraper(profile))

        assert written, "the rows must still be written when only the receipt failed"
        assert synced, "the rows must still reach the sheet when only the receipt failed"
        assert any("receipt" in s.lower() for s in fired), "the failure must still be reported"

    def test_capture_runs_before_the_csv_is_written(self, monkeypatch, profile):
        """The link has to land in the SAME sync as its rows, or the row is written blank and only
        picks the link up on a later re-check — which never comes for a terminal order."""
        order = []
        monkeypatch.setattr(main, "_classify_and_drop_personal", lambda items, label: items)
        monkeypatch.setattr(main, "_tag_cards", lambda items, label: None)
        monkeypatch.setattr("receipts.capture.attach_receipts",
                            lambda *a, **kw: order.append("capture"))
        monkeypatch.setattr(main, "write_csv",
                            lambda items: order.append("csv") or __import__("pathlib").Path("x.csv"))
        monkeypatch.setattr(main, "sync_csv_to_ledger", lambda p: {"appended": 0})
        monkeypatch.setattr(main, "alert", lambda *a: None)

        main.run_scrape(_Scraper(profile))

        assert order == ["capture", "csv"]


class TestTheLoopIsolatesEachRetailer:
    def test_one_retailer_failing_still_runs_the_others_and_the_sync(self, monkeypatch, profile):
        ran, synced = [], []

        class _Boom(_Scraper):
            retailer_name = "Amazon"

            def __init__(self, p):
                raise RuntimeError("constructor blew up")

        class _Ok(_Scraper):
            retailer_name = "Costco"

        monkeypatch.setattr(main, "SCRAPERS", {"amazon": _Boom, "costco": _Ok})
        monkeypatch.setattr(main, "load_profiles_for_retailer", lambda key: [profile])
        monkeypatch.setattr(main, "run_scrape", lambda s: ran.append(s.retailer_name))
        monkeypatch.setattr(main, "run_buying_group_sync", lambda: synced.append(True))
        monkeypatch.setattr(main, "alert", lambda *a: None)

        main.main(["amazon", "costco"])

        assert ran == ["Costco"], "the retailer after the failure must still run"
        assert synced, (
            "the buying-group sync must still run — it submits tracking for rows already on the "
            "sheet, so it has work to do even when every scrape fails"
        )

    def test_the_failure_is_alerted_not_silently_swallowed(self, monkeypatch, profile):
        alerted = []

        class _Boom(_Scraper):
            def __init__(self, p):
                raise RuntimeError("nope")

        monkeypatch.setattr(main, "SCRAPERS", {"bestbuy": _Boom})
        monkeypatch.setattr(main, "load_profiles_for_retailer", lambda key: [profile])
        monkeypatch.setattr(main, "run_buying_group_sync", lambda: None)
        monkeypatch.setattr(main, "alert", lambda subject, body: alerted.append(subject))

        main.main(["bestbuy"])

        assert alerted, "a skipped retailer must be visible, not silently dropped"


@pytest.fixture
def profile():
    from models.profile import ProfileConfig

    return ProfileConfig(label="p", profile_id="pid", retailers=["bestbuy"])


class TestSheetFailuresAreLoudNotSilent:
    """Sheet-side failures degrade collection SILENTLY, which CLAUDE.md ranks as the worst mode:
    "silently records nothing". Two were observed live on 2026-08-15 -- an append that dropped 4
    scraped rows, and a transient 503 on the order-state read that skipped every re-check. Both
    logged and carried on. Continuing the run is CORRECT; doing it quietly is not.
    """

    def test_a_failed_sheet_sync_alerts_and_says_how_many_rows_were_lost(self, monkeypatch, profile):
        monkeypatch.setattr(main, "_classify_and_drop_personal", lambda items, label: items)
        monkeypatch.setattr(main, "_tag_cards", lambda items, label: None)
        monkeypatch.setattr(main, "write_csv", lambda items: __import__("pathlib").Path("x.csv"))
        monkeypatch.setattr(main, "sync_csv_to_ledger",
                            lambda p: (_ for _ in ()).throw(RuntimeError("APIError 400")))
        fired = []
        monkeypatch.setattr(main, "alert", lambda subject, body: fired.append((subject, body)))

        main.run_scrape(_Scraper(profile))  # continues, does not raise

        assert fired, "a sync failure must alert -- the scraped rows are gone otherwise"
        subject, body = fired[0]
        assert "1 row" in subject, f"the alert must say how many rows were lost: {subject!r}"

    def test_an_unreadable_order_state_alerts(self, monkeypatch):
        """The 503 case. It is not enough to log: the run then looks completely normal apart from
        fetching fewer orders than usual."""
        from ledger import sync as ledger_sync

        monkeypatch.setattr(ledger_sync, "_get_worksheet",
                            lambda: (_ for _ in ()).throw(RuntimeError("503 unavailable")))
        fired = []
        monkeypatch.setattr(ledger_sync, "alert", lambda subject, body: fired.append(subject))

        state = ledger_sync.load_order_state("profile-alpha", retailer="Amazon")

        assert state["open_orders"] == [], "must still fail soft and let the run continue"
        assert fired, "an unreadable sheet must alert"
        assert "re-checks skipped" in fired[0].lower()

    def test_the_warning_no_longer_claims_it_treats_everything_as_new(self, monkeypatch, caplog):
        """The old wording read as conservative OVER-fetching. The real effect is the opposite: the
        fetch set is (new-in-window + still-open re-checks), so an empty state fetches FEWER orders.
        Live it took Amazon from "fetching 1" to "fetching 0"."""
        from ledger import sync as ledger_sync

        monkeypatch.setattr(ledger_sync, "_get_worksheet",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.setattr(ledger_sync, "alert", lambda *a: None)
        with caplog.at_level("WARNING"):
            ledger_sync.load_order_state("p")
        text = caplog.text.lower()
        assert "treating all orders as new" not in text
        assert "skipped" in text
