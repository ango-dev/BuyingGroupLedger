"""Offline tests for scripts/audit_ledger.py.

THE POINT OF THE FAKE IN THIS FILE. The auditor's whole job is to notice disagreements between the
three render modes of the worksheet contract, so a fake that returns the same grid for every mode would make
every render-mode check "pass" while being structurally incapable of representing a single bug those
checks exist to find. tests/test_ledger_sync.py's FakeWorksheet is exactly that: its `get_values`
ignores `value_render_option` entirely. That is also the shape of a bug that already bit this project
once — the design notes records `FakeWorksheet.get_all_values()` handing back raw Python types instead of
the contract's always-string formatted read, which produced a false "duplicate row" signal.

So here a test never writes the three grids by hand. It declares what each cell STORES (`Cell`), and
one renderer derives all three grids using the contract's render rules. A test physically cannot claim a cell
is an int in the unformatted grid *and* an int in the formatted grid, because it never writes the
formatted grid. TestTheFakeMatchesTheRenderRules pins the renderer itself, so when the fake drifts, that
class fails rather than a downstream check quietly going green.
"""

from dataclasses import dataclass
from datetime import date
from typing import Any

import pytest

from models.order import FIELDNAMES
from scripts import audit_ledger
from scripts.audit_ledger import Grids, Options, Sheet, run_checks
from ledger.sync import HEADER, _COL, _cogs_formula, _profit_formula
from tests.test_ledger_sync import _as_sheet_text

_SHEETS_EPOCH = date(1899, 12, 30)


def serial(iso: str) -> int:
    """The date serial a date-formatted cell stores for an ISO date (epoch 1899-12-30)."""
    y, m, d = (int(p) for p in iso.split("-"))
    return (date(y, m, d) - _SHEETS_EPOCH).days


# --------------------------------------------------------------------------------------------------
# The cell model and the renderer — one source of truth for all three grids
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Cell:
    """What a cell actually stores, plus how it's displayed.

    value    the stored value (for a formula cell: the formula's evaluated RESULT)
    fmt      None | "percent" | "currency" | "date" — a display format, the user's own choice
    formula  the literal "=..." text when the cell holds a formula
    """

    value: Any = ""
    fmt: str | None = None
    formula: str | None = None


# The ValueRenderOption values, mapped to the mode names used below.
_MODES = {
    "FORMATTED_VALUE": "formatted",
    "UNFORMATTED_VALUE": "unformatted",
    "FORMULA": "formula",
}


def render(cell: Cell, mode: str):
    """Derive one render mode's value from a stored cell, following the contract's render rules."""
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
            "is explicit that a new ledger reader picking it reinherits the corruption bug"
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
        "Receipt Link": Cell("/receipts/bestbuy/2026-08/BBY01-1.pdf"),
        "Cost Per Item": Cell(399.0),
        "Shipping": Cell(0.0),
        "Total Cost": Cell(798.0),
        "Card": Cell("Amex Business Gold"),
        "Cashback Rate": Cell(0.04, fmt="percent"),
        "Insurance": Cell("", fmt="currency"),
        "Actual Payout": Cell("", fmt="currency"),
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
        "Package ID": Cell(""),
        "Expected Payout": Cell("", fmt="currency"),
        "Receipt Link": Cell(
            "https://objectstorage.us-ashburn-1.oraclecloud.com/p/tok/n/ns/b/bkt/o/"
            f"receipts/bestbuy/2026-08/BBY01-{row_number:09d}.pdf"
        ),
    }
    for name, value in overrides.items():
        base[name] = value if isinstance(value, Cell) else Cell(value)
    return [base[name] for name in HEADER]


def build(*rows: list[Cell]) -> Sheet:
    return Sheet(grids_for(*rows))


def grids_for(*rows: list[Cell]) -> Grids:
    """The Grids a fake worksheet holding these rows would produce."""
    return audit_ledger.read_grids(RenderedFakeWorksheet([header_cells(), *rows]))


def result_for(sheet: Sheet, name: str, opts: Options | None = None):
    results = run_checks(sheet, opts or Options())
    matched = [r for r in results if r.name == name]
    assert matched, f"no check named {name!r}; have {[r.name for r in results]}"
    return matched[0]


# --------------------------------------------------------------------------------------------------
# The fake itself — pin it before anything depends on it
# --------------------------------------------------------------------------------------------------


class TestTheFakeMatchesTheRenderRules:
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
    audit_ledger.read_grids(worksheet)
    assert worksheet.reads == ["FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA"]


def _called_names(path: str) -> set[str]:
    """Every function/method NAME the module actually calls.

    Parsed from the AST rather than grepped, because audit_ledger.py deliberately NAMES the mutating
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
    called = _called_names(audit_ledger.__file__)
    for forbidden in (
        "_write_profit_formulas", "add_worksheet", "append_row", "append_rows",
        "batch_update", "update", "update_cell", "update_cells", "delete_rows", "clear",
        # The opener that CREATES a missing tab (ledger_sync.py:172-178) — see the module docstring.
        "_get_worksheet",
    ):
        assert forbidden not in called, f"audit_ledger.py must not call {forbidden!r}"
    # Positive control: the guard is actually looking at real calls.
    assert "read_grids" in called


# --------------------------------------------------------------------------------------------------
# A healthy ledger passes cleanly
# --------------------------------------------------------------------------------------------------


def test_a_healthy_ledger_has_no_failures():
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
    sheet = Sheet(audit_ledger.read_grids(worksheet))
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


def test_a_newline_in_item_name_fails_but_elsewhere_only_warns():
    """Item Name is in the upsert key, so a newline duplicates the row rather than just looking untidy."""
    assert result_for(build(row_cells(2, **{"Item Name": Cell("ASUS\nVivobook")})), "no_embedded_newlines").status == "FAIL"
    assert result_for(build(row_cells(2, **{"Delivery Address": Cell("1 Example Way\nTestville TS")})), "no_embedded_newlines").status == "WARN"


def test_two_rows_sharing_the_full_key_are_a_duplicate():
    sheet = build(row_cells(2), row_cells(2))  # identical key, two rows
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


def test_a_dashboard_relative_receipt_link_is_a_link_but_a_bare_path_elsewhere_is_not():
    """Receipts are files beside the ledger since 2026-09-18, linked as /receipts/<retailer>/<month>/<file>."""
    sheet = build(row_cells(2, **{"Receipt Link": Cell("/receipts/bestbuy/2026-09/BBY01-1.pdf")}))
    assert result_for(sheet, "column_shape").status == "PASS"
    sheet = build(row_cells(2, **{"Order Link": Cell("/receipts/bestbuy/2026-09/BBY01-1.pdf")}))
    assert result_for(sheet, "column_shape").status == "FAIL"
    sheet = build(row_cells(2, **{"Receipt Link": Cell("receipts/bestbuy/2026-09/BBY01-1.pdf")}))
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


def test_a_shipping_split_rounded_to_cents_is_not_an_inconsistency():
    """2026-09-18: the reproration rounds each share to cents, so two small rows' ratios differ in
    the fourth decimal; the check compares each row to its share, to the cent."""
    a = row_cells(2, **{"Order ID": Cell("BBY01-1"), "Shipping": Cell(3.33), "Total Cost": Cell(20.0), "Item Name": Cell("A")})
    b = row_cells(3, **{"Order ID": Cell("BBY01-1"), "Shipping": Cell(1.67), "Total Cost": Cell(10.0), "Item Name": Cell("B")})
    assert result_for(build(a, b), "shipping_is_cost_weighted").status == "PASS"


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
        from ledger.sync import _INT_FIELDS, _NUMERIC_FIELDS

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
    assert audit_ledger.exit_code(healthy, strict=False) == 0
    broken = run_checks(build(row_cells(2, **{"Card Last 4": Cell(766)})), Options())
    assert audit_ledger.exit_code(broken, strict=False) == 1


def test_strict_promotes_warnings_to_a_failing_exit_code():
    a = row_cells(2, **{"Order ID": Cell("BBY01-1"), "Shipping": Cell(10.0), "Item Name": Cell("A")})
    b = row_cells(3, **{"Order ID": Cell("BBY01-1"), "Shipping": Cell(0.0), "Item Name": Cell("B")})
    results = run_checks(build(a, b), Options())
    assert audit_ledger.exit_code(results, strict=False) == 0
    assert audit_ledger.exit_code(results, strict=True) == 1


def test_a_snapshot_round_trips_through_json():
    import json

    worksheet = RenderedFakeWorksheet([header_cells(), row_cells(2)])
    grids = audit_ledger.read_grids(worksheet)
    restored = Grids.from_snapshot(json.loads(json.dumps(grids.to_snapshot(), default=str)))
    assert restored.formatted == grids.formatted
    assert restored.formula == grids.formula
    # A snapshot audit reaches the same verdict as the live one.
    assert [r.status for r in run_checks(Sheet(restored), Options())] == [
        r.status for r in run_checks(Sheet(grids), Options())
    ]


def test_a_broken_check_does_not_hide_the_checks_after_it():
    sheet = build(row_cells(2))

    @audit_ledger.check("deliberately_broken")
    def _boom(s, o):
        raise ValueError("boom")

    try:
        results = run_checks(sheet, Options())
        broken = [r for r in results if r.name == "deliberately_broken"][0]
        assert broken.status == "FAIL" and "ValueError" in broken.summary
        assert len(results) == len(audit_ledger.CHECKS)
    finally:
        audit_ledger.CHECKS[:] = [c for c in audit_ledger.CHECKS if c[0] != "deliberately_broken"]


def test_header_and_fieldnames_stay_paired():
    """A guard the auditor leans on: it maps _NUMERIC_FIELDS to display names positionally."""
    assert len(HEADER) == len(FIELDNAMES)


# --------------------------------------------------------------------------------------------------
# The hardening pass: six more checks, each with a false-positive guard
#
# The guards matter more than the positive cases. A noisy auditor gets skimmed, and a check that cries
# wolf on a legitimate ledger is worse than no check -- it trains the reader to ignore the column that
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

    def test_a_superseded_row_is_terminal_and_never_stale(self):
        sheet = build(row_cells(2, Status=Cell("superseded"), **{"Last Scraped At": Cell(_iso_days_ago(400))}))
        assert result_for(sheet, "open_row_staleness").status == "PASS"
        assert result_for(sheet, "column_shape").status == "PASS"  # a known status


_SUPERSEDED_MONEY_BLANKS = {
    "Quantity": Cell(""), "Cost Per Item": Cell(""), "Total Cost": Cell(""), "Shipping": Cell(""),
    "Insurance": Cell(""), "Actual Payout": Cell(""), "Payout Date": Cell(""), "Gift Card": Cell(""),
    "Sales Tax": Cell(""), "Rewards Used": Cell(""),
}


class TestSupersededRowsCarryNoMoney:
    """The one non-negotiable of a kept superseded row: every money cell blank."""

    def _retired(self, n=2, **overrides):
        from ledger.sync import _cogs_formula, _profit_formula
        cells = dict(_SUPERSEDED_MONEY_BLANKS)
        cells.update({
            "Status": Cell("superseded"), "Shipment": Cell(2), "Tracking Number": Cell("DEAD"),
            "COGS": Cell("", formula=_cogs_formula(n)), "Total Profit": Cell("", formula=_profit_formula(n)),
        })
        cells.update(overrides)
        return row_cells(n, **cells)

    def test_a_clean_superseded_row_passes(self):
        result = result_for(build(self._retired()), "superseded_rows_carry_no_money")
        assert result.status == "PASS" and "1 superseded row(s)" in result.summary

    def test_a_cost_on_a_superseded_row_fails(self):
        result = result_for(build(self._retired(**{"Total Cost": Cell(2847.0)})),
                            "superseded_rows_carry_no_money")
        assert result.status == "FAIL" and "Total Cost holds 2847.0" in result.details[0]

    def test_a_quantity_on_a_superseded_row_fails(self):
        result = result_for(build(self._retired(Quantity=Cell(3))), "superseded_rows_carry_no_money")
        assert result.status == "FAIL" and "Quantity" in result.details[0]

    def test_a_live_row_with_money_is_not_this_checks_business(self):
        result = result_for(build(row_cells(2)), "superseded_rows_carry_no_money")
        assert result.status == "PASS" and "0 superseded" in result.summary

    def test_the_retired_row_numbered_after_the_live_box_keeps_shipments_contiguous(self):
        sheet = build(row_cells(2, Shipment=Cell(1), **{"Order ID": Cell("A"), "Tracking Number": Cell("LIVE")}),
                      self._retired(3, **{"Order ID": Cell("A")}))
        assert result_for(sheet, "shipment_numbers_contiguous").status == "PASS"


class TestReviewFindings:
    """Bugs an adversarial review found in the checks themselves, and the checks it prompted."""

    def test_a_blank_status_keeps_an_order_open_and_billable_forever(self):
        """column_shape guards with `if status and ...`, so blank slipped through; load_order_state
        then reads it as "ordered" and the order never closes."""
        sheet = build(row_cells(2, Status=Cell("")))
        assert result_for(sheet, "status_is_present").status == "FAIL"
        assert result_for(sheet, "column_shape").status == "PASS"  # why it needed its own check

    def test_an_unresolved_split_that_has_been_paid_out_books_the_whole_payout_as_profit(self):
        sheet = build(row_cells(2, Quantity=Cell("*"), **{
            "Total Cost": Cell(""), "Actual Payout": Cell(1500.0, fmt="currency"),
        }))
        assert result_for(sheet, "unresolved_split_quantity").status == "FAIL"

    def test_an_unresolved_split_awaiting_quantities_only_warns(self):
        sheet = build(row_cells(2, Quantity=Cell("*"), **{"Total Cost": Cell("")}))
        assert result_for(sheet, "unresolved_split_quantity").status == "WARN"

    def test_a_multi_sku_box_is_informational_not_a_permanent_warning(self):
        """Items boxed together legitimately share a shipment number and a tracking number. Warning
        would nag forever on a healthy ledger and make --strict exit 1 for good."""
        a = row_cells(2, **{"Order ID": Cell("C-1"), "Item Name": Cell("A"), "Tracking Number": Cell("1Z1")})
        b = row_cells(3, **{"Order ID": Cell("C-1"), "Item Name": Cell("B"), "Tracking Number": Cell("1Z1")})
        sheet = build(a, b)
        pair = [r for r in run_checks(sheet, Options()) if r.name in
                ("duplicate_shipment_lines", "duplicate_tracking_keys")]
        assert [r.status for r in pair] == ["INFO", "INFO"]
        # The point of INFO: these two cannot, on their own, make --strict exit non-zero.
        # (Scoped to this pair deliberately -- the coverage checks read the real warehouses.json /
        # cards.json, so a whole-ledger exit code would depend on the user's config, not on the code.)
        assert audit_ledger.exit_code(pair, strict=True) == 0

    def test_one_tracking_number_under_two_orders_is_a_combined_box_not_a_warning(self):
        """2026-09-18: the group combines several orders' units in one carton under one label --
        normal and permanent, so INFO (it was a WARN that nagged on every healthy ledger)."""
        a = row_cells(2, **{"Order ID": Cell("C-1"), "Tracking Number": Cell("1Z1")})
        b = row_cells(3, **{"Order ID": Cell("C-2"), "Tracking Number": Cell("1Z1")})
        result = result_for(build(a, b), "duplicate_tracking_keys")
        assert result.status == "INFO" and "combined box" in result.summary
        assert any("1Z1 is a combined box for 2 orders" in d for d in result.details)

    def test_expect_rows_is_not_swallowed_when_render_modes_disagree(self):
        """It used to return the height WARN before ever comparing, so --expect-rows reported success
        on the one ledger state that most warrants a hard stop."""
        worksheet = RenderedFakeWorksheet([header_cells(), row_cells(2)])
        grids = audit_ledger.read_grids(worksheet)
        ragged = Grids(
            formatted=grids.formatted,
            unformatted=grids.unformatted + [["extra"]],
            formula=grids.formula,
            meta=grids.meta,
        )
        result = result_for(Sheet(ragged), "row_count", Options(expect_rows=99))
        assert result.status == "FAIL" and "expected 99" in result.summary

    def test_render_modes_disagreeing_on_height_is_a_failure_not_a_warning(self):
        """sync_csv_to_ledger takes its append anchor from the formatted read's height alone."""
        worksheet = RenderedFakeWorksheet([header_cells(), row_cells(2)])
        grids = audit_ledger.read_grids(worksheet)
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
        text = audit_ledger.render_text(run_checks(build(a, b), Options()), {}, verbose=False)
        assert "informational" in text


class TestBuyingGroupPayouts:
    """Money invariants introduced with the BFMR / MaxOutDeals posting step."""

    def test_a_payout_written_in_full_to_every_row_of_one_package_is_caught(self):
        """A payout arrives per PACKAGE; a box with two items has two rows behind one tracking
        number. Writing the full amount to each books the group's money twice, and Total Profit
        re-derives nothing -- it just reads as a larger, plausible profit."""
        a = row_cells(2, **{"Order ID": Cell("O-1"), "Item Name": Cell("A"), "Tracking Number": Cell("1Z1"),
                            "Total Cost": Cell(600.0), "Actual Payout": Cell(1000.0)})
        b = row_cells(3, **{"Order ID": Cell("O-1"), "Item Name": Cell("B"), "Tracking Number": Cell("1Z1"),
                            "Total Cost": Cell(400.0), "Actual Payout": Cell(1000.0)})
        result = result_for(build(a, b), "payout_is_cost_weighted")
        assert result.status == "WARN" and "booked twice" in result.summary

    def test_a_correctly_split_payout_passes(self):
        a = row_cells(2, **{"Order ID": Cell("O-1"), "Item Name": Cell("A"), "Tracking Number": Cell("1Z1"),
                            "Total Cost": Cell(600.0), "Actual Payout": Cell(600.0)})
        b = row_cells(3, **{"Order ID": Cell("O-1"), "Item Name": Cell("B"), "Tracking Number": Cell("1Z1"),
                            "Total Cost": Cell(400.0), "Actual Payout": Cell(400.0)})
        assert result_for(build(a, b), "payout_is_cost_weighted").status == "PASS"

    def test_a_paid_row_with_a_zero_payout_is_a_fictitious_loss(self):
        """The case an earlier version of this check MISSED by testing only for blank.

        Blank makes Total Profit render blank; a literal 0 makes it compute `0 - Total Cost -
        Insurance`. Live BFMR reported three packages as paid while `amount_paid` was
        still "0.00", and the ledger booked -$1,678.46 against a $1,796 order.
        """
        sheet = build(row_cells(2, Status=Cell("paid"), **{"Actual Payout": Cell(0)}))
        result = result_for(sheet, "paid_rows_have_a_payout")
        assert result.status == "FAIL" and "fictitious" in result.details[0]

    def test_a_paid_row_awaiting_settlement_only_warns(self):
        """A group can mark a package paid minutes before it settles, and the next sync fills it -- so
        a blank is worth noticing but is not yet wrong. Staleness catches one that never fills."""
        sheet = build(row_cells(2, Status=Cell("paid"), **{"Actual Payout": Cell("")}))
        assert result_for(sheet, "paid_rows_have_a_payout").status == "WARN"

    def test_a_paid_row_with_its_payout_passes(self):
        sheet = build(row_cells(2, Status=Cell("paid"), **{"Actual Payout": Cell(1200.0, fmt="currency")}))
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
        diff = audit_ledger.diff_snapshots(before, after)
        assert len(diff["status_regressed"]) == 1
        assert "BACKWARDS" in diff["status_regressed"][0]

    def test_normal_forward_progress_is_not_a_regression(self):
        before = grids_for(row_cells(2, Status=Cell("shipped")))
        after = grids_for(row_cells(2, Status=Cell("delivered")))
        assert audit_ledger.diff_snapshots(before, after)["status_regressed"] == []

    def test_an_unchanged_status_is_not_a_regression(self):
        grids = grids_for(row_cells(2, Status=Cell("paid")))
        assert audit_ledger.diff_snapshots(grids, grids)["status_regressed"] == []

    def test_a_regression_is_called_out_in_the_rendered_diff(self):
        before = grids_for(row_cells(2, Status=Cell("paid")))
        after = grids_for(row_cells(2, Status=Cell("ordered")))
        text = audit_ledger.render_diff(audit_ledger.diff_snapshots(before, after), "before.json", 8)
        assert "BACKWARDS" in text and "REGRESSED" in text


class TestCompare:
    """The before/after diff -- the thing that actually answers "did that run update or duplicate?"."""

    def test_a_new_order_shows_as_added(self):
        before = grids_for(row_cells(2))
        after = grids_for(row_cells(2), row_cells(3))
        diff = audit_ledger.diff_snapshots(before, after)
        assert len(diff["added"]) == 1 and not diff["removed"] and not diff["changed"]
        assert diff["rows_before"] == 1 and diff["rows_after"] == 2

    def test_a_status_change_shows_as_a_changed_cell_not_an_add(self):
        before = grids_for(row_cells(2, Status=Cell("ordered")))
        after = grids_for(row_cells(2, Status=Cell("shipped")))
        diff = audit_ledger.diff_snapshots(before, after)
        assert not diff["added"] and not diff["removed"]
        assert any("Status" in c and "ordered" in c and "shipped" in c for c in diff["changed"])

    def test_a_deleted_row_shows_as_removed(self):
        diff = audit_ledger.diff_snapshots(grids_for(row_cells(2), row_cells(3)), grids_for(row_cells(2)))
        assert len(diff["removed"]) == 1

    def test_last_scraped_at_is_ignored_so_it_cannot_bury_the_real_signal(self):
        """Every touched row's timestamp changes on every run, so including it would mean every diff
        reported every row as changed."""
        before = grids_for(row_cells(2, **{"Last Scraped At": Cell("2026-08-01T00:00:00Z")}))
        after = grids_for(row_cells(2, **{"Last Scraped At": Cell("2026-08-12T00:00:00Z")}))
        assert audit_ledger.diff_snapshots(before, after)["changed"] == []


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
        sheet = build(row_cells(2, **{"COGS": Cell(""), "Actual Payout": Cell(800.0)}))

        assert result_for(sheet, "cogs_inputs_complete").status == "FAIL"

    def test_cogs_without_a_payout_is_reported_but_never_fails(self):
        """The normal state of a shipped-but-unpaid order, and at a year boundary it IS the straddle
        -- the cost and income sides fall in different tax years. Worth seeing, never worth failing."""
        sheet = build(row_cells(2, Status=Cell("shipped"), **{"Actual Payout": Cell("")}))

        result = result_for(sheet, "cogs_inputs_complete")

        assert result.status == "PASS"
        assert "no payout yet" in result.summary

    def test_a_cancelled_row_is_exempt(self):
        # It carries no money by design; flagging it would fail every audit forever.
        sheet = build(row_cells(2, Status=Cell("cancelled"), **{
            "COGS": Cell(""), "Total Cost": Cell(""), "Cashback Rate": Cell(""),
            "Actual Payout": Cell(""),
        }))

        assert result_for(sheet, "cogs_inputs_complete").status == "PASS"

    def test_a_superseded_row_is_exempt(self):
        sheet = build(row_cells(2, Status=Cell("superseded"), **{
            "COGS": Cell(""), "Total Cost": Cell(""), "Actual Payout": Cell(""),
        }))

        assert result_for(sheet, "cogs_inputs_complete").status == "PASS"

    def test_a_gift_card_row_is_not_counted_as_an_unpaid_straddle(self):
        """A gift card will NEVER have a payout of its own -- the income arrives through the order it
        funded, whose cost was netted down by the card. Counting it as a straddle would misreport the
        year-boundary number it exists to surface."""
        sheet = build(row_cells(2, **{"Buying Group": Cell("Gift Card"), "Actual Payout": Cell("")}))

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
    """A snapshot is the whole ledger, PII included, so a bare name must not land in the CWD."""

    def test_a_bare_filename_lands_under_data(self):
        assert audit_ledger._snapshot_path("before.json") == audit_ledger.SNAPSHOT_DIR / "before.json"

    def test_a_relative_directory_is_honoured(self):
        from pathlib import Path
        assert audit_ledger._snapshot_path("out/x.json") == Path("out/x.json")

    def test_an_absolute_path_is_honoured(self, tmp_path):
        target = tmp_path / "x.json"
        assert audit_ledger._snapshot_path(str(target)) == target

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


class TestCompareEmitsResults:
    """--compare used to print prose that gated nothing. Each KIND of change now has a verdict."""

    def _classify(self, before, after):
        diff = audit_ledger.diff_snapshots(grids_for(*before), grids_for(*after))
        return {r.name: r for r in audit_ledger.classify_diff(diff, Options())}

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

    def test_marking_a_row_superseded_is_the_expected_transition(self):
        """The repair blanks a terminal row's money and moves its status UP: neither a regression
        nor a hand edit."""
        before = [self._row(2, Status=Cell("delivered"), **{"Total Cost": Cell(798.0), "Quantity": Cell(3)})]
        after = [self._row(2, Status=Cell("superseded"), **{"Total Cost": Cell(""), "Quantity": Cell("")})]
        r = self._classify(before, after)
        assert "compare_terminal_money_changed" not in r and "compare_status_regressed" not in r
        assert r["compare_updated"].status == "PASS"

    def test_the_repairs_renumber_of_a_retired_row_is_info_not_an_identity_failure(self):
        before = [self._row(2, Status=Cell("delivered"), Shipment=Cell(1), **{"Tracking Number": Cell("DEAD")})]
        after = [self._row(2, Status=Cell("superseded"), Shipment=Cell(2), **{"Tracking Number": Cell("DEAD"),
                                                                              "Total Cost": Cell("")})]
        r = self._classify(before, after)
        assert r["compare_rows_retired"].status == "INFO"
        assert "compare_identity_changed" not in r and "compare_rows_removed" not in r

    def test_a_payout_landing_on_a_delivered_row_is_the_normal_sync(self):
        before = [self._row(2, Status=Cell("delivered"), **{"Actual Payout": Cell("", fmt="currency")})]
        after = [self._row(2, Status=Cell("paid"), **{"Actual Payout": Cell(900.0, fmt="currency"), "Payout Date": Cell("2026-08-30")})]
        r = self._classify(before, after)
        assert "compare_terminal_money_changed" not in r and "compare_status_regressed" not in r
        assert r["compare_updated"].status == "PASS" and "3 cell(s) updated" in r["compare_updated"].summary

    def test_a_cost_correction_on_an_open_row_is_an_ordinary_update(self):
        before = [self._row(2, Status=Cell("ordered"), **{"Total Cost": Cell(798.0)})]
        after = [self._row(2, Status=Cell("ordered"), **{"Total Cost": Cell(700.0)})]
        assert "compare_terminal_money_changed" not in self._classify(before, after)

    def test_json_output_hides_the_internal_records(self):
        import json
        diff = audit_ledger.diff_snapshots(grids_for(self._row(2)), grids_for(self._row(2)))
        payload = json.loads(audit_ledger.render_json([], {}, False, diff))
        assert "_records" not in payload["diff"] and "added" in payload["diff"]

    def test_the_verdicts_reach_the_exit_code(self):
        before, after = [self._row(2), self._row(3)], [self._row(2)]
        diff = audit_ledger.diff_snapshots(grids_for(*before), grids_for(*after))
        results = audit_ledger.classify_diff(diff, Options())
        assert audit_ledger.exit_code(results, strict=False) == 1


class TestImportedShapesAreNotFailures:
    """Three findings from the first real import (2026-08-30) that were audit bugs, not data bugs."""

    def test_a_zero_cost_bonus_with_a_payout_has_cogs_zero_not_missing(self):
        sheet = build(row_cells(2, **{"Status": Cell("paid"), "Cost Per Item": Cell(0.0), "Total Cost": Cell(0.0),
                                     "COGS": Cell(0.0, fmt="currency", formula=_cogs_formula(2)),
                                     "Actual Payout": Cell(250.0, fmt="currency"), "Payout Date": Cell("2026-03-19")}))
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

    def test_an_uneven_package_split_is_a_real_per_item_payout_info_only(self):
        sheet = build(
            row_cells(2, **{"Order ID": Cell("A"), "Tracking Number": Cell("T"), "Total Cost": Cell(59.98),
                            "Actual Payout": Cell(74.0, fmt="currency")}),
            row_cells(3, **{"Order ID": Cell("A"), "Tracking Number": Cell("T"), "Total Cost": Cell(597.0),
                            "Actual Payout": Cell(600.0, fmt="currency"), "Item Name": Cell("Watch")}),
        )
        result = result_for(sheet, "payout_is_cost_weighted")
        assert result.status == "INFO" and "per-item" in result.summary  # 2026-09-18: was a WARN


def test_a_gift_card_row_paid_with_a_zero_payout_is_by_rule():
    sheet = build(row_cells(2, **{"Status": Cell("paid"), "Buying Group": Cell("Gift Card"),
                                 "Actual Payout": Cell(0.0, fmt="currency"), "Payout Date": Cell("2026-07-08")}))
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


class TestPackageIdPerShipment:
    """Column 34 (2026-09-09): one package id under ONE Shipment number per order. Mirrors
    duplicate_tracking_keys: a multi-SKU carton is INFO, one id under two numbers is the 1f bug."""

    def _row(self, n, order="111-9990021-9990021", package_id="NxWmqLBj2", shipment=1, **extra):
        cells = {"Order ID": Cell(order), "Package ID": Cell(package_id), "Shipment": Cell(shipment)}
        cells.update(extra)
        return row_cells(n, **cells)

    def test_distinct_packages_pass(self):
        sheet = build(self._row(2, package_id="A"), self._row(3, package_id="B", shipment=2),
                      self._row(4, order="OTHER", package_id="A"))
        r = result_for(sheet, "package_id_per_shipment")
        assert r.status == "PASS" and "3 package id(s)" in r.summary

    def test_blank_ids_are_ignored(self):
        sheet = build(self._row(2, package_id=""), self._row(3, package_id="", shipment=2))
        assert result_for(sheet, "package_id_per_shipment").status == "PASS"

    def test_a_multi_sku_carton_is_info(self):
        sheet = build(self._row(2, **{"Item Name": Cell("A")}), self._row(3, **{"Item Name": Cell("B")}))
        r = result_for(sheet, "package_id_per_shipment")
        assert r.status == "INFO" and "covers rows [2, 3]" in r.details[0]

    def test_one_id_under_two_shipment_numbers_fails(self):
        """The exact 2026-08-22 shape: the same package booked as Shipment 1 AND Shipment 2."""
        sheet = build(self._row(2, shipment=1), self._row(3, shipment=2))
        r = result_for(sheet, "package_id_per_shipment")
        assert r.status == "FAIL"
        assert "package NxWmqLBj2 sits under shipments ['1', '2']" in r.details[0]

    def test_a_retired_row_keeps_its_id_under_another_number_without_failing(self):
        sheet = build(self._row(2, shipment=1),
                      self._row(3, shipment=2, Status=Cell("superseded"), **_SUPERSEDED_MONEY_BLANKS))
        assert result_for(sheet, "package_id_per_shipment").status == "PASS"

    def test_a_numeric_cell_fails(self):
        """A Costco id stored as a number has lost its leading zeros and no longer matches the mapping."""
        sheet = build(self._row(2, package_id=9999990206101794))
        r = result_for(sheet, "package_id_per_shipment")
        assert r.status == "FAIL" and "stored as int" in r.details[0]


class TestMandatoryByStage:
    """the audit checks for missing mandatory values, by stage."""

    def test_a_healthy_ledger_passes(self):
        sheet = build(row_cells(2), row_cells(3, Status=Cell("shipped"), **{"Delivery Date": Cell("")}),
                      row_cells(4, Status=Cell("cancelled"), **{"Cost Per Item": Cell(""), "Total Cost": Cell(""), "Quantity": Cell("")}))
        assert result_for(sheet, "mandatory_by_stage").status == "PASS"

    def test_a_deleted_mandatory_cell_fails_and_names_it(self):
        sheet = build(row_cells(2, Profile=Cell("")), row_cells(3, **{"Cost Per Item": Cell("")}))
        result = result_for(sheet, "mandatory_by_stage")
        assert result.status == "FAIL"
        assert any("row 2 (delivered): missing Profile" in d for d in result.details)
        assert any("row 3 (delivered): missing Cost Per Item" in d for d in result.details)

    def test_each_stage_requires_its_own_cells(self):
        shipped = build(row_cells(2, Status=Cell("shipped"), **{"Tracking Number": Cell("")}))
        assert "missing Tracking Number" in result_for(shipped, "mandatory_by_stage").details[0]
        paid = build(row_cells(2, Status=Cell("paid"), **{"Actual Payout": Cell(100.0, fmt="currency"), "Payout Date": Cell("")}))
        assert "missing Payout Date" in result_for(paid, "mandatory_by_stage").details[0]
        paid_ok = build(row_cells(2, Status=Cell("paid"), **{"Actual Payout": Cell(100.0, fmt="currency"), "Payout Date": Cell("2026-09-01"),
                                                             "Insurance": Cell(1.5, fmt="currency")}))
        assert result_for(paid_ok, "mandatory_by_stage").status == "PASS"
        assert "Insurance" in result_for(paid, "mandatory_by_stage").details[0]  # paid needs its insurance too
        returned = build(row_cells(2, Status=Cell("return"), **{"Return Qty": Cell(""), "Return Date": Cell("")}))
        assert "missing Return Qty, Return Date" in result_for(returned, "mandatory_by_stage").details[0]
        # delivered (and beyond) needs the retailer's receipt as well
        no_receipt = build(row_cells(2, **{"Receipt Link": Cell("")}))
        assert "row 2 (delivered): missing Receipt Link" in result_for(no_receipt, "mandatory_by_stage").details[0]
        shipped_no_receipt = build(row_cells(2, Status=Cell("shipped"), **{"Delivery Date": Cell(""), "Receipt Link": Cell("")}))
        assert result_for(shipped_no_receipt, "mandatory_by_stage").status == "PASS"
        # from ordered on: the order link, the delivery address, the card and its last 4, and COGS
        # shipped onward: the tracking number has been SUBMITTED (the box is ticked)
        unsubmitted = build(row_cells(2, Status=Cell("shipped"), **{"Delivery Date": Cell(""), "Tracking Submitted": Cell(False)}))
        assert "missing Tracking Submitted (not ticked)" in result_for(unsubmitted, "mandatory_by_stage").details[0]
        for name in ("Order Link", "Delivery Address", "Card", "Card Last 4", "Buying Group"):
            bare = build(row_cells(2, Status=Cell("ordered"), **{"Tracking Number": Cell(""), "Delivery Date": Cell(""), "Tracking Submitted": Cell(False), name: Cell("")}))
            assert f"row 2 (ordered): missing {name}" in result_for(bare, "mandatory_by_stage").details[0], name

    def test_a_cell_a_stage_should_not_have_yet_is_a_stale_status_warning(self):
        ordered = build(row_cells(2, Status=Cell("ordered"), **{"Delivery Date": Cell(""), "Tracking Submitted": Cell(False)}))  # keeps its tracking number
        result = result_for(ordered, "mandatory_by_stage")
        assert result.status == "WARN" and "row 2 (ordered): carries Tracking Number" in result.details[0]
        clean = build(row_cells(2, Status=Cell("ordered"), **{"Tracking Number": Cell(""), "Delivery Date": Cell(""), "Tracking Submitted": Cell(False)}))
        assert result_for(clean, "mandatory_by_stage").status == "PASS"

    def test_an_estimated_delivery_date_on_an_open_row_is_not_stale(self):
        ordered = build(row_cells(2, Status=Cell("ordered"), **{"Tracking Number": Cell(""), "Tracking Submitted": Cell(False)}))  # keeps its Delivery Date
        assert result_for(ordered, "mandatory_by_stage").status == "PASS"

    def test_a_gift_card_has_no_package_but_one_sold_to_a_group_is_submitted(self):
        from config.warehouses import GIFT_CARD

        bought = build(row_cells(2, Status=Cell("delivered"), **{"Buying Group": Cell(GIFT_CARD), "Tracking Number": Cell(""),
                                                                 "Delivery Address": Cell(""), "Delivery Date": Cell(""),
                                                                 "Tracking Submitted": Cell(False)}))
        assert result_for(bought, "mandatory_by_stage").status == "PASS"
        by_name = build(row_cells(2, Status=Cell("delivered"), **{"Item Name": Cell("Amazon.com eGift Card"), "Buying Group": Cell(""),
                                                                  "Tracking Number": Cell(""), "Delivery Address": Cell(""),
                                                                  "Tracking Submitted": Cell(False)}))
        # a gift card still names its group (the marker for a bought one, the group for a sold one)
        assert result_for(by_name, "mandatory_by_stage").details == ("row 2 (delivered): missing Buying Group",)
        sold = build(row_cells(2, Status=Cell("shipped"), **{"Item Name": Cell("Apple Gift Card $500"), "Buying Group": Cell("AI"),
                                                             "Tracking Number": Cell(""), "Delivery Address": Cell(""),
                                                             "Delivery Date": Cell(""), "Tracking Submitted": Cell(False)}))
        result = result_for(sold, "mandatory_by_stage")
        assert result.status == "FAIL" and result.details[0] == "row 2 (shipped): missing Tracking Submitted (not ticked)"
        sold_ok = build(row_cells(2, Status=Cell("shipped"), **{"Item Name": Cell("Apple Gift Card $500"), "Buying Group": Cell("AI"),
                                                                "Tracking Number": Cell(""), "Delivery Address": Cell(""),
                                                                "Delivery Date": Cell(""), "Tracking Submitted": Cell(True)}))
        assert result_for(sold_ok, "mandatory_by_stage").status == "PASS"

    def test_impossible_combinations_fail(self):
        """orders 111-9990011-9990011 said Tracking Submitted with no tracking number."""
        ticked_no_number = build(row_cells(2, Status=Cell("delivered"), **{"Tracking Number": Cell(""), "Tracking Submitted": Cell(True)}))
        result = result_for(ticked_no_number, "mandatory_by_stage")
        assert result.status == "FAIL"
        assert any("Tracking Submitted ticked with no Tracking Number" in d for d in result.details)
        ordered_ticked = build(row_cells(2, Status=Cell("ordered"), **{"Tracking Number": Cell(""), "Tracking Submitted": Cell(True)}))
        assert any("ticked on an ordered row" in d for d in result_for(ordered_ticked, "mandatory_by_stage").details)
        dated_unpaid = build(row_cells(2, **{"Payout Date": Cell("2026-08-20")}))
        assert any("a Payout Date with no Actual Payout" in d for d in result_for(dated_unpaid, "mandatory_by_stage").details)
        early = build(row_cells(2, **{"Delivery Date": Cell("2026-08-01")}))  # the order date is 2026-08-06
        assert any("Delivery Date 2026-08-01 is before the Order Date 2026-08-06" in d for d in result_for(early, "mandatory_by_stage").details)
        # a payout on a row that is not paid yet is a stale status, a warning
        paid_but_open = build(row_cells(2, Status=Cell("shipped"), **{"Delivery Date": Cell(""), "Actual Payout": Cell(100.0, fmt="currency")}))
        result = result_for(paid_but_open, "mandatory_by_stage")
        assert result.status == "WARN" and "carries Actual Payout" in result.details[0]

    def test_a_money_free_row_needs_only_its_identity(self):
        sheet = build(row_cells(2, Status=Cell("superseded"), **{"Cost Per Item": Cell(""), "Total Cost": Cell(""),
                                                                 "Quantity": Cell(""), "Profile": Cell("")}))
        assert result_for(sheet, "mandatory_by_stage").status == "PASS"
        sheet = build(row_cells(2, Status=Cell("cancelled"), Retailer=Cell("")))
        assert result_for(sheet, "mandatory_by_stage").status == "FAIL"
