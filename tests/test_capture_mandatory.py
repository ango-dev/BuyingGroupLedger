"""The capture gate (2026-09-19): a mandatory cell a scrape could not read is a dossier problem with
the page or payload attached, and an identity cell it could not read is a shape error -- never a
quiet blank, never a quiet default. This module pins the rule itself (models.order), the reporter
(diagnostics.report_unreadable_rows), its agreement with the ledger audit's mandatory lists, and the
Best Buy / Costco call sites driven for real with a fake client (the Amazon call sites are pinned in
tests/test_amazon_api.py and tests/test_amazon_business_api.py, the mapping-level shape errors in
each mapping's own test module).
"""

import copy
import json
from pathlib import Path

import pytest

import diagnostics
from models.order import (
    CAPTURE_FIELD_LABELS, CAPTURE_IDENTITY_FIELDS, CAPTURE_MANDATORY_FIELDS, FIELDNAMES, OrderItem,
    unreadable_fields,
)
from models.profile import ProfileConfig

_BB_FIXTURE = Path(__file__).parent / "fixtures" / "bestbuy_orders.json"
_COSTCO_FIXTURE = Path(__file__).parent / "fixtures" / "costco_order_details.json"
_EMPTY_STATE = {"open_orders": [], "delivered_ids": [], "cancelled_ids": []}


def _row(**overrides) -> OrderItem:
    base = dict(retailer="Amazon", profile_label="p", order_id="111-0000000-0000001", order_date="2026-09-01",
                status="shipped", item_name="Thing", quantity=2, cost_per_item=5.0,
                delivery_address="BFMR, 1 Warehouse Way", card_last4="1234", shipment="1")
    base.update(overrides)
    return OrderItem(**base)


class TestUnreadableFields:
    def test_a_complete_row_has_no_gaps(self):
        assert unreadable_fields(_row()) == []

    @pytest.mark.parametrize("field,blank", [
        ("quantity", None), ("cost_per_item", None), ("delivery_address", ""), ("card_last4", "  "),
    ])
    def test_each_mandatory_blank_is_named(self, field, blank):
        assert unreadable_fields(_row(**{field: blank})) == [field]

    def test_gaps_come_back_in_declared_order(self):
        assert unreadable_fields(_row(quantity=None, card_last4="", cost_per_item=None)) == [
            "quantity", "cost_per_item", "card_last4"]

    @pytest.mark.parametrize("status", ["cancelled", "superseded"])
    def test_a_money_free_row_is_exempt(self, status):
        assert unreadable_fields(_row(status=status, quantity=None, cost_per_item=None, card_last4="")) == []

    def test_a_gift_card_row_needs_no_address_but_still_a_cost_and_a_card(self):
        from config.warehouses import GIFT_CARD
        assert unreadable_fields(_row(buying_group=GIFT_CARD, delivery_address="")) == []
        assert unreadable_fields(_row(buying_group=GIFT_CARD, delivery_address="", cost_per_item=None)) == ["cost_per_item"]

    def test_the_undisclosed_split_marker_is_not_a_gap(self):
        row = _row()
        row.quantity = "*"
        assert unreadable_fields(row) == []


class TestTheGateAgreesWithTheAudit:
    """One vocabulary: every cell the capture must read is a cell the ledger audit demands, under the
    same display name, so a gap the gate reports is the gap mandatory_by_stage would flag later."""

    def test_every_capture_field_is_on_the_audits_mandatory_lists_under_its_ledger_name(self):
        from ledger.sync import HEADER
        from scripts.audit_ledger import MANDATORY_ALWAYS, MANDATORY_COSTED

        for field in CAPTURE_IDENTITY_FIELDS + CAPTURE_MANDATORY_FIELDS:
            header = HEADER[FIELDNAMES.index(field)]
            assert CAPTURE_FIELD_LABELS[field] == header, field
            assert header in MANDATORY_ALWAYS + MANDATORY_COSTED, f"{header} is capture-mandatory but not audit-mandatory"

    def test_identity_fields_are_the_upsert_key(self):
        assert set(CAPTURE_IDENTITY_FIELDS) == {"order_id", "order_date", "item_name"}


class TestReportUnreadableRows:
    def test_outside_a_dossier_it_computes_but_reports_nothing(self):
        found = diagnostics.report_unreadable_rows([_row(cost_per_item=None)], {"cost_per_item": "[data-component='unitPrice']"})
        assert list(found) == ["111-0000000-0000001"]
        assert "Cost Per Item could not be read for 'Thing'" in found["111-0000000-0000001"][0]
        assert "read from [data-component='unitPrice']" in found["111-0000000-0000001"][0]

    def test_inside_a_dossier_every_gap_is_a_problem_and_the_evidence_callback_runs_once_per_order(self, tmp_path):
        rows = [_row(cost_per_item=None), _row(shipment="2", card_last4=""),
                _row(order_id="111-0000000-0000002", item_name="Other"),      # complete: no callback
                _row(order_id="111-0000000-0000003", item_name="Third", quantity=None)]
        evidence = []
        with diagnostics.collecting("amazon", "p", root=tmp_path) as d:
            found = diagnostics.report_unreadable_rows(rows, evidence=lambda oid, msgs: evidence.append((oid, len(msgs))))
        assert set(found) == {"111-0000000-0000001", "111-0000000-0000003"}
        assert len(d.problems) == 3
        assert any("shipment 2: Card Last 4 could not be read" in p for p in d.problems)
        assert evidence == [("111-0000000-0000001", 2), ("111-0000000-0000003", 1)]
        assert all("shape change" in p for p in d.problems)

    def test_a_failing_evidence_callback_never_masks_the_scrape(self, tmp_path):
        def boom(oid, msgs):
            raise RuntimeError("no payload")
        with diagnostics.collecting("costco", "p", root=tmp_path) as d:
            diagnostics.report_unreadable_rows([_row(quantity=None)], evidence=boom)
        assert len(d.problems) == 1

    def test_problem_count_and_problems_since(self, tmp_path):
        assert diagnostics.problem_count() == 0 and diagnostics.problems_since(0) == []
        with diagnostics.collecting("amazon", "p", root=tmp_path):
            before = diagnostics.problem_count()
            diagnostics.problem("one")
            diagnostics.problem("two")
            assert diagnostics.problems_since(before) == ["one", "two"]
            assert diagnostics.problem_count() == 2


class TestCostcoCallSite:
    def _scraper(self, monkeypatch, details):
        from scrapers import costco
        import scrapers.costco_api as api

        class _Client:
            def __init__(self, *a, **k):
                pass

            def list_order_numbers(self, start, end):
                return [d["orderNumber"] for d in details]

            def get_order_details(self, numbers):
                return details

        monkeypatch.setattr(api, "CostcoApiClient", _Client)
        scraper = costco.CostcoScraper(ProfileConfig(label="p", profile_id="x", retailers=["costco"]), lookback_days=1)
        monkeypatch.setattr(scraper, "_load_order_state", lambda: _EMPTY_STATE)
        return scraper

    def test_a_line_without_a_price_records_a_blank_cost_and_attaches_the_payload(self, monkeypatch, tmp_path):
        details = json.loads(_COSTCO_FIXTURE.read_text(encoding="utf-8"))
        detail = copy.deepcopy(next(d for d in details if d["orderNumber"] == "1399000006"))
        for shipto in detail["shipToAddress"]:
            for line in shipto["orderLineItems"]:
                line["price"] = None
        scraper = self._scraper(monkeypatch, [detail])
        with diagnostics.collecting("costco", "p", root=tmp_path) as d:
            items = scraper._scrape_via_api()
        row = next(r for r in items if r.order_id == "1399000006")
        assert row.cost_per_item is None and row.total_cost is None      # never a $0 cost
        assert any("1399000006" in p and "Cost Per Item could not be read" in p
                   and "orderLineItems[].price" in p for p in d.problems)
        assert d.responses and d.responses[0]["label"] == "getOrderDetails order 1399000006 (unreadable cells)"

    def test_a_complete_payload_reports_nothing(self, monkeypatch, tmp_path):
        details = json.loads(_COSTCO_FIXTURE.read_text(encoding="utf-8"))
        scraper = self._scraper(monkeypatch, details)
        with diagnostics.collecting("costco", "p", root=tmp_path) as d:
            items = scraper._scrape_via_api()
        assert items
        assert d.problems == [] and d.responses == []

    def test_a_missing_order_date_is_a_shape_error_with_the_payload(self, monkeypatch, tmp_path):
        from scrapers.costco_api import CostcoApiError
        details = json.loads(_COSTCO_FIXTURE.read_text(encoding="utf-8"))
        detail = copy.deepcopy(details[0])
        detail.pop("orderPlacedDate", None)
        scraper = self._scraper(monkeypatch, [detail])
        with diagnostics.collecting("costco", "p", root=tmp_path) as d:
            with pytest.raises(CostcoApiError, match="orderPlacedDate"):
                scraper._scrape_via_api()
        assert d.responses and d.responses[0]["label"].startswith("getOrderDetails order (shape)")


class TestBestBuyCallSite:
    def _scraper(self, monkeypatch, payloads):
        from scrapers import bestbuy
        import scrapers.bestbuy_api as api

        class _Client:
            def __init__(self, *a, **k):
                pass

            def fetch_order_payloads(self, since, open_ids, terminal_ids):
                return payloads

        monkeypatch.setattr(api, "BestBuyApiClient", _Client)
        scraper = bestbuy.BestBuyScraper(ProfileConfig(label="p", profile_id="x", retailers=["bestbuy"]), lookback_days=1)
        monkeypatch.setattr(scraper, "_load_order_state", lambda: _EMPTY_STATE)
        return scraper

    def test_an_item_without_a_unit_price_is_reported_with_its_payload(self, monkeypatch, tmp_path):
        payloads = json.loads(_BB_FIXTURE.read_text(encoding="utf-8"))
        payload = copy.deepcopy(next(p for p in payloads if p["order"]["userOrderId"] == "BBY01-809900000004"))
        for item in payload["order"]["items"]:
            item.setdefault("price", {})["unitCurrentPrice"] = None
        scraper = self._scraper(monkeypatch, [payload])
        with diagnostics.collecting("bestbuy", "p", root=tmp_path) as d:
            items = scraper._scrape_via_api()
        row = next(r for r in items if r.order_id == "BBY01-809900000004")
        assert row.cost_per_item is None and row.quantity == 7           # the row is still recorded
        assert any("BBY01-809900000004" in p and "Cost Per Item could not be read" in p
                   and "unitCurrentPrice" in p for p in d.problems)
        assert d.responses and d.responses[0]["label"] == "ss-api order BBY01-809900000004 (unreadable cells)"

    def test_a_missing_created_date_is_a_shape_error_with_the_payload(self, monkeypatch, tmp_path):
        from scrapers.bestbuy_api import BestBuyApiError
        payloads = json.loads(_BB_FIXTURE.read_text(encoding="utf-8"))
        payload = copy.deepcopy(payloads[0])
        payload["order"].pop("created", None)
        scraper = self._scraper(monkeypatch, [payload])
        with diagnostics.collecting("bestbuy", "p", root=tmp_path) as d:
            with pytest.raises(BestBuyApiError, match="order.created"):
                scraper._scrape_via_api()
        assert d.responses and d.responses[0]["label"].startswith("ss-api order payload (shape)")
