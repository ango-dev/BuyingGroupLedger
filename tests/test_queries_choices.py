"""The choice columns' previous answers (web/queries.choice_values, 2026-09-18)."""
from models.order import FIELDNAMES
from web.ledger_reader import LedgerRow
from web.queries import CHOICE_FIELDS, choice_values


def row(**values):
    cells = {f: "" for f in FIELDNAMES}
    cells.update(values)
    return LedgerRow(cells=cells, row_number=2)


def test_most_used_first_then_alphabetical_and_blanks_skipped():
    rows = [row(profile_label="b", retailer="Costco"), row(profile_label="a", retailer="Costco"),
            row(profile_label="a", retailer=""), row(profile_label="  ", retailer="Best Buy")]
    values = choice_values(rows)
    assert set(values) == set(CHOICE_FIELDS)
    assert values["profile_label"] == ["a", "b"]
    assert values["retailer"] == ["Costco", "Best Buy"]
    assert values["status"] == [] and values["tracking_submitted"] == []


def test_the_choice_columns_are_editable_text_columns():
    from web.ledger_writer import EDITABLE_FIELDS
    assert all(f in EDITABLE_FIELDS for f in CHOICE_FIELDS)
