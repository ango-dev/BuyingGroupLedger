"""Offline tests for scripts/backfill_gift_cards.py's pure planning half.

The fetch half drives a real CDP browser and is exercised live; what must be provable for free is
WHICH orders get fetched (only Amazon rows with a blank Gift Card cell, cancelled rows never) and
what `plan_order_writes` decides — especially the legacy-netted CONVERSION, where getting the
arithmetic wrong double-nets a gift card out of COGS.
"""

from scripts.backfill_gift_cards import collect_candidates, plan_order_writes
from sheets.ledger_sync import HEADER


def row(**values):
    return [str(values.get(h, "")) for h in HEADER]


def grid(*rows):
    return [list(HEADER), *rows]


BASE = {"Retailer": "Amazon", "Profile": "p1", "Status": "delivered"}


def prow(n, cost, qty=1, shipping=0.0, gc_blank=True, tax_blank=True):
    return {"n": n, "cost": cost, "quantity": qty, "shipping": shipping,
            "gc_blank": gc_blank, "tax_blank": tax_blank}


# --- collect_candidates --------------------------------------------------------------------------
def test_only_orders_with_a_blank_gift_card_cell_are_candidates():
    g = grid(
        row(**BASE, **{"Order ID": "A1", "Total Cost": "100", "Gift Card": ""}),
        row(**BASE, **{"Order ID": "A2", "Total Cost": "50", "Gift Card": "10"}),  # already filled
        row(**{**BASE, "Retailer": "Best Buy"}, **{"Order ID": "B1", "Total Cost": "70"}),
    )
    out = collect_candidates(g, None, set())
    assert set(out) == {("Amazon", "p1")}
    assert set(out[("Amazon", "p1")]) == {"A1"}


def test_all_of_an_orders_rows_ride_along_for_the_cost_split():
    # A2's first row already has its share, but the split weights need EVERY row of the order.
    g = grid(
        row(**BASE, **{"Order ID": "A2", "Total Cost": "100", "Gift Card": "10"}),
        row(**BASE, **{"Order ID": "A2", "Total Cost": "300", "Gift Card": ""}),
    )
    out = collect_candidates(g, None, set())
    assert [r["cost"] for r in out[("Amazon", "p1")]["A2"]] == [100.0, 300.0]
    assert [r["gc_blank"] for r in out[("Amazon", "p1")]["A2"]] == [False, True]


def test_cancelled_rows_never_count():
    g = grid(row(**{**BASE, "Status": "cancelled"}, **{"Order ID": "A1", "Total Cost": "100"}))
    assert collect_candidates(g, None, set()) == {}


def test_retailer_and_order_filters():
    g = grid(
        row(**BASE, **{"Order ID": "A1", "Total Cost": "100"}),
        row(**{**BASE, "Retailer": "Amazon Business"}, **{"Order ID": "AB1", "Total Cost": "60"}),
    )
    assert set(collect_candidates(g, "amazon-business", set())) == {("Amazon Business", "p1")}
    assert set(collect_candidates(g, None, {"A1"})) == {("Amazon", "p1")}


# --- plan_order_writes ---------------------------------------------------------------------------
def test_an_unparsed_summary_means_no_writes_at_all():
    writes, note = plan_order_writes([prow(2, 100.0)], None, 5.0, 100.0)
    assert writes == [] and "no parsed summary" in note


def test_a_detected_no_gift_card_fills_real_zeros():
    # A detected 0 is a VALUE: "checked, none" beats "unknown".
    writes, note = plan_order_writes([prow(2, 60.0), prow(3, 40.0)], 0.0, 5.0, 100.0)
    assert "no gift card" in note
    assert [(w["n"], w["field"], w["value"]) for w in writes] == [
        (2, "gift_card", 0.0), (2, "sales_tax", 3.0),
        (3, "gift_card", 0.0), (3, "sales_tax", 2.0),
    ]


def test_a_gross_row_gets_its_shares_only():
    writes, _ = plan_order_writes([prow(2, 60.0), prow(3, 40.0)], 50.0, 8.0, 100.0)
    assert [(w["n"], w["field"], w["value"]) for w in writes] == [
        (2, "gift_card", 30.0), (2, "sales_tax", 4.8),
        (3, "gift_card", 20.0), (3, "sales_tax", 3.2),
    ]


def test_a_legacy_netted_row_is_converted_to_gross_plus_gift_card():
    """The real row 60: sticker 3 x 99.00 = 297, gc 200, old scaling left cost 96.99. The
    conversion restores unit 99.00 / total 297.00 and fills gc 200 -- the SAME batch, so the live
    COGS formula nets straight back to (297 - 200) x (1 - rate)."""
    writes, note = plan_order_writes([prow(60, 96.99, qty=3)], 200.0, 0.0, 297.0)
    assert "CONVERTED" in note
    assert {(w["field"], w["value"]) for w in writes} == {
        ("cost_per_item", 99.0), ("total_cost", 297.0), ("gift_card", 200.0), ("sales_tax", 0.0),
    }
    # The cost writes are expect-guarded (the cell must still hold the netted number at write time).
    assert all(w["expect"] for w in writes if w["field"] in ("cost_per_item", "total_cost"))


def test_conversion_splits_a_multi_row_order_proportionally():
    # Sticker 100 + 2x100 = 300, gc 200: the old factor 1/3 scaled the rows to 33.33 and 66.66
    # (unit-rounded), and the inversion apportions the subtotal by those same proportions.
    writes, note = plan_order_writes(
        [prow(2, 33.33, qty=1), prow(3, 66.66, qty=2)], 200.0, None, 300.0)
    assert "CONVERTED" in note
    by = {(w["n"], w["field"]): w["value"] for w in writes}
    assert by[(2, "total_cost")] == 100.0 and by[(3, "total_cost")] == 200.0
    assert by[(3, "cost_per_item")] == 100.0
    assert by[(2, "gift_card")] + by[(3, "gift_card")] == 200.0


def test_a_legacy_row_with_shipping_is_refused_not_guessed():
    writes, note = plan_order_writes([prow(2, 90.0, shipping=5.0)], 10.0, 0.0, 100.0)
    assert writes == [] and "Shipping" in note


def test_a_cost_matching_neither_shape_is_refused():
    writes, note = plan_order_writes([prow(2, 77.77)], 10.0, 0.0, 100.0)
    assert writes == [] and "neither" in note


def test_an_already_filled_cell_is_not_rewritten():
    writes, _ = plan_order_writes([prow(2, 100.0, gc_blank=False, tax_blank=False)], 40.0, 1.0, 100.0)
    assert writes == []
