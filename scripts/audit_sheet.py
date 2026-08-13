"""Read-only audit of the live Google Sheet ledger. WRITES NOTHING, EVER.

Why this exists: every schema change ended with the same manual ritual -- check the row count, look for duplicate upsert
keys, confirm the Total Profit formulas are intact, spot-check that Shipment is still an int and
Card Last 4 still has its leading zeros. Those results were written up as PROSE in the design notes and the
audit itself was thrown away each time. This is that ritual as a runnable artifact.

    python -m scripts.audit_sheet                        # audit the live sheet
    python -m scripts.audit_sheet --expect-rows 23       # ... and assert the row count
    python -m scripts.audit_sheet --save-snapshot before.json
    python -m scripts.audit_sheet --from-snapshot before.json   # re-audit offline, zero API calls
    python -m scripts.audit_sheet --json                 # machine-readable, for before/after diffs

READ-ONLY IS ENFORCED IN THREE LAYERS, because an auditor that can mutate what it audits is worse
than no auditor at all:
  1. It does NOT call sheets.ledger_sync._get_worksheet(). That function swallows WorksheetNotFound
     and CREATES the tab (ledger_sync.py:172-178) -- so a typo in GOOGLE_SHEET_WORKSHEET_NAME would
     make a fresh empty tab and this script would cheerfully report "0 rows, header OK" against a
     sheet it had just invented. `open_worksheet_readonly` below is that function minus the except.
  2. It authenticates with the READ-ONLY OAuth scope, so the capability simply isn't granted: even a
     future edit that reaches a write path gets a 403 from Google rather than changing the ledger.
  3. The checks never receive a worksheet -- only the frozen `Grids` value read once up front. No
     check *can* call a mutator because no check holds anything mutable.

ON READING FORMATTED VALUES: the design notes tells new sheet readers never to use the formatted render
mode, because that's how display formatting corrupts data on a write-back round trip. That rule is
for WRITERS. An auditor is the one place that must read all three modes, because the whole bug class
lives in the DISAGREEMENT between them -- `sync_csv_to_sheet` builds its upsert key from the formatted
read (ledger_sync.py:319), so to answer "will the next run duplicate a row?" the audit has to see
exactly the strings the writer will see, and compare them against what the cell actually stores.
That comparison is `check_key_is_format_independent`, the single most valuable check here.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Iterator

from models.order import FIELDNAMES, STATUSES
from sheets.ledger_sync import (
    HEADER,
    _INT_FIELDS,
    _NUMERIC_FIELDS,
    _parse_display_number,
    _profit_formula,
)

# Read-only counterpart to ledger_sync.SCOPES. This is the layer that makes "read-only" a capability
# rather than a promise -- see the module docstring.
READONLY_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# Sheets' date epoch, for reporting what day a stray date serial actually means.
_SHEETS_EPOCH = date(1899, 12, 30)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Columns that must hold plain ISO text, not a date serial. Order Date is the dangerous one: it's in
# the primary upsert key AND the name-agnostic fallback key (ledger_sync.py:273, :297).
_DATE_COLUMNS = ("Order Date", "Delivery Date", "Payout Date")

# Display-name lookups for the field-keyed sets imported from ledger_sync.
_HEADER_FOR_FIELD = dict(zip(FIELDNAMES, HEADER))


# --------------------------------------------------------------------------------------------------
# Data acquisition
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Grids:
    """The same sheet read three ways. The disagreements between them are the point.

    formatted   FORMATTED_VALUE   -- always str. "4%", "$1,299.00"; a formula cell shows its RESULT.
                                    This is what sync_csv_to_sheet sees and keys rows on.
    unformatted UNFORMATTED_VALUE -- real types: 0.04, 1299.0, and a date SERIAL if a date column was
                                    formatted as a Date. The only mode that reveals a cell's type.
    formula     FORMULA           -- the literal "=LET(...)" for a formula cell. The only mode that
                                    can tell a live formula from one frozen into a number.
    """

    formatted: list[list[str]]
    unformatted: list[list[Any]]
    formula: list[list[Any]]
    meta: dict = field(default_factory=dict)

    def to_snapshot(self) -> dict:
        return {
            "meta": self.meta,
            "formatted": self.formatted,
            "unformatted": self.unformatted,
            "formula": self.formula,
        }

    @classmethod
    def from_snapshot(cls, payload: dict) -> "Grids":
        return cls(
            formatted=payload["formatted"],
            unformatted=payload["unformatted"],
            formula=payload["formula"],
            meta=payload.get("meta", {}),
        )


def open_worksheet_readonly():
    """Open the ledger worksheet with read-only credentials.

    Deliberately NOT sheets.ledger_sync._get_worksheet(): that one creates the worksheet when it's
    missing (ledger_sync.py:172-178), which would let this script audit a tab it had just fabricated.
    Here a missing tab is a hard stop, and nothing is created.
    """
    import gspread
    from google.oauth2.service_account import Credentials

    from config.settings import settings

    creds = Credentials.from_service_account_file(
        settings.google_service_account_file, scopes=READONLY_SCOPES
    )
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(settings.google_sheet_id)
    name = settings.google_sheet_worksheet_name
    try:
        worksheet = spreadsheet.worksheet(name)
    except gspread.WorksheetNotFound:
        raise SystemExit(
            f"No worksheet named {name!r} in spreadsheet {settings.google_sheet_id!r}. "
            "Nothing was created -- check GOOGLE_SHEET_WORKSHEET_NAME."
        )
    return worksheet, spreadsheet.title


def read_grids(worksheet, spreadsheet_title: str = "") -> Grids:
    """Read the worksheet once per render mode. The ONLY function that touches the live sheet."""
    from gspread.utils import ValueRenderOption

    formatted = worksheet.get_values(value_render_option=ValueRenderOption.formatted)
    unformatted = worksheet.get_values(value_render_option=ValueRenderOption.unformatted)
    formula = worksheet.get_values(value_render_option=ValueRenderOption.formula)
    return Grids(
        formatted=formatted,
        unformatted=unformatted,
        formula=formula,
        meta={
            "spreadsheet": spreadsheet_title,
            "worksheet": getattr(worksheet, "title", ""),
            "rows": max(0, len(formatted) - 1),
            "cols": len(formatted[0]) if formatted else 0,
        },
    )


# --------------------------------------------------------------------------------------------------
# The in-memory sheet the checks operate on
# --------------------------------------------------------------------------------------------------


def _text(value) -> str:
    """Stringify an unformatted cell the way a FORMATTED read would render it.

    Mirrors ledger_sync._cell_text's rule: a whole float comes back without the trailing ".0" (the
    cell holds 1, the API hands back "1"). Used only to compare the two grids, never to write.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class Sheet:
    """The three grids plus the index arithmetic every check needs."""

    def __init__(self, grids: Grids):
        self.grids = grids
        self.header: list[str] = list(grids.formatted[0]) if grids.formatted else []
        self.schema_ok = self.header == list(HEADER)

    def col(self, name: str) -> int | None:
        try:
            return self.header.index(name)
        except ValueError:
            return None

    def cell(self, grid: list[list], row_number: int, name: str):
        """The value at (row_number, column `name`), or "" when the row is short.

        Short rows are read as "" rather than skipped -- the SAME rule as ledger_sync.py:319 -- so the
        audit sees legacy pre-Shipment rows exactly as the upsert does.
        """
        index = self.col(name)
        if index is None:
            return ""
        row = grid[row_number - 1] if row_number - 1 < len(grid) else []
        return row[index] if index < len(row) else ""

    def rows(self, grid: list[list]) -> Iterator[tuple[int, list]]:
        """(row_number, row) for every data row, row_number being the 1-based sheet row."""
        for row_number, row in enumerate(grid[1:], start=2):
            yield row_number, row

    def ledger_rows(self, grid: list[list]) -> Iterator[tuple[int, list]]:
        """Data rows with a non-blank Order ID -- the ones the upsert will actually consider."""
        for row_number, row in self.rows(grid):
            if str(self.cell(grid, row_number, "Order ID")).strip():
                yield row_number, row

    def primary_key(self, grid, row_number) -> tuple:
        """ledger_sync.py:273 -- Order ID + Order Date + Item Name + Shipment."""
        return tuple(
            self.cell(grid, row_number, c)
            for c in ("Order ID", "Order Date", "Item Name", "Shipment")
        )

    def shipment_key(self, grid, row_number) -> tuple:
        """ledger_sync.py:297 -- the name-agnostic fallback."""
        return tuple(
            self.cell(grid, row_number, c) for c in ("Order ID", "Order Date", "Shipment")
        )

    def tracking_key(self, grid, row_number) -> tuple | None:
        """ledger_sync.py:326 -- (Order ID, Tracking Number), or None when untracked."""
        tracking = str(self.cell(grid, row_number, "Tracking Number")).strip()
        if not tracking:
            return None
        return (str(self.cell(grid, row_number, "Order ID")), tracking)


# --------------------------------------------------------------------------------------------------
# Check registry
# --------------------------------------------------------------------------------------------------

Status = str  # "PASS" | "WARN" | "FAIL" | "SKIP" | "INFO"


@dataclass(frozen=True)
class Result:
    name: str
    status: Status
    summary: str
    details: tuple[str, ...] = ()


@dataclass(frozen=True)
class Options:
    expect_rows: int | None = None
    strict: bool = False
    max_detail: int = 8


CHECKS: list[tuple[str, bool, Callable]] = []


def check(name: str, requires_schema: bool = True):
    """Register a check.

    requires_schema=True means the check reads columns by index, so it must not run when the header
    doesn't match HEADER -- the indices would point at the wrong columns and it would report a
    confident, wrong answer. Those checks report SKIP instead.
    """

    def decorate(fn):
        CHECKS.append((name, requires_schema, fn))
        return fn

    return decorate


def _truncate(items: list[str], limit: int) -> tuple[str, ...]:
    if len(items) <= limit:
        return tuple(items)
    return tuple(items[:limit] + [f"... and {len(items) - limit} more"])


# --------------------------------------------------------------------------------------------------
# Structural
# --------------------------------------------------------------------------------------------------


@check("header_matches_schema", requires_schema=False)
def check_header(sheet: Sheet, opts: Options) -> Result:
    """The guard at ledger_sync.py:286-293, checkable BEFORE a run instead of during one.

    Right names in the wrong order is the worst failure this codebase has: every header.index()
    succeeds, the code looks healthy, and the positional write scrambles every field of every row it
    touches with no exception and no log entry.
    """
    if sheet.schema_ok:
        return Result("header_matches_schema", "PASS", f"{len(HEADER)}/{len(HEADER)} columns, canonical order")
    expected, actual = list(HEADER), sheet.header
    details = []
    for name in expected:
        if name not in actual:
            details.append(f"MISSING  {name!r}")
    for name in actual:
        if name not in expected:
            details.append(f"UNKNOWN  {name!r}")
    for i, name in enumerate(expected):
        if name in actual and actual.index(name) != i:
            details.append(f"MOVED    {name!r}: column {actual.index(name) + 1}, expected {i + 1}")
    details.append("Fix: python -m scripts.reorder_sheet  (then --apply)")
    return Result("header_matches_schema", "FAIL", "header does not match the schema", _truncate(details, 20))


@check("row_count", requires_schema=False)
def check_row_count(sheet: Sheet, opts: Options) -> Result:
    total = max(0, len(sheet.grids.formatted) - 1)
    ledger = sum(1 for _ in sheet.ledger_rows(sheet.grids.formatted)) if sheet.schema_ok else total
    heights = {len(sheet.grids.formatted), len(sheet.grids.unformatted), len(sheet.grids.formula)}
    if len(heights) > 1:
        return Result("row_count", "WARN", f"render modes disagree on height: {sorted(heights)}")
    summary = f"{total} data rows ({ledger} with an Order ID)"
    if opts.expect_rows is not None:
        if total != opts.expect_rows:
            return Result("row_count", "FAIL", f"{summary} -- expected {opts.expect_rows}")
        summary += f" (expected {opts.expect_rows})"
    return Result("row_count", "PASS", summary)


# --------------------------------------------------------------------------------------------------
# Identity / duplication -- the "0 appended" proof
# --------------------------------------------------------------------------------------------------


@check("duplicate_primary_keys")
def check_duplicate_primary_keys(sheet: Sheet, opts: Options) -> Result:
    """The runnable form of "zero duplicate upsert keys"."""
    grid = sheet.grids.formatted
    seen: dict[tuple, list[int]] = {}
    for row_number, _ in sheet.ledger_rows(grid):
        seen.setdefault(sheet.primary_key(grid, row_number), []).append(row_number)
    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    if not dupes:
        return Result(
            "duplicate_primary_keys", "PASS",
            f"{len(seen)} unique (Order ID, Order Date, Item Name, Shipment)",
        )
    details = [f"rows {v}: {k}" for k, v in dupes.items()]
    return Result("duplicate_primary_keys", "FAIL", f"{len(dupes)} duplicated key(s)", _truncate(details, opts.max_detail))


@check("key_is_format_independent")
def check_key_is_format_independent(sheet: Sheet, opts: Options) -> Result:
    """Does each row's upsert key depend on how its cells are FORMATTED?

    This is the generic form of the entire §8/§9 bug family, in one invariant. sync_csv_to_sheet keys
    rows on the FORMATTED read, so if a key cell holds anything other than plain text, the string the
    writer sees can change when the user changes a number format -- and a key that changes means the
    next re-check APPENDS a duplicate instead of updating.

    It fires on a date column re-formatted as a real Date (formatted "8/6/2026" vs the stored serial
    46610), on a Shipment stored as 2.0, and on anything else non-textual that landed in a key column.
    the design notes removed the code that defended against this, so this check is now the only thing
    standing between the user and that regression.
    """
    offenders = []
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        shown = sheet.primary_key(sheet.grids.formatted, row_number)
        stored = tuple(_text(v) for v in sheet.primary_key(sheet.grids.unformatted, row_number))
        if shown != stored:
            differing = [
                f"{c}: displays {s!r} but stores {t!r}"
                for c, s, t in zip(("Order ID", "Order Date", "Item Name", "Shipment"), shown, stored)
                if s != t
            ]
            offenders.append(f"row {row_number}: " + "; ".join(differing))
    if not offenders:
        return Result("key_is_format_independent", "PASS", "every key cell is plain text")
    return Result(
        "key_is_format_independent", "FAIL",
        f"{len(offenders)} row(s) whose identity depends on cell formatting -- "
        "re-formatting will make the next re-check append a duplicate",
        _truncate(offenders, opts.max_detail),
    )


@check("duplicate_shipment_lines")
def check_duplicate_shipment_lines(sheet: Sheet, opts: Options) -> Result:
    """(Order ID, Order Date, Shipment) collisions are LEGAL -- one shipment can hold several SKUs.

    But they disable the name-agnostic fallback merge, which only fires on a unique 1:1 match. So a
    re-worded item name on such a shipment appends instead of merging. Worth knowing, not a failure.
    """
    grid = sheet.grids.formatted
    seen: dict[tuple, list[int]] = {}
    for row_number, _ in sheet.ledger_rows(grid):
        seen.setdefault(sheet.shipment_key(grid, row_number), []).append(row_number)
    multi = {k: v for k, v in seen.items() if len(v) > 1}
    if not multi:
        return Result("duplicate_shipment_lines", "PASS", "every shipment line holds exactly one item")
    details = [f"rows {v}: order {k[0]} shipment {k[2]!r}" for k, v in multi.items()]
    return Result(
        "duplicate_shipment_lines", "WARN",
        f"{len(multi)} shipment line(s) hold several items -- the name-agnostic merge can't fire there",
        _truncate(details, opts.max_detail),
    )


@check("duplicate_tracking_keys")
def check_duplicate_tracking_keys(sheet: Sheet, opts: Options) -> Result:
    """(Order ID, Tracking Number) is the strongest reconciliation rule (ledger_sync.py:396)."""
    grid = sheet.grids.formatted
    seen: dict[tuple, list[int]] = {}
    by_number: dict[str, set] = {}
    for row_number, _ in sheet.ledger_rows(grid):
        key = sheet.tracking_key(grid, row_number)
        if key is None:
            continue
        seen.setdefault(key, []).append(row_number)
        by_number.setdefault(key[1], set()).add(key[0])
    details = [f"rows {v}: order {k[0]} tracking {k[1]}" for k, v in seen.items() if len(v) > 1]
    details += [
        f"tracking {number} appears under {len(orders)} different Order IDs: {sorted(orders)}"
        for number, orders in by_number.items() if len(orders) > 1
    ]
    if not details:
        return Result("duplicate_tracking_keys", "PASS", f"{len(seen)} tracked row(s), all 1:1")
    return Result(
        "duplicate_tracking_keys", "WARN",
        "tracking-number reconciliation is ambiguous for some rows",
        _truncate(details, opts.max_detail),
    )


@check("blank_order_id_rows")
def check_blank_order_id_rows(sheet: Sheet, opts: Options) -> Result:
    """A row with no Order ID can never be updated again -- both ledger_sync.py:317 and :360 skip it.

    It's a permanent orphan: every future re-check appends alongside it instead of updating it.
    """
    offenders = []
    for row_number, row in sheet.rows(sheet.grids.formatted):
        if not any(str(c).strip() for c in row):
            continue  # a wholly blank row is padding, not an orphan
        if not str(sheet.cell(sheet.grids.formatted, row_number, "Order ID")).strip():
            item = sheet.cell(sheet.grids.formatted, row_number, "Item Name")
            offenders.append(f"row {row_number}: {item!r}")
    if not offenders:
        return Result("blank_order_id_rows", "PASS", "every non-empty row carries an Order ID")
    return Result(
        "blank_order_id_rows", "FAIL",
        f"{len(offenders)} orphan row(s) with no Order ID -- they can never be updated",
        _truncate(offenders, opts.max_detail),
    )


# --------------------------------------------------------------------------------------------------
# Formula integrity
# --------------------------------------------------------------------------------------------------


@check("profit_formula_coverage")
def check_profit_formula_coverage(sheet: Sheet, opts: Options) -> Result:
    """"23/23 formulas intact" -- a frozen cell looks normal and just stops updating.

    ledger_sync.py:520-524: a FORMATTED read carries the formula's evaluated NUMBER forward, and the
    RAW row write freezes it into place. Only the FORMULA render mode can tell the difference.
    """
    missing, total = [], 0
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        total += 1
        value = sheet.cell(sheet.grids.formula, row_number, "Total Profit")
        if not (isinstance(value, str) and value.startswith("=")):
            missing.append(f"row {row_number}: holds {value!r} instead of a formula")
    if not missing:
        return Result("profit_formula_coverage", "PASS", f"{total}/{total} rows carry a formula")
    return Result(
        "profit_formula_coverage", "FAIL",
        f"{total - len(missing)}/{total} rows carry a formula",
        _truncate(missing, opts.max_detail),
    )


@check("profit_formula_literal")
def check_profit_formula_literal(sheet: Sheet, opts: Options) -> Result:
    """Nothing else in the repo can catch a STALE formula after a column reorder.

    The formula addresses columns by LETTER, and the design notes records those letters moving twice in one
    day. A stale formula still evaluates and still shows a plausible dollar figure -- it's just
    silently pointed at the wrong cells. Deriving the expectation from _profit_formula rather than
    hardcoding it means this check follows any future reorder automatically.
    """
    wrong, total = [], 0
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        value = sheet.cell(sheet.grids.formula, row_number, "Total Profit")
        if not (isinstance(value, str) and value.startswith("=")):
            continue  # coverage check owns that failure
        total += 1
        expected = _profit_formula(row_number)
        if value != expected:
            wrong.append(f"row {row_number}:\n      is:       {value}\n      should be: {expected}")
    if not wrong:
        return Result("profit_formula_literal", "PASS", f"{total}/{total} match _profit_formula(row)")
    return Result(
        "profit_formula_literal", "FAIL",
        f"{len(wrong)}/{total} formula(s) differ from the current schema -- likely stale after a reorder",
        _truncate(wrong, min(opts.max_detail, 4)),
    )


@check("no_stray_formulas")
def check_no_stray_formulas(sheet: Sheet, opts: Options) -> Result:
    """A hand-written formula anywhere but Total Profit gets flattened to text by the next RAW write."""
    profit_index = sheet.col("Total Profit")
    offenders = []
    for row_number, row in sheet.rows(sheet.grids.formula):
        for index, value in enumerate(row):
            if index == profit_index:
                continue
            if isinstance(value, str) and value.startswith("="):
                name = sheet.header[index] if index < len(sheet.header) else f"col {index + 1}"
                offenders.append(f"row {row_number}, {name}: {value}")
    if not offenders:
        return Result("no_stray_formulas", "PASS", "no hand-written formulas outside Total Profit")
    return Result(
        "no_stray_formulas", "FAIL",
        f"{len(offenders)} formula(s) outside Total Profit -- the next sync flattens them to text",
        _truncate(offenders, opts.max_detail),
    )


# --------------------------------------------------------------------------------------------------
# Type integrity -- the reason the unformatted grid is read
# --------------------------------------------------------------------------------------------------


@check("shipment_is_int")
def check_shipment_is_int(sheet: Sheet, opts: Options) -> Result:
    """§9 coerced Shipment to a plain int (_INT_FIELDS, ledger_sync.py:70).

    Also cross-checks the FORMATTED text, because that string IS part of the upsert key: it must be
    bare digits -- no "Shipment " prefix (the pre-2026-08-12 spelling) and no trailing ".0".
    """
    counts = {"int": 0, "float": 0, "text": 0, "label": 0, "blank": 0}
    offenders = []
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        stored = sheet.cell(sheet.grids.unformatted, row_number, "Shipment")
        shown = str(sheet.cell(sheet.grids.formatted, row_number, "Shipment"))
        if stored == "" or stored is None:
            counts["blank"] += 1
        elif isinstance(stored, bool):
            counts["text"] += 1
        elif isinstance(stored, int):
            counts["int"] += 1
        elif isinstance(stored, float):
            counts["float"] += 1
            offenders.append(f"row {row_number}: stored as float {stored!r}, should be int")
        elif str(stored).strip().isdigit():
            counts["text"] += 1
            offenders.append(f"row {row_number}: stored as text {stored!r}, should be int")
        else:
            counts["label"] += 1  # a non-numeric fallback label is allowed (normalize_shipment)
        if shown and not shown.isdigit():
            offenders.append(f"row {row_number}: displays {shown!r} -- the key wants bare digits")
    summary = " | ".join(f"{v} {k}" for k, v in counts.items() if v)
    if not offenders:
        return Result("shipment_is_int", "PASS", summary or "no rows")
    return Result("shipment_is_int", "FAIL", summary, _truncate(offenders, opts.max_detail))


@check("card_last4_is_text")
def check_card_last4_is_text(sheet: Sheet, opts: Options) -> Result:
    """Card Last 4 is deliberately EXCLUDED from _NUMERIC_FIELDS so "0315" stays "0315".

    An int here means the leading zero is already gone, and the real digits are unrecoverable.
    """
    offenders, values = [], set()
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        stored = sheet.cell(sheet.grids.unformatted, row_number, "Card Last 4")
        if stored == "" or stored is None:
            continue
        if isinstance(stored, (int, float)) and not isinstance(stored, bool):
            offenders.append(f"row {row_number}: stored as {type(stored).__name__} {stored!r} -- leading zeros lost")
        else:
            values.add(str(stored))
    leading_zero = sorted(v for v in values if v.startswith("0"))
    summary = f"{len(values)} distinct value(s), all text"
    if leading_zero:
        summary += f" (leading zeros intact: {', '.join(leading_zero)})"
    if not offenders:
        return Result("card_last4_is_text", "PASS", summary)
    return Result("card_last4_is_text", "FAIL", f"{len(offenders)} numeric cell(s)", _truncate(offenders, opts.max_detail))


@check("numeric_columns_are_numeric")
def check_numeric_columns(sheet: Sheet, opts: Options) -> Result:
    """A text value in a money column stops it summing -- the number this ledger exists to get right.

    A string that _parse_display_number CAN parse ("$3,402.00", "4%") is the §8 corruption signature
    exactly: a formatted value that was read back and rewritten as literal text.
    """
    columns = [
        _HEADER_FOR_FIELD[f] for f in _NUMERIC_FIELDS
        if f not in _INT_FIELDS and f in _HEADER_FOR_FIELD
    ]
    offenders, checked = [], 0
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        for name in sorted(columns):
            stored = sheet.cell(sheet.grids.unformatted, row_number, name)
            if stored == "" or stored is None:
                continue
            checked += 1
            if isinstance(stored, (int, float)) and not isinstance(stored, bool):
                continue
            if name == "Quantity" and str(stored).strip() == "*":
                continue  # the documented undisclosed-split marker
            hint = ""
            if _parse_display_number(stored) is not None:
                hint = " -- a formatted value written back as literal TEXT"
            offenders.append(f"row {row_number}, {name}: text {stored!r}{hint}")
    if not offenders:
        return Result("numeric_columns_are_numeric", "PASS", f"{checked} numeric cell(s) across {len(columns)} columns")
    return Result("numeric_columns_are_numeric", "FAIL", f"{len(offenders)} text cell(s) in numeric columns", _truncate(offenders, opts.max_detail))


@check("dates_are_iso_text")
def check_dates_are_iso_text(sheet: Sheet, opts: Options) -> Result:
    """THE MARQUEE CHECK. §9 removed the code that tolerated date serials, at the user's request.

    Order Date is in the primary upsert key and in the name-agnostic fallback key, so if these columns
    are ever formatted as real Dates again, a row without a tracking number appends a duplicate on its
    next re-check. That was already true on the live sheet once (§8). Nothing in the codebase defends
    against it any more -- this check is the defence.
    """
    offenders, warnings, checked = [], [], 0
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        for name in _DATE_COLUMNS:
            stored = sheet.cell(sheet.grids.unformatted, row_number, name)
            if stored == "" or stored is None:
                continue
            checked += 1
            if isinstance(stored, str) and _ISO_DATE.match(stored.strip()):
                continue
            if isinstance(stored, (int, float)) and not isinstance(stored, bool):
                as_day = _SHEETS_EPOCH + timedelta(days=int(stored))
                message = f"row {row_number}, {name}: date SERIAL {stored!r} (= {as_day.isoformat()}) -- the column is formatted as a Date"
            else:
                message = f"row {row_number}, {name}: {stored!r} is not ISO YYYY-MM-DD"
            (offenders if name == "Order Date" else warnings).append(message)
    if offenders:
        return Result(
            "dates_are_iso_text", "FAIL",
            f"{len(offenders)} Order Date cell(s) are not plain ISO text -- re-checks will duplicate rows",
            _truncate(offenders + warnings, opts.max_detail),
        )
    if warnings:
        return Result("dates_are_iso_text", "WARN", f"{len(warnings)} non-ISO date cell(s) outside Order Date", _truncate(warnings, opts.max_detail))
    return Result("dates_are_iso_text", "PASS", f"{checked} date cell(s), all plain ISO text")


# --------------------------------------------------------------------------------------------------
# Content sanity / anti-scramble canaries
# --------------------------------------------------------------------------------------------------


@check("no_embedded_newlines")
def check_no_embedded_newlines(sheet: Sheet, opts: Options) -> Result:
    """A newline in Item Name changes the upsert key and duplicates the row; elsewhere it's cosmetic."""
    fails, warns = [], []
    for row_number, row in sheet.rows(sheet.grids.formatted):
        for index, value in enumerate(row):
            if "\n" not in str(value):
                continue
            name = sheet.header[index] if index < len(sheet.header) else f"col {index + 1}"
            message = f"row {row_number}, {name}: {value!r}"
            (fails if name == "Item Name" else warns).append(message)
    if fails:
        return Result("no_embedded_newlines", "FAIL", f"{len(fails)} newline(s) in Item Name -- that's in the upsert key", _truncate(fails + warns, opts.max_detail))
    if warns:
        return Result("no_embedded_newlines", "WARN", f"{len(warns)} cell(s) contain newlines", _truncate(warns, opts.max_detail))
    return Result("no_embedded_newlines", "PASS", "0 cells")


@check("key_cells_have_no_edge_whitespace")
def check_key_whitespace(sheet: Sheet, opts: Options) -> Result:
    """ledger_sync.py:319 builds the key from the raw cell with NO .strip(), so a trailing space is
    an invisible, guaranteed duplicate."""
    offenders = []
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        for name in ("Order ID", "Order Date", "Item Name", "Shipment"):
            value = str(sheet.cell(sheet.grids.formatted, row_number, name))
            if value != value.strip():
                offenders.append(f"row {row_number}, {name}: {value!r}")
    if not offenders:
        return Result("key_cells_have_no_edge_whitespace", "PASS", "no stray leading/trailing spaces in key cells")
    return Result("key_cells_have_no_edge_whitespace", "FAIL", f"{len(offenders)} key cell(s) with edge whitespace", _truncate(offenders, opts.max_detail))


@check("column_shape")
def check_column_shape(sheet: Sheet, opts: Options) -> Result:
    """The scramble canary -- "Order Links still URLs and Card Last 4 still digits".

    After a positional scramble every individual cell is still plausible text; only per-column shape
    assertions notice that Delivery Address ended up in the Status column.
    """
    fails, warns = [], []
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        for name in ("Order Link", "Tracking Link"):
            value = str(sheet.cell(sheet.grids.formatted, row_number, name)).strip()
            if value and not value.startswith(("http://", "https://")):
                fails.append(f"row {row_number}, {name}: {value!r} is not a URL")
        status = str(sheet.cell(sheet.grids.formatted, row_number, "Status")).strip().lower()
        if status and status not in STATUSES:
            fails.append(f"row {row_number}, Status: {status!r} not in {STATUSES} -- the order stays open forever")
        last4 = str(sheet.cell(sheet.grids.formatted, row_number, "Card Last 4")).strip()
        if last4 and not last4.isdigit():
            warns.append(f"row {row_number}, Card Last 4: {last4!r} is not digits")
    if fails:
        return Result("column_shape", "FAIL", f"{len(fails)} column(s) hold the wrong shape of value", _truncate(fails + warns, opts.max_detail))
    if warns:
        return Result("column_shape", "WARN", f"{len(warns)} odd value(s)", _truncate(warns, opts.max_detail))
    return Result("column_shape", "PASS", "links are URLs | statuses known | card digits well-formed")


@check("shipped_rows_have_tracking")
def check_shipped_rows_have_tracking(sheet: Sheet, opts: Options) -> Result:
    """OrderItem._shipped_requires_tracking makes shipped-without-tracking unreachable from every
    producer, so finding one means a hand edit or a row written before that rule existed."""
    fails, warns = [], []
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        status = str(sheet.cell(sheet.grids.formatted, row_number, "Status")).strip().lower()
        tracking = str(sheet.cell(sheet.grids.formatted, row_number, "Tracking Number")).strip()
        delivered_on = str(sheet.cell(sheet.grids.formatted, row_number, "Delivery Date")).strip()
        if status == "shipped" and not tracking:
            fails.append(f"row {row_number}: shipped with no tracking number")
        if status == "delivered" and not delivered_on:
            warns.append(f"row {row_number}: delivered with no delivery date")
    if fails:
        return Result("shipped_rows_have_tracking", "FAIL", f"{len(fails)} shipped row(s) with no tracking number", _truncate(fails + warns, opts.max_detail))
    if warns:
        return Result("shipped_rows_have_tracking", "WARN", f"{len(warns)} delivered row(s) with no delivery date", _truncate(warns, opts.max_detail))
    return Result("shipped_rows_have_tracking", "PASS", "status, tracking and delivery date are consistent")


@check("total_cost_matches_quantity")
def check_total_cost(sheet: Sheet, opts: Options) -> Result:
    """models.order._compute_total_cost guarantees Total Cost == Quantity x Cost Per Item at write
    time, so a divergence is a hand edit or a partial-merge artifact."""
    offenders, checked = [], 0
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        qty = sheet.cell(sheet.grids.unformatted, row_number, "Quantity")
        cpi = sheet.cell(sheet.grids.unformatted, row_number, "Cost Per Item")
        total = sheet.cell(sheet.grids.unformatted, row_number, "Total Cost")
        numbers = [qty, cpi, total]
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in numbers):
            continue
        checked += 1
        if abs(total - round(qty * cpi, 2)) > 0.01:
            offenders.append(f"row {row_number}: {qty} x {cpi} = {round(qty * cpi, 2)}, but Total Cost is {total}")
    if not offenders:
        return Result("total_cost_matches_quantity", "PASS", f"{checked} row(s) reconcile")
    return Result("total_cost_matches_quantity", "WARN", f"{len(offenders)} row(s) don't reconcile", _truncate(offenders, opts.max_detail))


@check("shipping_is_order_level")
def check_shipping_is_order_level(sheet: Sheet, opts: Options) -> Result:
    """_profit_formula's pro-rata SUMIF assumes every row of an order repeats the SAME order-level
    shipping total. Violate that and the profit column is quietly wrong with nothing to surface it."""
    by_order: dict[str, set] = {}
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        order_id = str(sheet.cell(sheet.grids.formatted, row_number, "Order ID")).strip()
        shipping = sheet.cell(sheet.grids.unformatted, row_number, "Shipping")
        by_order.setdefault(order_id, set()).add(_text(shipping))
    offenders = [f"order {oid}: rows disagree -- {sorted(values)}" for oid, values in by_order.items() if len(values) > 1]
    if not offenders:
        return Result("shipping_is_order_level", "PASS", f"{len(by_order)} order(s) repeat one shipping total")
    return Result(
        "shipping_is_order_level", "WARN",
        f"{len(offenders)} order(s) carry inconsistent shipping -- pro-rata profit will be wrong there",
        _truncate(offenders, opts.max_detail),
    )


# --------------------------------------------------------------------------------------------------
# Coverage -- delegate to the existing read-only planners rather than reimplementing them
# --------------------------------------------------------------------------------------------------


@check("buying_group_coverage")
def check_buying_group_coverage(sheet: Sheet, opts: Options) -> Result:
    from config.warehouses import load_warehouses
    from sheets.ledger_sync import plan_buying_group_retag

    warehouses = load_warehouses()
    if not warehouses:
        return Result("buying_group_coverage", "SKIP", "no warehouses.json -- nothing to classify against")
    plan = plan_buying_group_retag(sheet.header, sheet.grids.formatted[1:], warehouses)
    counts = " | ".join(f"{g} {c}" for g, c in sorted(plan["group_counts"].items()))
    details, status = [], "PASS"
    if plan["updates"]:
        status = "WARN"
        details.append(f"{len(plan['updates'])} row(s) would be retagged -- run `python -m scripts.retag_buying_groups`")
    if plan["deletions"]:
        status = "WARN"
        details.append(f"{len(plan['deletions'])} row(s) classify as Personal and shouldn't be in the ledger")
    unclassified = plan["group_counts"].get("Unclassified", 0)
    if unclassified:
        status = "WARN"
        details.append(f"{unclassified} row(s) Unclassified -- a real warehouse may be missing from warehouses.json")
    return Result("buying_group_coverage", status, counts or "no rows", _truncate(details, opts.max_detail))


@check("card_and_rate_coverage")
def check_card_and_rate_coverage(sheet: Sheet, opts: Options) -> Result:
    from config.cards import load_cards
    from config.settings import settings
    from scripts.backfill_profit_columns import plan_profit_backfill

    cards = load_cards()
    if not cards:
        return Result("card_and_rate_coverage", "SKIP", "no cards.json -- nothing to resolve against")
    plan = plan_profit_backfill(
        sheet.header, sheet.grids.unformatted[1:], cards, settings.default_cashback_rate, refresh=True
    )
    details, status = [], "PASS"
    for label, key in (("never filled", "fills"), ("disagree with cards.json", "changes")):
        entries = plan.get(key) or []
        if entries:
            status = "WARN"
            details.append(f"{len(entries)} cell(s) {label} -- run `python -m scripts.backfill_profit_columns`")
    unresolved = plan.get("unresolved") or []
    if unresolved:
        status = "WARN"
        details.append(f"{len(unresolved)} card last-4(s) not in cards.json")
    return Result("card_and_rate_coverage", status, "Card + Cashback Rate resolve cleanly" if status == "PASS" else "gaps found", _truncate(details, opts.max_detail))


# --------------------------------------------------------------------------------------------------
# Running and rendering
# --------------------------------------------------------------------------------------------------


def run_checks(sheet: Sheet, opts: Options) -> list[Result]:
    results = []
    for name, requires_schema, fn in CHECKS:
        if requires_schema and not sheet.schema_ok:
            results.append(Result(name, "SKIP", "header doesn't match the schema -- indices would be wrong"))
            continue
        try:
            results.append(fn(sheet, opts))
        except Exception as exc:  # a broken check must not hide the checks after it
            results.append(Result(name, "FAIL", f"check raised {type(exc).__name__}: {exc}"))
    return results


def exit_code(results: list[Result], strict: bool) -> int:
    if any(r.status == "FAIL" for r in results):
        return 1
    if strict and any(r.status == "WARN" for r in results):
        return 1
    return 0


def render_text(results: list[Result], meta: dict, verbose: bool) -> str:
    lines = []
    title = meta.get("spreadsheet") or "?"
    tab = meta.get("worksheet") or "?"
    lines.append(f'Ledger audit -- tab "{tab}" of "{title}"')
    lines.append(f"{meta.get('rows', '?')} data rows x {meta.get('cols', '?')} cols | 3 render modes | 0 writes")
    if meta.get("source"):
        lines.append(f"source: {meta['source']}")
    lines.append("")
    width = max((len(r.name) for r in results), default=0)
    for result in results:
        lines.append(f"{result.status:<5} {result.name:<{width}}  {result.summary}")
        if result.details and (result.status != "PASS" or verbose):
            for detail in result.details:
                lines.append(f"      {detail}")
    lines.append("")
    tally = {s: sum(1 for r in results if r.status == s) for s in ("PASS", "WARN", "FAIL", "SKIP")}
    lines.append(
        f"{tally['PASS']} passed | {tally['WARN']} warnings | {tally['FAIL']} failures"
        + (f" | {tally['SKIP']} skipped" if tally["SKIP"] else "")
    )
    return "\n".join(lines)


def render_json(results: list[Result], meta: dict, strict: bool) -> str:
    return json.dumps(
        {
            "ok": exit_code(results, strict) == 0,
            "meta": meta,
            "checks": [
                {"name": r.name, "status": r.status, "summary": r.summary, "details": list(r.details)}
                for r in results
            ],
        },
        indent=2,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only audit of the Google Sheet ledger. Writes nothing, ever.",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output (for before/after diffs)")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures in the exit code")
    parser.add_argument("--expect-rows", type=int, default=None, help="fail unless the sheet has exactly N data rows")
    parser.add_argument("-v", "--verbose", action="store_true", help="show detail lines for passing checks too")
    parser.add_argument("--save-snapshot", metavar="PATH", help="also write the raw grids to a local JSON file")
    parser.add_argument("--from-snapshot", metavar="PATH", help="audit a saved snapshot offline (no credentials, no API calls)")
    args = parser.parse_args()

    if args.from_snapshot:
        with open(args.from_snapshot, encoding="utf-8") as f:
            grids = Grids.from_snapshot(json.load(f))
        grids.meta["source"] = f"snapshot {args.from_snapshot}"
    else:
        worksheet, title = open_worksheet_readonly()
        grids = read_grids(worksheet, title)

    if not grids.formatted:
        print("The worksheet is empty -- nothing to audit. Nothing was created.", file=sys.stderr)
        raise SystemExit(2)

    if args.save_snapshot:
        with open(args.save_snapshot, "w", encoding="utf-8") as f:
            json.dump(grids.to_snapshot(), f, indent=2, default=str)

    sheet = Sheet(grids)
    opts = Options(expect_rows=args.expect_rows, strict=args.strict)
    results = run_checks(sheet, opts)

    if args.json:
        print(render_json(results, grids.meta, args.strict))
    else:
        print(render_text(results, grids.meta, args.verbose))
    raise SystemExit(exit_code(results, args.strict))


if __name__ == "__main__":
    main()
