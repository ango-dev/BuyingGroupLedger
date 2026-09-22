"""The choice columns' previous answers (web/queries.choice_values, 2026-09-18)."""
from models.order import FIELDNAMES
from web.ledger_reader import LedgerRow
from web.queries import CHOICE_FIELDS, SUGGEST_FIELDS, choice_values


def row(**values):
    cells = {f: "" for f in FIELDNAMES}
    cells.update(values)
    return LedgerRow(cells=cells, row_number=2)


def test_most_used_first_then_alphabetical_and_blanks_skipped():
    rows = [row(profile_label="b", retailer="Costco"), row(profile_label="a", retailer="Costco"),
            row(profile_label="a", retailer=""), row(profile_label="  ", retailer="Best Buy")]
    values = choice_values(rows)
    assert set(values) == set(SUGGEST_FIELDS) and set(CHOICE_FIELDS) < set(SUGGEST_FIELDS)  # every box suggests (2026-09-21)
    assert values["profile_label"] == ["a", "b"]
    assert values["retailer"] == ["Costco", "Best Buy"]
    assert values["status"] == [] and "tracking_submitted" not in values


def test_the_choice_columns_are_editable_text_columns():
    from web.ledger_writer import EDITABLE_FIELDS
    assert all(f in EDITABLE_FIELDS for f in CHOICE_FIELDS)


def test_card_pairs_come_from_the_rows_and_the_settings_most_used_first():
    from types import SimpleNamespace

    from web.queries import card_pairs, cell_choices

    rows = [row(card_name="Chase Prime Visa", card_last4="0315"), row(card_name="Chase Prime Visa", card_last4="0315"),
            row(card_name="Venmo Visa", card_last4="4351"), row(card_name="Chase Prime Visa", card_last4="1111"),
            row(card_name="", card_last4="9999"), row(card_name="Lonely", card_last4="")]
    cards = [SimpleNamespace(name="Amex Blue", last4="4331"), SimpleNamespace(name="Chase Prime Visa", last4="0315")]
    assert card_pairs(rows, cards) == [["Chase Prime Visa", "0315"], ["Chase Prime Visa", "1111"],
                                       ["Venmo Visa", "4351"], ["Amex Blue", "4331"]]
    bundle = cell_choices(rows, cards)
    assert set(bundle) == {"values", "card_pairs"} and bundle["values"]["card_last4"][0] == "0315"
