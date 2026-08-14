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
