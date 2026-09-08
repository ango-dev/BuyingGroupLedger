"""Offline tests for scripts/audit_sheet.py.

THE POINT OF THE FAKE IN THIS FILE. The auditor's whole job is to notice disagreements between the
three Google Sheets render modes, so a fake that returns the same grid for every mode would make
every render-mode check "pass" while being structurally incapable of representing a single bug those
checks exist to find. tests/test_ledger_sync.py's FakeWorksheet is exactly that: its `get_values`
ignores `value_render_option` entirely. That is also the shape of a bug that already bit this project
once — the design notes records `FakeWorksheet.get_all_values()` handing back raw Python types instead of
gspread's always-string formatted read, which produced a false "duplicate row" signal.

So here a test never writes the three grids by hand. It declares what each cell STORES (`Cell`), and
one renderer derives all three grids using Google's real rules. A test physically cannot claim a cell
is an int in the unformatted grid *and* an int in the formatted grid, because it never writes the
formatted grid. TestTheFakeMatchesRealGspread pins the renderer itself, so when the fake drifts, that
class fails rather than a downstream check quietly going green.
"""

from dataclasses import dataclass
from datetime import date
from typing import Any

import pytest

from models.order import FIELDNAMES
from scripts import audit_sheet
from scripts.audit_sheet import Grids, Options, Sheet, run_checks
from sheets.ledger_sync import HEADER, _COL, _cogs_formula, _profit_formula
from tests.test_ledger_sync import _as_sheet_text

_SHEETS_EPOCH = date(1899, 12, 30)


def serial(iso: str) -> int:
    """The date serial Google Sheets stores for an ISO date (epoch 1899-12-30)."""
    y, m, d = (int(p) for p in iso.split("-"))
    return (date(y, m, d) - _SHEETS_EPOCH).days


# --------------------------------------------------------------------------------------------------
# The cell model and the renderer — one source of truth for all three grids
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Cell:
    """What a sheet cell actually stores, plus how it's displayed.

    value    the stored value (for a formula cell: the formula's evaluated RESULT)
    fmt      None | "percent" | "currency" | "date" — a display format, the user's own choice
    formula  the literal "=..." text when the cell holds a formula
    """

    value: Any = ""
    fmt: str | None = None
    formula: str | None = None


# gspread's ValueRenderOption values, mapped to the mode names used below.
_MODES = {
    "FORMATTED_VALUE": "formatted",
    "UNFORMATTED_VALUE": "unformatted",
    "FORMULA": "formula",
}


def render(cell: Cell, mode: str):
    """Derive one render mode's value from a stored cell, following gspread/Sheets' real rules."""
    mode = _MODES.get(mode, mode)
    if mode == "formatted":
        # ALWAYS a string. A formula cell shows its RESULT, not its text.
        if cell.value == "" or cell.value is None:
            return ""
        if cell.fmt == "percent":
            return f"{cell.value * 100:g}%"
        if cell.fmt == "currency":
            return f"${cell.value:,.2f}"
        if cell.fmt == "currency0":   # a 0-decimal currency format: destroys cents on display
            return f"${cell.value:,.0f}"
        if cell.fmt == "percent0":    # a 0-decimal percent format: 0.0375 shows as 4%
            return f"{cell.value * 100:.0f}%"
        if cell.fmt == "hidden":      # a custom format like ;;; that renders the number as nothing
            return ""
        if cell.fmt == "date":
            d = _SHEETS_EPOCH + __import__("datetime").timedelta(days=int(cell.value))
            return f"{d.month}/{d.day}/{d.year}"
        return _as_sheet_text(cell.value)
    if mode == "unformatted":
        # Real types. A formula cell yields its evaluated result.
        return cell.value
    if mode == "formula":
        # The literal formula text; a date cell still yields its serial.
        return cell.formula if cell.formula is not None else cell.value
    raise AssertionError(f"unknown render mode {mode!r}")


class RenderedFakeWorksheet:
    """A worksheet whose three render modes genuinely differ, and which refuses a sloppy read."""

    title = "Orders"

    def __init__(self, grid: list[list[Cell]]):
        self.grid = grid
        self.reads: list[str] = []

    def get_all_values(self):
        raise AssertionError(
            "the auditor must never use get_all_values() — it's the FORMATTED read, and the design notes "
            "is explicit that a new sheet reader picking it reinherits the corruption bug"
        )

    def get_values(self, range_name=None, value_render_option=None, **kwargs):
        if value_render_option is None:
            raise AssertionError("every audit read must pass an explicit value_render_option")
        mode = str(getattr(value_render_option, "value", value_render_option))
        self.reads.append(mode)
        return [[render(c, mode) for c in row] for row in self.grid]


# --------------------------------------------------------------------------------------------------
# Building a realistic ledger row
# --------------------------------------------------------------------------------------------------


def header_cells() -> list[Cell]:
    return [Cell(name) for name in HEADER]


def row_cells(row_number: int, **overrides) -> list[Cell]:
    """A well-formed ledger row; pass column names to override individual cells with a Cell or value."""
    base = {
        "Order Date": Cell("2026-08-06"),
        "Status": Cell("delivered"),
        "Profile": Cell("profile-alpha"),
        "Retailer": Cell("Best Buy"),
        "Item Name": Cell("ASUS Vivobook 15"),
        "Quantity": Cell(2),
        "Order ID": Cell(f"BBY01-{row_number:09d}"),
        "Tracking Number": Cell(f"1Z999AA{row_number:08d}"),
        "Shipment": Cell(1),
        "Delivery Date": Cell("2026-08-09"),
        "Cost Per Item": Cell(399.0),
        "Shipping": Cell(0.0),
        "Total Cost": Cell(798.0),
        "Card": Cell("Amex Business Gold"),
        "Cashback Rate": Cell(0.04, fmt="percent"),
        "Insurance": Cell("", fmt="currency"),
        "Payout Amount": Cell("", fmt="currency"),
        "Payout Date": Cell(""),
        "COGS": Cell(798.0, fmt="currency", formula=_cogs_formula(row_number)),
        "Total Profit": Cell("", formula=_profit_formula(row_number)),
        "Buying Group": Cell("BFMR"),
        "Order Link": Cell("https://www.bestbuy.com/order/1"),
        "Tracking Link": Cell("https://www.ups.com/track"),
        "Delivery Address": Cell("1 Example Way, Testville TS 00000"),
        "Card Last 4": Cell("0315"),
        "Last Scraped At": Cell("2026-08-12T06:00:00Z"),
        "Tracking Submitted": Cell(True),
        "Return Qty": Cell(""),
        "Return Date": Cell(""),
        "Gift Card": Cell("", fmt="currency"),
        "Sales Tax": Cell("", fmt="currency"),
        "Rewards Used": Cell("", fmt="currency"),
        "Receipt Link": Cell(
            "https://objectstorage.us-ashburn-1.oraclecloud.com/p/tok/n/ns/b/bkt/o/"
            f"receipts/bestbuy/2026-08/BBY01-{row_number:09d}.pdf"
        ),
    }
    for name, value in overrides.items():
        base[name] = value if isinstance(value, Cell) else Cell(value)
    return [base[name] for name in HEADER]


def build(*rows: list[Cell], merges=None) -> Sheet:
    return Sheet(grids_for(*rows, merges=merges))


def grids_for(*rows: list[Cell], merges=None) -> Grids:
    """The Grids a fake worksheet holding these rows would produce.

    `merges` is threaded through meta the way read_grids captures it live, so the merge check can be
    exercised without the fake needing to imitate the whole spreadsheet-metadata API.
    """
    worksheet = RenderedFakeWorksheet([header_cells(), *rows])
    grids = audit_sheet.read_grids(worksheet)
    return Grids(
        formatted=grids.formatted,
        unformatted=grids.unformatted,
        formula=grids.formula,
        meta={**grids.meta, "merges": merges},
    )


def result_for(sheet: Sheet, name: str, opts: Options | None = None):
    results = run_checks(sheet, opts or Options())
    matched = [r for r in results if r.name == name]
    assert matched, f"no check named {name!r}; have {[r.name for r in results]}"
    return matched[0]


# --------------------------------------------------------------------------------------------------
# The fake itself — pin it before anything depends on it
# --------------------------------------------------------------------------------------------------


class TestTheFakeMatchesRealGspread:
    """If these are wrong, every check below is testing a fiction. See the module docstring."""

    def test_formatted_is_always_a_string(self):
        assert render(Cell(1), "formatted") == "1"
        assert render(Cell(2.0), "formatted") == "2"
        assert render(Cell(399.5), "formatted") == "399.5"

    def test_percent_format_displays_as_percent_but_stores_a_float(self):
        cell = Cell(0.04, fmt="percent")
        assert render(cell, "formatted") == "4%"
        assert render(cell, "unformatted") == 0.04

    def test_currency_format_displays_with_symbol_but_stores_a_float(self):
        cell = Cell(1299.0, fmt="currency")
        assert render(cell, "formatted") == "$1,299.00"
        assert render(cell, "unformatted") == 1299.0

    def test_a_date_formatted_cell_stores_a_serial_in_both_non_display_modes(self):
        cell = Cell(serial("2026-08-06"), fmt="date")
        assert render(cell, "formatted") == "8/6/2026"
        assert render(cell, "unformatted") == serial("2026-08-06")
        assert render(cell, "formula") == serial("2026-08-06")

    def test_a_formula_cell_shows_its_result_except_in_formula_mode(self):
        cell = Cell(41.99, formula="=IF(A1,1,2)")
        assert render(cell, "formatted") == "41.99"
        assert render(cell, "unformatted") == 41.99
        assert render(cell, "formula") == "=IF(A1,1,2)"

    def test_get_all_values_is_refused(self):
        with pytest.raises(AssertionError, match="never use get_all_values"):
            RenderedFakeWorksheet([]).get_all_values()

    def test_a_read_without_an_explicit_render_option_is_refused(self):
        with pytest.raises(AssertionError, match="explicit value_render_option"):
            RenderedFakeWorksheet([]).get_values()


def test_read_grids_requests_all_three_modes_exactly_once():
    """The anti-drift guarantee: the auditor asks for each representation, deliberately, once."""
    worksheet = RenderedFakeWorksheet([header_cells(), row_cells(2)])
    audit_sheet.read_grids(worksheet)
    assert worksheet.reads == ["FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA"]


def _called_names(path: str) -> set[str]:
    """Every function/method NAME the module actually calls.

    Parsed from the AST rather than grepped, because audit_sheet.py deliberately NAMES the mutating
    calls it refuses to make, in order to explain why — a substring scan would flag that explanation
    as the very thing it warns against.
    """
    import ast

    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Attribute):
                names.add(target.attr)
            elif isinstance(target, ast.Name):
                names.add(target.id)
    return names


def test_the_audit_module_never_calls_a_writer():
    """Crude, but this is exactly the class of mistake that recurs here, and it's cheap insurance."""
    called = _called_names(audit_sheet.__file__)
    for forbidden in (
        "_write_profit_formulas", "add_worksheet", "append_row", "append_rows",
        "batch_update", "update", "update_cell", "update_cells", "delete_rows", "clear",
        # The opener that CREATES a missing tab (ledger_sync.py:172-178) — see the module docstring.
        "_get_worksheet",
    ):
        assert forbidden not in called, f"audit_sheet.py must not call {forbidden!r}"
    # Positive control: the guard is actually looking at real calls.
    assert "read_grids" in called


# --------------------------------------------------------------------------------------------------
# A healthy sheet passes cleanly
# --------------------------------------------------------------------------------------------------


def test_a_healthy_sheet_has_no_failures():
    sheet = build(row_cells(2), row_cells(3))
    results = run_checks(sheet, Options())
    failures = [r for r in results if r.status == "FAIL"]
    assert not failures, [(r.name, r.summary, r.details) for r in failures]


def test_expect_rows_asserts_the_count():
    sheet = build(row_cells(2), row_cells(3))
    assert result_for(sheet, "row_count", Options(expect_rows=2)).status == "PASS"
    assert result_for(sheet, "row_count", Options(expect_rows=23)).status == "FAIL"


# --------------------------------------------------------------------------------------------------
# One regression case per historical bug
# --------------------------------------------------------------------------------------------------


def test_a_reordered_header_fails_and_skips_every_index_based_check():
    """the design notes: right names, wrong order — every header.index() succeeds and the positional
    write then scrambles every field of every row it touches, with no exception and no log."""
    swapped = list(HEADER)
    swapped[0], swapped[1] = swapped[1], swapped[0]
    worksheet = RenderedFakeWorksheet([[Cell(n) for n in swapped], row_cells(2)])
    sheet = Sheet(audit_sheet.read_grids(worksheet))
    results = {r.name: r for r in run_checks(sheet, Options())}
    assert results["header_matches_schema"].status == "FAIL"
    assert any("MOVED" in d for d in results["header_matches_schema"].details)
    assert results["duplicate_primary_keys"].status == "SKIP"
    assert results["dates_are_iso_text"].status == "SKIP"


def test_an_order_date_formatted_as_a_real_date_is_caught_twice():
    """the design notes's landmine. Order Date is in the upsert key, so a date-formatted column means the
    next re-check of a row without a tracking number APPENDS a duplicate."""
    sheet = build(row_cells(2, **{"Order Date": Cell(serial("2026-08-06"), fmt="date")}))
    dates = result_for(sheet, "dates_are_iso_text")
    assert dates.status == "FAIL"
    assert "SERIAL" in dates.details[0] and "2026-08-06" in dates.details[0]
    # ... and the generic invariant catches the same thing without knowing about dates at all.
    assert result_for(sheet, "key_is_format_independent").status == "FAIL"


def test_a_percent_formatted_rate_is_fine_because_formatting_is_the_users_call():
    sheet = build(row_cells(2, **{"Cashback Rate": Cell(0.04, fmt="percent")}))
    assert result_for(sheet, "numeric_columns_are_numeric").status == "PASS"
    assert result_for(sheet, "key_is_format_independent").status == "PASS"


def test_a_currency_string_written_back_as_text_is_the_section_8_signature():
    sheet = build(row_cells(2, **{"Total Cost": Cell("$3,402.00")}))
    numeric = result_for(sheet, "numeric_columns_are_numeric")
    assert numeric.status == "FAIL"
    assert "literal TEXT" in numeric.details[0]


def test_shipment_stored_as_an_int_passes_and_does_not_look_like_a_duplicate():
    """The §9 false signal: a numeric-typed Shipment cell once looked like it wouldn't match."""
    sheet = build(row_cells(2, Shipment=Cell(1)), row_cells(3, Shipment=Cell(2)))
    assert result_for(sheet, "shipment_is_int").status == "PASS"
    assert result_for(sheet, "duplicate_primary_keys").status == "PASS"


def test_a_legacy_shipment_label_is_caught():
    sheet = build(row_cells(2, Shipment=Cell("Shipment 2")))
    assert result_for(sheet, "shipment_is_int").status == "FAIL"


def test_card_last4_stored_as_an_int_loses_its_leading_zero():
    sheet = build(row_cells(2, **{"Card Last 4": Cell(766)}))
    result = result_for(sheet, "card_last4_is_text")
    assert result.status == "FAIL"
    assert "leading zeros lost" in result.details[0]


def test_leading_zeros_are_reported_when_intact():
    sheet = build(row_cells(2), row_cells(3))
    assert "0315" in result_for(sheet, "card_last4_is_text").summary


def test_a_frozen_profit_formula_is_caught():
    """ledger_sync.py:520-524 — a formatted read carries the evaluated number forward and the RAW row
    write freezes it. The frozen cell looks completely normal; it just stops updating."""
    sheet = build(row_cells(2, **{"Total Profit": Cell(41.99)}))
    assert result_for(sheet, "profit_formula_coverage").status == "FAIL"


def test_a_stale_formula_from_before_a_reorder_is_caught():
    """A stale formula still evaluates and still shows a plausible dollar figure.

    The perturbed column is derived from _COL rather than written as a literal letter: this test used
    to say .replace("Q2", "X2"), and when the 2026-08-13 reorder moved Payout Amount off Q that became
    a silent no-op — the formula was left untouched, the check passed, and the test still "passed"
    while asserting nothing at all.
    """
    stale = _profit_formula(2).replace(f'{_COL["payout_amount"]}2', "X2")
    assert stale != _profit_formula(2), "the perturbation must actually change the formula"
    sheet = build(row_cells(2, **{"Total Profit": Cell("", formula=stale)}))
    result = result_for(sheet, "profit_formula_literal")
    assert result.status == "FAIL"
    assert result_for(sheet, "profit_formula_coverage").status == "PASS"


def test_a_stale_cogs_formula_is_caught():
    """COGS is the year-end cost figure, so a stale one misreports taxes, not just a cell."""
    stale = _cogs_formula(2).replace(f'{_COL["total_cost"]}2', "X2")
    assert stale != _cogs_formula(2)
    sheet = build(row_cells(2, **{"COGS": Cell(798.0, formula=stale)}))
    assert result_for(sheet, "cogs_formula_literal").status == "FAIL"
    assert result_for(sheet, "cogs_formula_coverage").status == "PASS"


def test_a_frozen_cogs_formula_is_caught():
    sheet = build(row_cells(2, **{"COGS": Cell(798.0)}))
    assert result_for(sheet, "cogs_formula_coverage").status == "FAIL"


def test_a_hand_written_formula_elsewhere_is_caught():
    sheet = build(row_cells(2, **{"Total Cost": Cell(798.0, formula="=C2*2")}))
    assert result_for(sheet, "no_stray_formulas").status == "FAIL"


def test_a_newline_in_item_name_fails_but_elsewhere_only_warns():
    """Item Name is in the upsert key, so a newline duplicates the row rather than just looking untidy."""
    assert result_for(build(row_cells(2, **{"Item Name": Cell("ASUS\nVivobook")})), "no_embedded_newlines").status == "FAIL"
    assert result_for(build(row_cells(2, **{"Delivery Address": Cell("1 Example Way\nTestville TS")})), "no_embedded_newlines").status == "WARN"


def test_two_rows_sharing_the_full_key_are_a_duplicate():
    sheet = build(row_cells(2), row_cells(2))  # identical key, two sheet rows
    assert result_for(sheet, "duplicate_primary_keys").status == "FAIL"


def test_a_blank_order_id_row_is_a_permanent_orphan():
    """Both ledger_sync.py:317 and :360 skip these, so nothing can ever update them again."""
    sheet = build(row_cells(2, **{"Order ID": Cell("")}))
    assert result_for(sheet, "blank_order_id_rows").status == "FAIL"


def test_an_orphan_inside_the_sorted_block_is_reported_as_one_the_sort_will_move():
    """The second consequence, and the surprising one: sort_ledger_by_date_desc sorts everything from
    row 2 down to the last row that HAS an Order ID, so an orphan in that span gets shuffled."""
    sheet = build(
        row_cells(2),
        row_cells(3, **{"Order ID": Cell("")}),  # orphan between two real rows
        row_cells(4),
    )

    result = result_for(sheet, "blank_order_id_rows")

    assert result.status == "FAIL"
    assert "sort" in result.summary
    assert any("row 3" in d and "sort will move it" in d for d in result.details)


def test_a_note_row_below_the_last_order_is_not_reported_as_movable():
    """Below the block is where a hand-written note belongs — the sort range stops at the last order,
    so it stays put. Still an orphan (it's not a ledger row), just not a movable one."""
    sheet = build(row_cells(2), row_cells(3, **{"Order ID": Cell("")}))

    result = result_for(sheet, "blank_order_id_rows")

    assert result.status == "FAIL"
    assert "sort" not in result.summary
    assert not any("sort will move it" in d for d in result.details)


def test_trailing_whitespace_in_a_key_cell_is_an_invisible_duplicate():
    """ledger_sync.py:319 builds the key with no .strip()."""
    sheet = build(row_cells(2, **{"Item Name": Cell("ASUS Vivobook 15 ")}))
    assert result_for(sheet, "key_cells_have_no_edge_whitespace").status == "FAIL"


def test_a_scrambled_column_is_caught_by_shape():
    """After a positional scramble every cell is individually plausible; only shape assertions notice."""
    sheet = build(row_cells(2, **{"Order Link": Cell("delivered"), "Status": Cell("https://x.com")}))
    assert result_for(sheet, "column_shape").status == "FAIL"


def test_an_unknown_status_keeps_an_order_open_forever():
    sheet = build(row_cells(2, Status=Cell("in transit")))
    assert result_for(sheet, "column_shape").status == "FAIL"


def test_shipped_without_a_tracking_number_is_unreachable_from_every_producer():
    sheet = build(row_cells(2, Status=Cell("shipped"), **{"Tracking Number": Cell("")}))
    assert result_for(sheet, "shipped_rows_have_tracking").status == "FAIL"


def test_inconsistent_shipping_split_breaks_the_pro_rata_profit():
    # Both rows share the default Total Cost (798.0, see row_cells), so their Shipping/Total Cost
    # ratios should match if the split were applied consistently -- 10.0/798.0 vs 0.0/798.0 don't.
    a = row_cells(2, **{"Order ID": Cell("BBY01-1"), "Shipping": Cell(10.0), "Item Name": Cell("A")})
    b = row_cells(3, **{"Order ID": Cell("BBY01-1"), "Shipping": Cell(0.0), "Item Name": Cell("B")})
    assert result_for(build(a, b), "shipping_is_cost_weighted").status == "WARN"


def test_total_cost_that_does_not_reconcile_is_flagged():
    sheet = build(row_cells(2, Quantity=Cell(2), **{"Cost Per Item": Cell(399.0), "Total Cost": Cell(1.0)}))
    assert result_for(sheet, "total_cost_matches_quantity").status == "WARN"


class TestQuantityIsChecked:
    """Regression: Quantity had NO type check at all.

    `numeric_columns_are_numeric` builds its column list as `_NUMERIC_FIELDS - _INT_FIELDS`, which
    drops Quantity — so the `*` carve-out that used to live inside it was unreachable, and the test
    that "proved" it passed vacuously against a column the check never inspects. Asserting PASS on a
    check that isn't looking is worse than having no test.
    """

    def test_numeric_columns_check_genuinely_does_not_inspect_quantity(self):
        from sheets.ledger_sync import _INT_FIELDS, _NUMERIC_FIELDS

        assert "quantity" not in {f for f in _NUMERIC_FIELDS if f not in _INT_FIELDS}

    def test_a_text_quantity_is_caught_by_the_dedicated_check(self):
        sheet = build(row_cells(2, Quantity=Cell("2 units")))
        assert result_for(sheet, "quantity_is_int").status == "FAIL"

    def test_a_float_quantity_is_caught(self):
        assert result_for(build(row_cells(2, Quantity=Cell(2.0))), "quantity_is_int").status == "FAIL"

    def test_the_undisclosed_split_marker_is_legal(self):
        """Quantity "*" is what the split safety net writes -- a type error would be a false positive."""
        assert result_for(build(row_cells(2, Quantity=Cell("*"))), "quantity_is_int").status == "PASS"

    def test_a_row_with_non_numeric_factors_is_reported_as_skipped_not_silently_passed(self):
        sheet = build(row_cells(2, Quantity=Cell("*")))
        assert "skipped" in result_for(sheet, "total_cost_matches_quantity").summary


# --------------------------------------------------------------------------------------------------
# Exit codes and snapshots
# --------------------------------------------------------------------------------------------------


def test_exit_code_is_1_on_failure_and_0_otherwise():
    healthy = run_checks(build(row_cells(2)), Options())
    assert audit_sheet.exit_code(healthy, strict=False) == 0
    broken = run_checks(build(row_cells(2, **{"Card Last 4": Cell(766)})), Options())
    assert audit_sheet.exit_code(broken, strict=False) == 1


def test_strict_promotes_warnings_to_a_failing_exit_code():
    a = row_cells(2, **{"Order ID": Cell("BBY01-1"), "Shipping": Cell(10.0), "Item Name": Cell("A")})
    b = row_cells(3, **{"Order ID": Cell("BBY01-1"), "Shipping": Cell(0.0), "Item Name": Cell("B")})
    results = run_checks(build(a, b), Options())
    assert audit_sheet.exit_code(results, strict=False) == 0
    assert audit_sheet.exit_code(results, strict=True) == 1


def test_a_snapshot_round_trips_through_json():
    import json

    worksheet = RenderedFakeWorksheet([header_cells(), row_cells(2)])
    grids = audit_sheet.read_grids(worksheet)
    restored = Grids.from_snapshot(json.loads(json.dumps(grids.to_snapshot(), default=str)))
    assert restored.formatted == grids.formatted
    assert restored.formula == grids.formula
    # A snapshot audit reaches the same verdict as the live one.
    assert [r.status for r in run_checks(Sheet(restored), Options())] == [
        r.status for r in run_checks(Sheet(grids), Options())
    ]


def test_a_broken_check_does_not_hide_the_checks_after_it():
    sheet = build(row_cells(2))

    @audit_sheet.check("deliberately_broken")
    def _boom(s, o):
        raise ValueError("boom")

    try:
        results = run_checks(sheet, Options())
        broken = [r for r in results if r.name == "deliberately_broken"][0]
        assert broken.status == "FAIL" and "ValueError" in broken.summary
        assert len(results) == len(audit_sheet.CHECKS)
    finally:
        audit_sheet.CHECKS[:] = [c for c in audit_sheet.CHECKS if c[0] != "deliberately_broken"]


def test_header_and_fieldnames_stay_paired():
    """A guard the auditor leans on: it maps _NUMERIC_FIELDS to display names positionally."""
    assert len(HEADER) == len(FIELDNAMES)


# --------------------------------------------------------------------------------------------------
# The hardening pass: six more checks, each with a false-positive guard
#
# The guards matter more than the positive cases. A noisy auditor gets skimmed, and a check that cries
# wolf on a legitimate sheet is worse than no check -- it trains the reader to ignore the column that
# the real failures will one day appear in.
# --------------------------------------------------------------------------------------------------


def _iso_days_ago(days: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class TestLegacyBlankShipment:
    def test_a_row_with_no_shipment_number_is_flagged(self):
        """It orphans if that order later splits: the scraper emits 1..N, none matching the blank."""
        assert result_for(build(row_cells(2, Shipment=Cell(""))), "legacy_blank_shipment").status == "WARN"

    def test_a_normal_row_is_not_flagged(self):
        assert result_for(build(row_cells(2)), "legacy_blank_shipment").status == "PASS"


class TestContentOutsideTheSchema:
    def test_content_past_the_last_schema_column_fails(self):
        """The signature of the append_rows bug that once landed rows ten columns right, in K:AB."""
        wide = row_cells(2) + [Cell("stray")]
        assert result_for(build(wide), "content_outside_the_schema").status == "FAIL"

    def test_content_below_the_data_block_warns(self):
        """The append anchor is len(existing)+1, so a note under the data misplaces the next append."""
        note = [Cell("") for _ in HEADER]
        # Indexed BY NAME, not by a literal: this said note[4] until the 2026-08-25 reorder moved
        # Order ID to index 4, at which point the "note" became a real ledger row and the check had
        # nothing to warn about — the test passed while asserting the opposite of its intent.
        note[HEADER.index("Item Name")] = Cell("my notes")  # no Order ID -> not a ledger row
        assert result_for(build(row_cells(2), note), "content_outside_the_schema").status == "WARN"

    def test_a_blank_row_inside_the_data_block_warns(self):
        blank = [Cell("") for _ in HEADER]
        assert result_for(build(row_cells(2), blank, row_cells(4)), "content_outside_the_schema").status == "WARN"

    def test_a_clean_block_passes(self):
        assert result_for(build(row_cells(2), row_cells(3)), "content_outside_the_schema").status == "PASS"


class TestNoFormulaErrors:
    def test_a_ref_error_is_caught(self):
        """What a column delete leaves behind -- and profit_formula_coverage would still see a formula."""
        sheet = build(row_cells(2, **{"Total Profit": Cell("#REF!", formula="=IF(#REF!,1,2)")}))
        assert result_for(sheet, "no_formula_errors").status == "FAIL"

    def test_a_costco_item_number_is_not_an_error(self):
        """THE false positive to avoid: costco_mapping appends "(Item #N)" to disambiguate Costco's
        truncated descriptions, so a 'starts with #' rule would flag real data on every Costco row."""
        sheet = build(row_cells(2, **{"Item Name": Cell("KIRKLAND SIGNATURE (Item #1847785)")}))
        assert result_for(sheet, "no_formula_errors").status == "PASS"


class TestCashbackRateSane:
    def test_a_rate_of_4_meaning_4_percent_is_caught(self):
        """It multiplies straight into the profit formula -- 100x overstatement, plausible-looking."""
        sheet = build(row_cells(2, **{"Cashback Rate": Cell(4)}))
        assert result_for(sheet, "cashback_rate_sane").status == "FAIL"

    def test_the_confirmed_13_percent_costco_rate_is_not_flagged(self):
        """Deliberately no 'suspiciously high' band: this rate is real and user-confirmed, so a band
        would be permanent noise on every Costco row."""
        sheet = build(row_cells(2, **{"Cashback Rate": Cell(0.13, fmt="percent")}))
        assert result_for(sheet, "cashback_rate_sane").status == "PASS"


class TestOpenRowStaleness:
    def test_an_open_row_that_stopped_being_scraped_warns(self):
        sheet = build(row_cells(2, Status=Cell("ordered"), **{"Last Scraped At": Cell(_iso_days_ago(9))}))
        result = result_for(sheet, "open_row_staleness")
        assert result.status == "WARN" and "9 days ago" in result.details[0]

    def test_a_terminal_row_is_never_stale(self):
        """Delivered rows are terminal and deliberately never re-scraped -- flagging them would mean
        warning about all 20-odd historical rows forever."""
        sheet = build(row_cells(2, Status=Cell("delivered"), **{"Last Scraped At": Cell(_iso_days_ago(400))}))
        assert result_for(sheet, "open_row_staleness").status == "PASS"

    def test_a_recently_scraped_open_row_passes(self):
        sheet = build(row_cells(2, Status=Cell("ordered"), **{"Last Scraped At": Cell(_iso_days_ago(0))}))
        assert result_for(sheet, "open_row_staleness").status == "PASS"


class TestMergedCells:
    def test_a_merge_is_a_failure(self):
        """A merged cell reads as its top-left value and blanks its neighbours -- which _merge_row
        then PRESERVES as though the data were legitimately absent, freezing those cells forever."""
        merges = [{"startRowIndex": 1, "endRowIndex": 3, "startColumnIndex": 0, "endColumnIndex": 2}]
        assert result_for(build(row_cells(2), merges=merges), "no_merged_cells").status == "FAIL"

    def test_no_merges_passes(self):
        assert result_for(build(row_cells(2), merges=[]), "no_merged_cells").status == "PASS"

    def test_a_snapshot_without_merge_data_skips_rather_than_passing(self):
        """An older snapshot must not report a confident PASS about something it never captured."""
        assert result_for(build(row_cells(2), merges=None), "no_merged_cells").status == "SKIP"


class TestReviewFindings:
    """Bugs an adversarial review found in the checks themselves, and the checks it prompted."""

    def test_a_broken_formula_renders_blank_and_only_the_payout_pairing_catches_it(self):
        """_profit_formula wraps its body in IFERROR(..., ""), so an error inside is SWALLOWED and the
        cell renders blank -- identical to a not-yet-paid-out row. An error-string scan sees nothing;
        the blank-profit-with-a-payout pairing is what actually catches it."""
        sheet = build(row_cells(2, **{
            "Payout Amount": Cell(1500.0, fmt="currency"),
            "Total Profit": Cell("", formula=_profit_formula(2)),
        }))
        assert result_for(sheet, "profit_blank_despite_payout").status == "FAIL"
        assert result_for(sheet, "no_formula_errors").status == "PASS"  # the scan is blind to it

    def test_an_unpaid_row_with_a_blank_profit_is_correct_not_a_failure(self):
        assert result_for(build(row_cells(2)), "profit_blank_despite_payout").status == "PASS"

    def test_a_blank_status_keeps_an_order_open_and_billable_forever(self):
        """column_shape guards with `if status and ...`, so blank slipped through; load_order_state
        then reads it as "ordered" and the order never closes."""
        sheet = build(row_cells(2, Status=Cell("")))
        assert result_for(sheet, "status_is_present").status == "FAIL"
        assert result_for(sheet, "column_shape").status == "PASS"  # why it needed its own check

    def test_an_unresolved_split_that_has_been_paid_out_books_the_whole_payout_as_profit(self):
        sheet = build(row_cells(2, Quantity=Cell("*"), **{
            "Total Cost": Cell(""), "Payout Amount": Cell(1500.0, fmt="currency"),
        }))
        assert result_for(sheet, "unresolved_split_quantity").status == "FAIL"

    def test_an_unresolved_split_awaiting_quantities_only_warns(self):
        sheet = build(row_cells(2, Quantity=Cell("*"), **{"Total Cost": Cell("")}))
        assert result_for(sheet, "unresolved_split_quantity").status == "WARN"

    def test_a_multi_sku_box_is_informational_not_a_permanent_warning(self):
        """Items boxed together legitimately share a shipment number and a tracking number. Warning
        would nag forever on a healthy sheet and make --strict exit 1 for good."""
        a = row_cells(2, **{"Order ID": Cell("C-1"), "Item Name": Cell("A"), "Tracking Number": Cell("1Z1")})
        b = row_cells(3, **{"Order ID": Cell("C-1"), "Item Name": Cell("B"), "Tracking Number": Cell("1Z1")})
        sheet = build(a, b)
        pair = [r for r in run_checks(sheet, Options()) if r.name in
                ("duplicate_shipment_lines", "duplicate_tracking_keys")]
        assert [r.status for r in pair] == ["INFO", "INFO"]
        # The point of INFO: these two cannot, on their own, make --strict exit non-zero.
        # (Scoped to this pair deliberately -- the coverage checks read the real warehouses.json /
        # cards.json, so a whole-sheet exit code would depend on the user's config, not on the code.)
        assert audit_sheet.exit_code(pair, strict=True) == 0

    def test_one_tracking_number_under_two_orders_is_still_a_warning(self):
        a = row_cells(2, **{"Order ID": Cell("C-1"), "Tracking Number": Cell("1Z1")})
        b = row_cells(3, **{"Order ID": Cell("C-2"), "Tracking Number": Cell("1Z1")})
        assert result_for(build(a, b), "duplicate_tracking_keys").status == "WARN"

    def test_expect_rows_is_not_swallowed_when_render_modes_disagree(self):
        """It used to return the height WARN before ever comparing, so --expect-rows reported success
        on the one sheet state that most warrants a hard stop."""
        worksheet = RenderedFakeWorksheet([header_cells(), row_cells(2)])
        grids = audit_sheet.read_grids(worksheet)
        ragged = Grids(
            formatted=grids.formatted,
            unformatted=grids.unformatted + [["extra"]],
            formula=grids.formula,
            meta=grids.meta,
        )
        result = result_for(Sheet(ragged), "row_count", Options(expect_rows=99))
        assert result.status == "FAIL" and "expected 99" in result.summary

    def test_render_modes_disagreeing_on_height_is_a_failure_not_a_warning(self):
        """sync_csv_to_sheet takes its append anchor from the formatted read's height alone."""
        worksheet = RenderedFakeWorksheet([header_cells(), row_cells(2)])
        grids = audit_sheet.read_grids(worksheet)
        ragged = Grids(
            formatted=grids.formatted,
            unformatted=grids.unformatted + [["extra"]],
            formula=grids.formula,
            meta=grids.meta,
        )
        assert result_for(Sheet(ragged), "row_count").status == "FAIL"

    def test_a_blank_shipping_row_is_excluded_rather_than_false_flagged(self):
        """A blank Shipping (partial re-check that never touched this field) can't form a ratio, so
        it's dropped from the comparison rather than compared raw against 0.0 -- which would
        false-WARN on any legacy or partially-filled row."""
        a = row_cells(2, **{"Order ID": Cell("O-1"), "Shipping": Cell(0.0), "Item Name": Cell("A")})
        b = row_cells(3, **{"Order ID": Cell("O-1"), "Shipping": Cell(""), "Item Name": Cell("B")})
        assert result_for(build(a, b), "shipping_is_cost_weighted").status == "PASS"

    def test_a_non_numeric_shipment_label_is_allowed_consistently(self):
        """The check used to count the label as allowed and then fail it two lines later, so the
        'label' bucket could never coexist with a PASS."""
        assert result_for(build(row_cells(2, Shipment=Cell("backorder"))), "shipment_is_int").status == "PASS"

    def test_informational_results_are_counted_in_the_tally(self):
        a = row_cells(2, **{"Order ID": Cell("C-1"), "Item Name": Cell("A"), "Tracking Number": Cell("1Z1")})
        b = row_cells(3, **{"Order ID": Cell("C-1"), "Item Name": Cell("B"), "Tracking Number": Cell("1Z1")})
        text = audit_sheet.render_text(run_checks(build(a, b), Options()), {}, verbose=False)
        assert "informational" in text


class TestBuyingGroupPayouts:
    """Money invariants introduced with the BFMR / MaxOutDeals posting step."""

    def test_a_payout_written_in_full_to_every_row_of_one_package_is_caught(self):
        """A payout arrives per PACKAGE; a box with two items has two rows behind one tracking
        number. Writing the full amount to each books the group's money twice, and Total Profit
        re-derives nothing -- it just reads as a larger, plausible profit."""
        a = row_cells(2, **{"Order ID": Cell("O-1"), "Item Name": Cell("A"), "Tracking Number": Cell("1Z1"),
                            "Total Cost": Cell(600.0), "Payout Amount": Cell(1000.0)})
        b = row_cells(3, **{"Order ID": Cell("O-1"), "Item Name": Cell("B"), "Tracking Number": Cell("1Z1"),
                            "Total Cost": Cell(400.0), "Payout Amount": Cell(1000.0)})
        assert result_for(build(a, b), "payout_is_cost_weighted").status == "WARN"  # WARN since 2026-08-30: a real per-item payout looks the same by ratio

    def test_a_correctly_split_payout_passes(self):
        a = row_cells(2, **{"Order ID": Cell("O-1"), "Item Name": Cell("A"), "Tracking Number": Cell("1Z1"),
                            "Total Cost": Cell(600.0), "Payout Amount": Cell(600.0)})
        b = row_cells(3, **{"Order ID": Cell("O-1"), "Item Name": Cell("B"), "Tracking Number": Cell("1Z1"),
                            "Total Cost": Cell(400.0), "Payout Amount": Cell(400.0)})
        assert result_for(build(a, b), "payout_is_cost_weighted").status == "PASS"

    def test_a_paid_row_with_a_zero_payout_is_a_fictitious_loss(self):
        """The case an earlier version of this check MISSED by testing only for blank.

        Blank makes Total Profit render blank; a literal 0 makes it compute `0 - Total Cost -
        Insurance`. Live BFMR reported three packages as paid while `amount_paid` was
        still "0.00", and the sheet booked -$1,678.46 against a $1,796 order.
        """
        sheet = build(row_cells(2, Status=Cell("paid"), **{"Payout Amount": Cell(0)}))
        result = result_for(sheet, "paid_rows_have_a_payout")
        assert result.status == "FAIL" and "fictitious" in result.details[0]

    def test_a_paid_row_awaiting_settlement_only_warns(self):
        """A group can mark a package paid minutes before it settles, and the next sync fills it -- so
        a blank is worth noticing but is not yet wrong. Staleness catches one that never fills."""
        sheet = build(row_cells(2, Status=Cell("paid"), **{"Payout Amount": Cell("")}))
        assert result_for(sheet, "paid_rows_have_a_payout").status == "WARN"

    def test_a_paid_row_with_its_payout_passes(self):
        sheet = build(row_cells(2, Status=Cell("paid"), **{"Payout Amount": Cell(1200.0, fmt="currency")}))
        assert result_for(sheet, "paid_rows_have_a_payout").status == "PASS"

    def test_an_unpaid_row_without_a_payout_is_not_flagged(self):
        assert result_for(build(row_cells(2, Status=Cell("shipped"))), "paid_rows_have_a_payout").status == "PASS"


class TestStatusRegression:
    """sync_tracking drops any write that walks a row backwards, and calls that guard load-bearing:
    MOD publishes no return signal, so a return is typed in BY HAND while MOD keeps reporting the
    package as received (= paid) forever. A regression means a human correction was silently undone --
    which no single-snapshot check can see, only a before/after comparison."""

    def test_a_status_walking_backwards_is_surfaced(self):
        before = grids_for(row_cells(2, Status=Cell("return")))
        after = grids_for(row_cells(2, Status=Cell("paid")))
        diff = audit_sheet.diff_snapshots(before, after)
        assert len(diff["status_regressed"]) == 1
        assert "BACKWARDS" in diff["status_regressed"][0]

    def test_normal_forward_progress_is_not_a_regression(self):
        before = grids_for(row_cells(2, Status=Cell("shipped")))
        after = grids_for(row_cells(2, Status=Cell("delivered")))
        assert audit_sheet.diff_snapshots(before, after)["status_regressed"] == []

    def test_an_unchanged_status_is_not_a_regression(self):
        grids = grids_for(row_cells(2, Status=Cell("paid")))
        assert audit_sheet.diff_snapshots(grids, grids)["status_regressed"] == []

    def test_a_regression_is_called_out_in_the_rendered_diff(self):
        before = grids_for(row_cells(2, Status=Cell("paid")))
        after = grids_for(row_cells(2, Status=Cell("ordered")))
        text = audit_sheet.render_diff(audit_sheet.diff_snapshots(before, after), "before.json", 8)
        assert "BACKWARDS" in text and "REGRESSED" in text


class TestCompare:
    """The before/after diff -- the thing that actually answers "did that run update or duplicate?"."""

    def test_a_new_order_shows_as_added(self):
        before = grids_for(row_cells(2))
        after = grids_for(row_cells(2), row_cells(3))
        diff = audit_sheet.diff_snapshots(before, after)
        assert len(diff["added"]) == 1 and not diff["removed"] and not diff["changed"]
        assert diff["rows_before"] == 1 and diff["rows_after"] == 2

    def test_a_status_change_shows_as_a_changed_cell_not_an_add(self):
        before = grids_for(row_cells(2, Status=Cell("ordered")))
        after = grids_for(row_cells(2, Status=Cell("shipped")))
        diff = audit_sheet.diff_snapshots(before, after)
        assert not diff["added"] and not diff["removed"]
        assert any("Status" in c and "ordered" in c and "shipped" in c for c in diff["changed"])

    def test_a_deleted_row_shows_as_removed(self):
        diff = audit_sheet.diff_snapshots(grids_for(row_cells(2), row_cells(3)), grids_for(row_cells(2)))
        assert len(diff["removed"]) == 1

    def test_last_scraped_at_is_ignored_so_it_cannot_bury_the_real_signal(self):
        """Every touched row's timestamp changes on every run, so including it would mean every diff
        reported every row as changed."""
        before = grids_for(row_cells(2, **{"Last Scraped At": Cell("2026-08-01T00:00:00Z")}))
        after = grids_for(row_cells(2, **{"Last Scraped At": Cell("2026-08-12T00:00:00Z")}))
        assert audit_sheet.diff_snapshots(before, after)["changed"] == []


class TestRowsAreDateDescending:
    """The ledger is kept newest-first. This is a WARN, not a FAIL: position never affects the upsert
    (which matches on key), so being out of order is only a readability problem — and it's expected to
    drift, since main.run_scrape re-sorts only when a sync APPENDED rows."""

    def test_newest_first_passes(self):
        sheet = build(
            row_cells(2, **{"Order Date": Cell("2026-08-11")}),
            row_cells(3, **{"Order Date": Cell("2026-08-06")}),
            row_cells(4, **{"Order Date": Cell("2026-08-02")}),
        )

        assert result_for(sheet, "rows_are_date_descending").status == "PASS"

    def test_an_older_row_above_a_newer_one_warns(self):
        sheet = build(
            row_cells(2, **{"Order Date": Cell("2026-08-02")}),
            row_cells(3, **{"Order Date": Cell("2026-08-11")}),
        )

        result = result_for(sheet, "rows_are_date_descending")
        assert result.status == "WARN"
        assert "sort_ledger" in result.summary

    def test_same_order_shipments_out_of_sequence_warns(self):
        # Shipment 2 above shipment 1 within one order: the tie-breaker exists to keep an order's rows
        # adjacent AND in shipment order.
        common = {"Order Date": Cell("2026-08-10"), "Order ID": Cell("BBY01-1")}
        sheet = build(
            row_cells(2, **common, **{"Shipment": Cell(2)}),
            row_cells(3, **common, **{"Shipment": Cell(1)}),
        )

        assert result_for(sheet, "rows_are_date_descending").status == "WARN"

    def test_same_date_different_orders_sort_by_order_id(self):
        date = {"Order Date": Cell("2026-08-10")}
        sheet = build(
            row_cells(2, **date, **{"Order ID": Cell("AAA-1")}),
            row_cells(3, **date, **{"Order ID": Cell("ZZZ-9")}),
        )

        assert result_for(sheet, "rows_are_date_descending").status == "PASS"

    def test_a_single_row_passes(self):
        assert result_for(build(row_cells(2)), "rows_are_date_descending").status == "PASS"

    def test_it_never_fails_only_warns(self):
        # Pinned deliberately: a FAIL here would make the audit red over something that cannot corrupt
        # data, training the eye to ignore red.
        sheet = build(
            row_cells(2, **{"Order Date": Cell("2020-01-01")}),
            row_cells(3, **{"Order Date": Cell("2026-08-11")}),
        )

        assert result_for(sheet, "rows_are_date_descending").status == "WARN"


class TestCogsInputsComplete:
    """COGS is the year-end cost figure and every way it breaks is silent.

    cashback_rate_sane only asks whether a rate is PLAUSIBLE; it cannot see a missing one. A blank
    rate makes COGS count the full cost, which overstates the cost side and under-reports tax while
    looking entirely normal.
    """

    def test_a_healthy_row_passes(self):
        assert result_for(build(row_cells(2)), "cogs_inputs_complete").status == "PASS"

    def test_cogs_without_a_cashback_rate_fails(self):
        sheet = build(row_cells(2, **{"Cashback Rate": Cell("")}))

        result = result_for(sheet, "cogs_inputs_complete")

        assert result.status == "FAIL"
        assert any("no rate resolved" in d for d in result.details)

    def test_a_payout_without_cogs_fails(self):
        # Income recorded with no cost against it -- profit overstated.
        sheet = build(row_cells(2, **{"COGS": Cell(""), "Payout Amount": Cell(800.0)}))

        assert result_for(sheet, "cogs_inputs_complete").status == "FAIL"

    def test_cogs_without_a_payout_is_reported_but_never_fails(self):
        """The normal state of a shipped-but-unpaid order, and at a year boundary it IS the straddle
        -- the cost and income sides fall in different tax years. Worth seeing, never worth failing."""
        sheet = build(row_cells(2, Status=Cell("shipped"), **{"Payout Amount": Cell("")}))

        result = result_for(sheet, "cogs_inputs_complete")

        assert result.status == "PASS"
        assert "no payout yet" in result.summary

    def test_a_cancelled_row_is_exempt(self):
        # It carries no money by design; flagging it would fail every audit forever.
        sheet = build(row_cells(2, Status=Cell("cancelled"), **{
            "COGS": Cell(""), "Total Cost": Cell(""), "Cashback Rate": Cell(""),
            "Payout Amount": Cell(""),
        }))

        assert result_for(sheet, "cogs_inputs_complete").status == "PASS"

    def test_a_gift_card_row_is_not_counted_as_an_unpaid_straddle(self):
        """A gift card will NEVER have a payout of its own -- the income arrives through the order it
        funded, whose cost was netted down by the card. Counting it as a straddle would misreport the
        year-boundary number it exists to surface."""
        sheet = build(row_cells(2, **{"Buying Group": Cell("Gift Card"), "Payout Amount": Cell("")}))

        result = result_for(sheet, "cogs_inputs_complete")

        assert result.status == "PASS"
        assert "no payout yet" not in result.summary
        assert any("gift-card row(s) carry cost with no payout, as designed" in d
                   for d in result.details) or "gift-card" in result.summary


# --------------------------------------------------------------------------------------------------
# the design notes "smaller" items -- 2026-08-29
# --------------------------------------------------------------------------------------------------


class TestImpossibleDates:
    """The regex only tests the SHAPE. A date that does not exist must not pass as ISO text."""

    def test_an_impossible_order_date_fails(self):
        sheet = build(row_cells(2, **{"Order Date": Cell("2026-13-45")}))
        result = result_for(sheet, "dates_are_iso_text")
        assert result.status == "FAIL"
        assert "not a real calendar date" in result.details[0]

    def test_a_non_leap_february_29_fails(self):
        sheet = build(row_cells(2, **{"Order Date": Cell("2026-02-29")}))
        assert result_for(sheet, "dates_are_iso_text").status == "FAIL"

    def test_a_real_leap_day_passes(self):
        sheet = build(row_cells(2, **{"Order Date": Cell("2028-02-29")}))
        assert result_for(sheet, "dates_are_iso_text").status == "PASS"

    def test_an_impossible_date_outside_order_date_is_only_a_warning(self):
        sheet = build(row_cells(2, **{"Delivery Date": Cell("2026-00-10")}))
        assert result_for(sheet, "dates_are_iso_text").status == "WARN"


class TestSnapshotPath:
    """A snapshot is the whole sheet, PII included, so a bare name must not land in the CWD."""

    def test_a_bare_filename_lands_under_data(self):
        assert audit_sheet._snapshot_path("before.json") == audit_sheet.SNAPSHOT_DIR / "before.json"

    def test_a_relative_directory_is_honoured(self):
        from pathlib import Path
        assert audit_sheet._snapshot_path("out/x.json") == Path("out/x.json")

    def test_an_absolute_path_is_honoured(self, tmp_path):
        target = tmp_path / "x.json"
        assert audit_sheet._snapshot_path(str(target)) == target

    def test_data_is_gitignored(self):
        from pathlib import Path
        ignored = Path(__file__).resolve().parents[1] / ".gitignore"
        lines = [line.strip() for line in ignored.read_text(encoding="utf-8").splitlines()]
        assert "data/" in lines


class TestShipmentNumbersContiguous:
    def _order(self, *shipments):
        rows = [
            row_cells(n, **{"Order ID": Cell("BBY01-1"), "Shipment": Cell(s),
                            "Tracking Number": Cell(f"1Z{n}")})
            for n, s in enumerate(shipments, start=2)
        ]
        return build(*rows)

    def test_one_through_n_passes(self):
        assert result_for(self._order(1, 2), "shipment_numbers_contiguous").status == "PASS"

    def test_a_gap_warns_and_names_the_missing_number(self):
        result = result_for(self._order(1, 3), "shipment_numbers_contiguous")
        assert result.status == "WARN"
        assert "2 missing" in result.details[0]

    def test_a_lone_second_box_warns(self):
        assert result_for(self._order(2), "shipment_numbers_contiguous").status == "WARN"

    def test_blank_and_label_shipments_are_ignored(self):
        assert result_for(self._order(1, "", "Box B"), "shipment_numbers_contiguous").status == "PASS"

    def test_two_orders_are_numbered_independently(self):
        sheet = build(
            row_cells(2, **{"Order ID": Cell("A"), "Shipment": Cell(1)}),
            row_cells(3, **{"Order ID": Cell("B"), "Shipment": Cell(1)}),
            row_cells(4, **{"Order ID": Cell("B"), "Shipment": Cell(2)}),
        )
        assert result_for(sheet, "shipment_numbers_contiguous").status == "PASS"


class TestFormulaLocale:
    """Sheets re-serialises formulas in the spreadsheet's locale; that must not read as 'stale'."""

    def test_a_semicolon_locale_still_matches(self):
        european = _profit_formula(2).replace(",", "; ")
        assert european != _profit_formula(2)
        sheet = build(row_cells(2, **{"Total Profit": Cell("", formula=european)}))
        assert result_for(sheet, "profit_formula_literal").status == "PASS"

    def test_a_respaced_cogs_formula_still_matches(self):
        respaced = _cogs_formula(2).replace("(", "( ").replace(")", " )")
        sheet = build(row_cells(2, **{"COGS": Cell(798.0, formula=respaced)}))
        assert result_for(sheet, "cogs_formula_literal").status == "PASS"

    def test_a_changed_column_letter_is_still_caught_after_canonicalising(self):
        stale = _profit_formula(2).replace(f'{_COL["payout_amount"]}2', "X2").replace(",", ";")
        sheet = build(row_cells(2, **{"Total Profit": Cell("", formula=stale)}))
        assert result_for(sheet, "profit_formula_literal").status == "FAIL"


class TestProfitValueMatchesInputs:
    """The number itself, recomputed in Python from the same cells the formula reads."""

    def _row(self, profit, payout=900.0, cogs=798.0, insurance=7.4, status="paid"):
        return row_cells(2, **{
            "Status": Cell(status),
            "Payout Amount": Cell(payout, fmt="currency"),
            "COGS": Cell(cogs, fmt="currency", formula=_cogs_formula(2)),
            "Insurance": Cell(insurance, fmt="currency"),
            "Total Profit": Cell(profit, formula=_profit_formula(2)),
        })

    def test_a_consistent_row_passes(self):
        assert result_for(build(self._row(94.6)), "profit_value_matches_inputs").status == "PASS"

    def test_a_blank_insurance_counts_as_zero(self):
        sheet = build(self._row(102.0, insurance=""))
        assert result_for(sheet, "profit_value_matches_inputs").status == "PASS"

    def test_a_disagreeing_number_fails_and_shows_the_arithmetic(self):
        result = result_for(build(self._row(50.0)), "profit_value_matches_inputs")
        assert result.status == "FAIL"
        assert "900.00 - 798.00 - 7.40 = 94.60" in result.details[0]

    def test_rows_without_a_payout_or_cancelled_are_skipped(self):
        no_payout = build(self._row(50.0, payout=""))
        cancelled = build(self._row(50.0, status="cancelled"))
        assert result_for(no_payout, "profit_value_matches_inputs").status == "PASS"
        assert result_for(cancelled, "profit_value_matches_inputs").status == "PASS"

    def test_a_blank_profit_is_left_to_the_payout_pairing_check(self):
        sheet = build(self._row(""))
        assert result_for(sheet, "profit_value_matches_inputs").status == "PASS"
        assert result_for(sheet, "profit_blank_despite_payout").status != "PASS"


class TestOrderLevelCellsAgree:
    """Retailer / Profile / Order Date are per-ORDER facts; a row that disagrees is invisible to its run."""

    def _order(self, **second_row):
        return build(
            row_cells(2, **{"Order ID": Cell("A1"), "Shipment": Cell(1), "Tracking Number": Cell("1Z1")}),
            row_cells(3, **{"Order ID": Cell("A1"), "Shipment": Cell(2), "Tracking Number": Cell("1Z2"),
                            **second_row}),
        )

    def test_a_consistent_order_passes(self):
        assert result_for(self._order(), "order_level_cells_agree").status == "PASS"

    def test_a_differing_profile_fails_and_names_both_rows(self):
        result = result_for(self._order(Profile=Cell("profile-bravo")), "order_level_cells_agree")
        assert result.status == "FAIL"
        assert "Profile differs" in result.details[0]
        assert "rows [2]" in result.details[0] and "rows [3]" in result.details[0]

    def test_a_differing_retailer_or_order_date_fails(self):
        assert result_for(self._order(Retailer=Cell("Amazon")), "order_level_cells_agree").status == "FAIL"
        assert result_for(self._order(**{"Order Date": Cell("2026-08-07")}), "order_level_cells_agree").status == "FAIL"

    def test_edge_whitespace_alone_is_left_to_the_whitespace_check(self):
        """Stripped before comparing: 'p ' vs 'p' is key_cells_have_no_edge_whitespace's finding."""
        assert result_for(self._order(Profile=Cell("profile-alpha ")), "order_level_cells_agree").status == "PASS"

    def test_different_orders_are_independent(self):
        sheet = build(
            row_cells(2, **{"Order ID": Cell("A1"), "Profile": Cell("profile-alpha")}),
            row_cells(3, **{"Order ID": Cell("B2"), "Profile": Cell("profile-bravo")}),
        )
        assert result_for(sheet, "order_level_cells_agree").status == "PASS"


class TestDisplayRoundTripsToStored:
    """The generic §8 invariant: every numeric cell's display text must read back as its stored value."""

    def test_the_default_formats_all_round_trip(self):
        assert result_for(build(row_cells(2)), "display_round_trips_to_stored").status == "PASS"

    def test_a_zero_decimal_currency_loses_cents(self):
        sheet = build(row_cells(2, **{"Total Cost": Cell(1300.45, fmt="currency0")}))
        r = result_for(sheet, "display_round_trips_to_stored")
        assert r.status == "FAIL" and "loses precision" in r.details[0] and "Total Cost" in r.details[0]

    def test_a_zero_decimal_percent_rounds_the_rate(self):
        sheet = build(row_cells(2, **{"Cashback Rate": Cell(0.0375, fmt="percent0")}))
        assert result_for(sheet, "display_round_trips_to_stored").status == "FAIL"

    def test_a_format_that_hides_the_number_is_the_erase_case(self):
        sheet = build(row_cells(2, **{"Payout Amount": Cell(631.0, fmt="hidden")}))
        r = result_for(sheet, "display_round_trips_to_stored")
        assert r.status == "FAIL" and "DISPLAYS BLANK" in r.details[0] and "erase" in r.details[0]

    def test_blank_and_text_cells_are_left_to_other_checks(self):
        sheet = build(row_cells(2, **{"Insurance": Cell(""), "Shipping": Cell("n/a")}))
        assert result_for(sheet, "display_round_trips_to_stored").status == "PASS"


class TestCompareEmitsResults:
    """--compare used to print prose that gated nothing. Each KIND of change now has a verdict."""

    def _classify(self, before, after):
        diff = audit_sheet.diff_snapshots(grids_for(*before), grids_for(*after))
        return {r.name: r for r in audit_sheet.classify_diff(diff, Options())}

    def _row(self, n, **kw):
        base = {"Order ID": Cell(f"O{n}"), "Tracking Number": Cell(f"1Z{n}"), "Shipment": Cell(1)}
        base.update(kw)
        return row_cells(n, **base)

    def test_a_removed_row_fails(self):
        r = self._classify([self._row(2), self._row(3)], [self._row(2)])
        assert r["compare_rows_removed"].status == "FAIL" and "compare_appended" not in r

    def test_a_new_order_is_an_info_append(self):
        r = self._classify([self._row(2)], [self._row(2), self._row(3)])
        assert r["compare_appended"].status == "INFO" and "1 row(s) appended across 1 order(s)" in r["compare_appended"].summary
        assert r["compare_updated"].status == "PASS"

    def test_a_new_key_reusing_an_existing_tracking_number_is_a_duplicate(self):
        before = [self._row(2, **{"Order ID": Cell("A"), "Tracking Number": Cell("1ZA")})]
        after = before + [self._row(3, **{"Order ID": Cell("A"), "Tracking Number": Cell("1ZA"),
                                          "Item Name": Cell("ASUS Vivobook 15 (re-worded)")})]
        r = self._classify(before, after)
        assert r["compare_appended_duplicate"].status == "FAIL"
        assert "reuses tracking 1ZA" in r["compare_appended_duplicate"].details[0]
        assert "compare_appended" not in r

    def test_a_hand_edited_item_name_is_an_identity_change_not_a_remove_plus_add(self):
        before = [self._row(2, **{"Order ID": Cell("A"), "Tracking Number": Cell("1ZA"), "Item Name": Cell("Widget")})]
        after = [self._row(2, **{"Order ID": Cell("A"), "Tracking Number": Cell("1ZA"), "Item Name": Cell("Widget Pro")})]
        r = self._classify(before, after)
        assert r["compare_identity_changed"].status == "FAIL"
        assert "compare_rows_removed" not in r and "compare_appended" not in r

    def test_a_status_regression_fails(self):
        before = [self._row(2, Status=Cell("paid"))]
        after = [self._row(2, Status=Cell("shipped"))]
        assert self._classify(before, after)["compare_status_regressed"].status == "FAIL"

    def test_a_scraped_cost_changing_on_a_terminal_row_warns(self):
        before = [self._row(2, Status=Cell("delivered"), **{"Total Cost": Cell(798.0)})]
        after = [self._row(2, Status=Cell("delivered"), **{"Total Cost": Cell(700.0)})]
        r = self._classify(before, after)
        assert r["compare_terminal_money_changed"].status == "WARN"

    def test_a_payout_landing_on_a_delivered_row_is_the_normal_sync(self):
        before = [self._row(2, Status=Cell("delivered"), **{"Payout Amount": Cell("", fmt="currency")})]
        after = [self._row(2, Status=Cell("paid"), **{"Payout Amount": Cell(900.0, fmt="currency"), "Payout Date": Cell("2026-08-30")})]
        r = self._classify(before, after)
        assert "compare_terminal_money_changed" not in r and "compare_status_regressed" not in r
        assert r["compare_updated"].status == "PASS" and "3 cell(s) updated" in r["compare_updated"].summary

    def test_a_cost_correction_on_an_open_row_is_an_ordinary_update(self):
        before = [self._row(2, Status=Cell("ordered"), **{"Total Cost": Cell(798.0)})]
        after = [self._row(2, Status=Cell("ordered"), **{"Total Cost": Cell(700.0)})]
        assert "compare_terminal_money_changed" not in self._classify(before, after)

    def test_json_output_hides_the_internal_records(self):
        import json
        diff = audit_sheet.diff_snapshots(grids_for(self._row(2)), grids_for(self._row(2)))
        payload = json.loads(audit_sheet.render_json([], {}, False, diff))
        assert "_records" not in payload["diff"] and "added" in payload["diff"]

    def test_the_verdicts_reach_the_exit_code(self):
        before, after = [self._row(2), self._row(3)], [self._row(2)]
        diff = audit_sheet.diff_snapshots(grids_for(*before), grids_for(*after))
        results = audit_sheet.classify_diff(diff, Options())
        assert audit_sheet.exit_code(results, strict=False) == 1


class TestStateVisibility:
    """What each run would see, and the rows no run can see."""

    def _scopes(self, monkeypatch, scopes, scraped=("Best Buy", "Amazon", "Amazon Business", "Costco")):
        monkeypatch.setattr(audit_sheet, "_configured_scopes", lambda: (scopes, set(scraped)))

    def test_every_row_in_a_configured_scope_passes_with_counts(self, monkeypatch):
        self._scopes(monkeypatch, [("profile-alpha", "Best Buy")])
        sheet = build(
            row_cells(2, Status=Cell("delivered")),
            row_cells(3, Status=Cell("ordered"), **{"Tracking Number": Cell(""), "Order ID": Cell("BBY01-2")}),
        )
        r = result_for(sheet, "state_visibility")
        assert r.status == "PASS"
        assert "profile-alpha/Best Buy: 1 terminal, 1 open (1 need a re-read)" in r.summary

    def test_an_open_row_no_configured_run_can_see_fails(self, monkeypatch):
        self._scopes(monkeypatch, [("profile-alpha", "Best Buy")])
        sheet = build(row_cells(2, Profile=Cell("profile-bravo"), Status=Cell("shipped")))
        r = result_for(sheet, "state_visibility")
        assert r.status == "FAIL" and "row 2: Profile 'profile-bravo' / Retailer 'Best Buy' [shipped]" in r.details[0]

    def test_a_terminal_row_outside_every_run_is_only_info(self, monkeypatch):
        """An imported delivered/paid order never needs a run again -- it must not fail the audit."""
        self._scopes(monkeypatch, [("profile-alpha", "Best Buy")])
        sheet = build(row_cells(2, Profile=Cell("profile-bravo"), Status=Cell("paid")))
        assert result_for(sheet, "state_visibility").status == "INFO"

    def test_a_hand_entered_retailer_is_info_not_fail(self, monkeypatch):
        self._scopes(monkeypatch, [("profile-alpha", "Best Buy")])
        sheet = build(row_cells(2), row_cells(3, Retailer=Cell("Newegg"), Profile=Cell("")))
        r = result_for(sheet, "state_visibility")
        assert r.status == "INFO" and "1 terminal / hand-entered row(s)" in r.summary

    def test_no_config_is_a_skip(self, monkeypatch):
        monkeypatch.setattr(audit_sheet, "_configured_scopes", lambda: (_ for _ in ()).throw(FileNotFoundError("config.json")))
        assert result_for(build(row_cells(2)), "state_visibility").status == "SKIP"

    def test_no_scopes_at_all_is_a_skip_not_every_row_invisible(self, monkeypatch):
        self._scopes(monkeypatch, [])
        assert result_for(build(row_cells(2)), "state_visibility").status == "SKIP"



class TestImportedShapesAreNotFailures:
    """Three findings from the first real import (2026-08-30) that were audit bugs, not data bugs."""

    def test_a_zero_cost_bonus_with_a_payout_has_cogs_zero_not_missing(self):
        sheet = build(row_cells(2, **{"Status": Cell("paid"), "Cost Per Item": Cell(0.0), "Total Cost": Cell(0.0),
                                     "COGS": Cell(0.0, fmt="currency", formula=_cogs_formula(2)),
                                     "Payout Amount": Cell(250.0, fmt="currency"), "Payout Date": Cell("2026-03-19")}))
        r = result_for(sheet, "cogs_inputs_complete")
        assert "with no COGS" not in " ".join(r.details)

    def test_a_return_row_may_carry_its_own_date(self):
        sheet = build(
            row_cells(2, **{"Order ID": Cell("A"), "Order Date": Cell("2026-06-04"), "Status": Cell("paid")}),
            row_cells(3, **{"Order ID": Cell("A"), "Order Date": Cell("2026-06-05"), "Status": Cell("return"),
                            "Tracking Number": Cell("1Z999AA00000002")}),
        )
        assert result_for(sheet, "order_level_cells_agree").status == "PASS"

    def test_a_return_row_must_still_agree_on_retailer(self):
        sheet = build(
            row_cells(2, **{"Order ID": Cell("A"), "Status": Cell("paid")}),
            row_cells(3, **{"Order ID": Cell("A"), "Status": Cell("return"), "Retailer": Cell("Amazon"),
                            "Tracking Number": Cell("1Z999AA00000002")}),
        )
        assert result_for(sheet, "order_level_cells_agree").status == "FAIL"

    def test_an_uneven_package_split_is_a_warning_to_read_not_a_failure(self):
        sheet = build(
            row_cells(2, **{"Order ID": Cell("A"), "Tracking Number": Cell("T"), "Total Cost": Cell(59.98),
                            "Payout Amount": Cell(74.0, fmt="currency")}),
            row_cells(3, **{"Order ID": Cell("A"), "Tracking Number": Cell("T"), "Total Cost": Cell(597.0),
                            "Payout Amount": Cell(600.0, fmt="currency"), "Item Name": Cell("Watch")}),
        )
        assert result_for(sheet, "payout_is_cost_weighted").status == "WARN"


def test_a_gift_card_row_paid_with_a_zero_payout_is_by_rule():
    sheet = build(row_cells(2, **{"Status": Cell("paid"), "Buying Group": Cell("Gift Card"),
                                 "Payout Amount": Cell(0.0, fmt="currency"), "Payout Date": Cell("2026-07-08")}))
    assert result_for(sheet, "paid_rows_have_a_payout").status == "PASS"


class TestReturnColumnsConsistent:
    def test_no_returns_is_a_quiet_pass(self):
        assert result_for(build(row_cells(2)), "return_columns_consistent").status == "PASS"

    def test_a_well_formed_return_passes(self):
        sheet = build(row_cells(2, **{"Quantity": Cell(3), "Return Qty": Cell(1), "Return Date": Cell("2026-04-30")}))
        assert result_for(sheet, "return_columns_consistent").status == "PASS"

    def test_a_return_qty_above_quantity_fails(self):
        sheet = build(row_cells(2, **{"Quantity": Cell(3), "Return Qty": Cell(4), "Return Date": Cell("2026-04-30")}))
        assert result_for(sheet, "return_columns_consistent").status == "FAIL"

    def test_a_return_without_a_date_or_a_date_without_a_qty_fails(self):
        a = build(row_cells(2, **{"Return Qty": Cell(1)}))
        b = build(row_cells(2, **{"Return Date": Cell("2026-04-30")}))
        assert result_for(a, "return_columns_consistent").status == "FAIL"
        assert result_for(b, "return_columns_consistent").status == "FAIL"


    def test_a_group_reported_return_without_a_return_qty_is_the_owed_hand_edit(self):
        sheet = build(row_cells(2, Status=Cell("return")))
        r = result_for(sheet, "return_columns_consistent")
        assert r.status == "FAIL" and "type Return Qty" in r.details[0]

    def test_a_group_reported_return_with_the_qty_typed_passes(self):
        sheet = build(row_cells(2, **{"Status": Cell("return"), "Quantity": Cell(2),
                                     "Return Qty": Cell(2), "Return Date": Cell("2026-06-05")}))
        assert result_for(sheet, "return_columns_consistent").status == "PASS"
