"""scripts/backfill_promo_rate: Promo Rate for old rows from the numbers on the ledger, offline.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger.sync import HEADER  # noqa: E402
from models.card import Card  # noqa: E402
from scripts.backfill_promo_rate import plan_promo_backfill, promo_step  # noqa: E402

CARDS = [Card(last4="5555", name="ABP", cashback_rate="1%", retailer_rates={"Amazon": "5%", "Amazon Business": "5%"})]


def sheet_row(**values):
    base = {"Retailer": "Amazon", "Order ID": "A1", "Order Date": "2026-03-01", "Item Name": "Widget",
            "Shipment": "1", "Card Last 4": "5555", "Cashback Rate": 0.05, "Promo Rate": "", "Profile": "p"}
    base.update(values)
    return [base.get(h, "") for h in HEADER]


def plan(*rows, protected=None):
    return plan_promo_backfill(list(HEADER), list(rows), CARDS, protected)


class TestPromoStep:
    def test_half_percent_steps_up_to_ten_percent(self):
        assert promo_step(0.01) == 0.01 and promo_step(0.005) == 0.005 and promo_step(0.1) == 0.1
        assert promo_step(0.0100000001) == 0.01  # float noise from a subtraction

    def test_anything_else_is_not_a_promo(self):
        assert promo_step(0.0) is None and promo_step(-0.01) is None
        assert promo_step(0.004) is None and promo_step(0.11) is None


class TestThePlan:
    def test_the_difference_above_the_configured_rate_moves_into_promo_rate(self):
        p = plan(sheet_row(**{"Cashback Rate": 0.06}), sheet_row(**{"Order ID": "A2", "Cashback Rate": 0.05}))
        assert p["will_write"] == [(2, 0.01)] and p["unexplained"] == []

    def test_a_percent_text_cell_reads_as_a_number(self):
        assert plan(sheet_row(**{"Cashback Rate": "7%"}))["will_write"] == [(2, 0.02)]

    def test_only_amazon_rows_and_only_blank_promo_cells(self):
        p = plan(sheet_row(**{"Retailer": "Best Buy", "Cashback Rate": 0.06}),
                 sheet_row(**{"Order ID": "A2", "Cashback Rate": 0.06, "Promo Rate": 0.01}),
                 sheet_row(**{"Order ID": "A3", "Retailer": "Amazon Business", "Cashback Rate": 0.06}))
        assert p["will_write"] == [(4, 0.01)]

    def test_an_odd_difference_is_listed_not_guessed(self):
        p = plan(sheet_row(**{"Cashback Rate": 0.09}),      # 4% above: a promo step, filled
                 sheet_row(**{"Order ID": "A2", "Cashback Rate": 0.053}),   # 0.3% above: not a step
                 sheet_row(**{"Order ID": "A3", "Cashback Rate": 0.2}))     # 15% above: too much
        assert p["will_write"] == [(2, 0.04)]
        assert [(n, o) for n, o, _c, _r in p["unexplained"]] == [(3, "A2"), (4, "A3")]

    def test_a_hand_typed_rate_an_unconfigured_card_and_a_lower_rate_are_left(self):
        protected = {("A1", "2026-03-01", "Widget", "1"): {"cashback_rate"}}
        p = plan(sheet_row(**{"Cashback Rate": 0.06}),
                 sheet_row(**{"Order ID": "A2", "Card Last 4": "1111", "Cashback Rate": 0.06}),
                 sheet_row(**{"Order ID": "A3", "Cashback Rate": 0.04}),
                 protected=protected)
        assert p["will_write"] == [] and p["skipped_hand"] == [2] and p["unexplained"] == []
