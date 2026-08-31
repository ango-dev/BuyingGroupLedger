"""Offline tests for scripts/backfill_gift_cards.py's pure planning half.

The fetch half drives a real CDP browser and is exercised live; what must be provable for free is
WHICH orders get fetched (only Amazon rows with a blank Gift Card cell, cancelled rows never) and
that the whole-order row list rides along for the cost-weighted split.
"""

from scripts.backfill_gift_cards import collect_candidates
from sheets.ledger_sync import HEADER


def row(**values):
    return [str(values.get(h, "")) for h in HEADER]


def grid(*rows):
    return [list(HEADER), *rows]


BASE = {"Retailer": "Amazon", "Profile": "p1", "Status": "delivered"}


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
    assert [c for _n, c in out[("Amazon", "p1")]["A2"]] == [100.0, 300.0]


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
