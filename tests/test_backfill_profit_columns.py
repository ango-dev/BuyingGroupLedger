"""The one-off backfill of Card / Cashback Rate / Total Profit onto rows already on the sheet.

Those columns are derived at SCRAPE time, and a delivered/cancelled row is terminal — never re-read —
so without this they stay blank forever. The rules that matter: it must reproduce exactly what a scrape
would have written, and it must not overwrite anything typed by hand unless explicitly asked to.
"""

from models.card import Card
from scripts.backfill_profit_columns import plan_profit_backfill
from sheets.ledger_sync import HEADER

CARDS = [
    Card(last4="4321", name="Amex Business Gold", cashback_rate=0.01,
         retailer_rates={"Best Buy": 0.04}),
    Card(last4="4335", name="Venmo Visa", cashback_rate=0.03),
]


def sheet_row(**values):
    return [str(values.get(name, "")) for name in HEADER]


def plan(*rows, refresh=False):
    return plan_profit_backfill(list(HEADER), list(rows), CARDS, default_rate=0.0, refresh=refresh)


class TestFills:
    def test_blank_card_columns_are_filled_from_the_recorded_last4(self):
        p = plan(sheet_row(**{"Order ID": "A1", "Card Last 4": "4321", "Retailer": "Best Buy"}))

        assert p["fills"] == [
            (2, "Card", "", "Amex Business Gold"),
            (2, "Cashback Rate", "", 0.04),  # the Best Buy retailer override, not the 1% base
        ]
        assert p["changes"] == []

    def test_retailer_override_is_applied_per_row(self):
        # The same card on a different retailer resolves to its overall rate.
        p = plan(sheet_row(**{"Order ID": "A1", "Card Last 4": "4321", "Retailer": "Costco"}))

        assert (2, "Cashback Rate", "", 0.01) in p["fills"]

    def test_masked_digits_still_resolve(self):
        p = plan(sheet_row(**{"Order ID": "A1", "Card Last 4": "************4321",
                              "Retailer": "Best Buy"}))

        assert any(f[3] == "Amex Business Gold" for f in p["fills"])

    def test_row_without_an_order_id_is_skipped_entirely(self):
        # Same rule as sync_csv_to_sheet — it isn't a real ledger row.
        p = plan(sheet_row(**{"Card Last 4": "4321"}))

        assert p["fills"] == []
        assert p["formula_rows"] == []

    def test_row_without_a_recorded_card_is_counted_not_guessed(self):
        p = plan(sheet_row(**{"Order ID": "A1", "Card Last 4": ""}))

        assert p["fills"] == []
        assert p["skipped_no_last4"] == 1
        assert p["formula_rows"] == [2], "it still gets a profit formula"

    def test_unconfigured_card_gets_the_default_rate_and_no_name(self):
        # Matches what a live scrape writes (config.cards.tag_cards): the name stays blank so the gap
        # stays visible, and the default rate is written EXPLICITLY — an explicit 0 says "this card
        # earns nothing", where a blank cell is indistinguishable from "not yet backfilled".
        p = plan(sheet_row(**{"Order ID": "A1", "Card Last 4": "9999", "Retailer": "Costco"}))

        assert p["unresolved"] == [(2, "9999")]
        assert p["fills"] == [(2, "Cashback Rate", "", 0.0)]
        assert all(f[1] != "Card" for f in p["fills"]), "no name is invented for an unknown card"


class TestDoesNotClobber:
    def test_hand_typed_card_name_is_reported_but_not_written(self):
        p = plan(sheet_row(**{"Order ID": "A1", "Card Last 4": "4321", "Retailer": "Best Buy",
                              "Card": "My Own Label"}))

        assert (2, "Card", "My Own Label", "Amex Business Gold") in p["changes"]
        assert all(f[1] != "Card" for f in p["fills"])
        assert all(c[1] != "Card" for c in p["will_write"]), "not written without --refresh"

    def test_refresh_opts_in_to_correcting_a_stale_rate(self):
        row = sheet_row(**{"Order ID": "A1", "Card Last 4": "4321", "Retailer": "Best Buy",
                           "Cashback Rate": "0.02"})

        assert (2, "Cashback Rate", "0.02", 0.04) in plan(row)["changes"]
        assert (2, "Cashback Rate", "0.02", 0.04) in plan(row, refresh=True)["will_write"]

    def test_already_correct_cells_are_not_rewritten(self):
        p = plan(sheet_row(**{"Order ID": "A1", "Card Last 4": "4321", "Retailer": "Best Buy",
                              "Card": "Amex Business Gold", "Cashback Rate": "0.04"}))

        assert p["fills"] == [] and p["changes"] == []

    def test_equivalent_rate_spellings_count_as_equal(self):
        # "0.0300" and 0.03 are the same rate; rewriting it would be churn.
        p = plan(sheet_row(**{"Order ID": "A1", "Card Last 4": "4335", "Cashback Rate": "0.0300"}))

        assert all(c[1] != "Cashback Rate" for c in p["changes"])

    def test_percent_formatted_cell_is_not_mistaken_for_a_disagreement(self):
        # Found live: the Cashback Rate column is percent-FORMATTED, so a formatted read returns the
        # display text "3%" for a cell whose real value is 0.03. Treating that as a disagreement would
        # flag every already-correct row and make --refresh rewrite 0.03 on top of 0.03.
        p = plan(sheet_row(**{"Order ID": "A1", "Card Last 4": "4335", "Cashback Rate": "3%"}))

        assert p["changes"] == []
        assert all(f[1] != "Cashback Rate" for f in p["fills"])

    def test_a_folded_in_amazon_promo_is_not_reverted(self):
        # An Amazon row's rate can legitimately EXCEED cards.json: the order page's "extra 1% back" is
        # summed into the same cell. Without the tolerance, --refresh would quietly undo every promo
        # and check_card_and_rate_coverage would flag them forever.
        p = plan(sheet_row(**{"Order ID": "A1", "Card Last 4": "4335", "Retailer": "Amazon",
                              "Cashback Rate": "0.04"}))  # 0.03 card + 0.01 promo

        assert p["changes"] == []
        assert all(c[1] != "Cashback Rate" for c in p["will_write"])

    def test_a_rate_below_the_configured_one_is_still_a_disagreement(self):
        # The tolerance is one-directional — a stale/too-low rate is still real drift to report.
        p = plan(sheet_row(**{"Order ID": "A1", "Card Last 4": "4335", "Cashback Rate": "0.01"}))

        assert (2, "Cashback Rate", "0.01", 0.03) in p["changes"]

    def test_an_implausibly_large_excess_is_still_a_disagreement(self):
        # Well beyond any real promo — that's a mis-typed rate, not a bonus.
        p = plan(sheet_row(**{"Order ID": "A1", "Card Last 4": "4335", "Cashback Rate": "0.50"}))

        assert (2, "Cashback Rate", "0.50", 0.03) in p["changes"]


class TestFormulaRows:
    def test_every_real_row_gets_a_formula_even_with_no_payout(self):
        # The formula reads blank until Payout Amount is filled, so stamping early costs nothing and
        # means the number appears the moment a payout is entered.
        p = plan(
            sheet_row(**{"Order ID": "A1", "Card Last 4": "4321"}),
            sheet_row(**{"Order ID": "A2", "Card Last 4": ""}),
            sheet_row(**{"Order ID": ""}),
        )

        assert p["formula_rows"] == [2, 3]
