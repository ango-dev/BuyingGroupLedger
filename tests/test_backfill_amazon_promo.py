"""The one-off backfill that re-reads Amazon order pages to correct rows already on the sheet.

Terminal rows are never re-scraped, so an order that closed before the promo / gift-card rules
existed still carries the pre-promo rate and full sticker cost. Only `plan_amazon_backfill` is
exercised here: it is deliberately pure so the risky half (what would be WRITTEN to the live sheet)
can be tested with no browser and no network. The fetch half is a thin CDP loop over production
parsers, proven by the scrapers' own tests.
"""

from models.order import OrderItem
from scripts.backfill_amazon_promo import plan_amazon_backfill
from sheets.ledger_sync import HEADER


def sheet_row(**values):
    return [values.get(name, "") for name in HEADER]


def rebuilt_item(order_id="A1", order_date="2026-08-12", item_name="iPad", shipment="1", **kwargs):
    return OrderItem(
        retailer="Amazon", order_id=order_id, order_date=order_date, item_name=item_name,
        shipment=shipment, **kwargs,
    )


def keyed(*items):
    from scripts.backfill_amazon_promo import _key

    return {_key(i.order_id, i.order_date, i.item_name, i.shipment): i for i in items}


def plan(rows, rebuilt):
    return plan_amazon_backfill(list(HEADER), rows, rebuilt)


class TestChanges:
    def test_a_stale_rate_is_reported(self):
        # The order page carried "extra 1% back", so the row should read 0.06, not the card's 0.05.
        rows = [sheet_row(**{"Order ID": "A1", "Order Date": "2026-08-12", "Item Name": "iPad",
                             "Shipment": 1, "Cashback Rate": 0.05})]
        p = plan(rows, keyed(rebuilt_item(cashback_rate=0.06)))

        assert (2, "Cashback Rate", 0.05, 0.06) in p["changes"]
        assert p["orders"] == ["A1"]

    def test_gift_card_netted_cost_is_reported(self):
        rows = [sheet_row(**{"Order ID": "A1", "Order Date": "2026-08-12", "Item Name": "iPad",
                             "Shipment": 1, "Cost Per Item": 100.0, "Total Cost": 100.0})]
        p = plan(rows, keyed(rebuilt_item(quantity=1, cost_per_item=60.0)))

        cols = {c[1]: (c[2], c[3]) for c in p["changes"]}
        assert cols["Cost Per Item"] == (100.0, 60.0)
        assert cols["Total Cost"] == (100.0, 60.0)  # derived from qty * cost by the model

    def test_an_already_correct_row_is_left_alone(self):
        rows = [sheet_row(**{"Order ID": "A1", "Order Date": "2026-08-12", "Item Name": "iPad",
                             "Shipment": 1, "Cashback Rate": 0.06, "Cost Per Item": 60.0,
                             "Total Cost": 60.0, "Shipping": 0.0})]
        p = plan(rows, keyed(rebuilt_item(cashback_rate=0.06, quantity=1, cost_per_item=60.0,
                                          shipping=0.0)))

        assert p["changes"] == []
        assert p["orders"] == []

    def test_percent_and_currency_formatted_cells_are_compared_numerically(self):
        # A hand-formatted cell can read back as "5%" / "$100.00"; that is the same number, and
        # rewriting it would be churn (the same trap backfill_profit_columns._same_rate guards).
        rows = [sheet_row(**{"Order ID": "A1", "Order Date": "2026-08-12", "Item Name": "iPad",
                             "Shipment": 1, "Cashback Rate": "6%", "Cost Per Item": "$60.00"})]
        p = plan(rows, keyed(rebuilt_item(cashback_rate=0.06, quantity=1, cost_per_item=60.0)))

        assert all(c[1] not in ("Cashback Rate", "Cost Per Item") for c in p["changes"])


class TestDoesNotClobber:
    def test_a_rebuilt_blank_never_overwrites_a_recorded_value(self):
        # The page didn't say (e.g. no unit price parsed). Same rule as ledger_sync._merge_row.
        rows = [sheet_row(**{"Order ID": "A1", "Order Date": "2026-08-12", "Item Name": "iPad",
                             "Shipment": 1, "Cost Per Item": 100.0, "Cashback Rate": 0.05})]
        p = plan(rows, keyed(rebuilt_item(cost_per_item=None, cashback_rate=None)))

        assert p["changes"] == []

    def test_only_the_money_columns_are_ever_touched(self):
        rows = [sheet_row(**{"Order ID": "A1", "Order Date": "2026-08-12", "Item Name": "iPad",
                             "Shipment": 1, "Status": "paid", "Actual Payout": 999,
                             "Cashback Rate": 0.05})]
        p = plan(rows, keyed(rebuilt_item(cashback_rate=0.06, status="delivered")))

        assert {c[1] for c in p["changes"]} == {"Cashback Rate"}, "status/payout must be untouched"

    def test_rows_for_other_orders_are_ignored(self):
        rows = [
            sheet_row(**{"Order ID": "OTHER", "Order Date": "2026-08-12", "Item Name": "iPad",
                         "Shipment": 1, "Cashback Rate": 0.01}),
            sheet_row(**{"Order ID": "A1", "Order Date": "2026-08-12", "Item Name": "iPad",
                         "Shipment": 1, "Cashback Rate": 0.05}),
        ]
        p = plan(rows, keyed(rebuilt_item(cashback_rate=0.06)))

        assert [c[0] for c in p["changes"]] == [3], "only the matched row, at its real sheet row"

    def test_a_blank_order_id_row_is_skipped(self):
        p = plan([sheet_row(**{"Order ID": "", "Cashback Rate": 0.05})],
                 keyed(rebuilt_item(cashback_rate=0.06)))

        assert p["changes"] == []


class TestKeyMatching:
    def test_numeric_shipment_from_an_unformatted_read_still_matches(self):
        """The sheet stores Shipment as a NUMBER, so an unformatted read yields 1 or 1.0 while the
        model holds "1". A mismatch here would quietly report 'nothing to change'."""
        for stored in (1, 1.0, "1"):
            rows = [sheet_row(**{"Order ID": "A1", "Order Date": "2026-08-12", "Item Name": "iPad",
                                 "Shipment": stored, "Cashback Rate": 0.05})]
            p = plan(rows, keyed(rebuilt_item(cashback_rate=0.06)))

            assert p["changes"], f"shipment stored as {stored!r} should still match"

    def test_a_rebuilt_row_absent_from_the_sheet_is_surfaced(self):
        # e.g. an order that split after it was recorded — the new shipment has no row yet.
        p = plan([], keyed(rebuilt_item(shipment="2", cashback_rate=0.06)))

        assert p["changes"] == []
        assert p["unmatched"] == [("A1", "2026-08-12", "iPad", "2")]
