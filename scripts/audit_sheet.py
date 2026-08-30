"""Read-only audit of the live Google Sheet ledger. WRITES NOTHING, EVER.

Why this exists: every schema change ended with the same manual ritual -- check the row count, look for duplicate upsert
keys, confirm the Total Profit formulas are intact, spot-check that Shipment is still an int and
Card Last 4 still has its leading zeros. Those results were written up as PROSE in the design notes and the
audit itself was thrown away each time. This is that ritual as a runnable artifact.

    python -m scripts.audit_sheet                        # audit the live sheet
    python -m scripts.audit_sheet --expect-rows 23       # ... and assert the row count
    python -m scripts.audit_sheet --save-snapshot before.json   # lands in data/ (gitignored: it holds PII)
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
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

from models.order import FIELDNAMES, STATUSES, TERMINAL_STATUSES, normalize_shipment
from sheets.ledger_sync import (
    HEADER,
    _INT_FIELDS,
    _NUMERIC_FIELDS,
    _STATUS_RANK,
    _coerce,
    _cogs_formula,
    _parse_display_number,
    _profit_formula,
)

# Read-only counterpart to ledger_sync.SCOPES. This is the layer that makes "read-only" a capability
# rather than a promise -- see the module docstring.
READONLY_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# Sheets' date epoch, for reporting what day a stray date serial actually means.
_SHEETS_EPOCH = date(1899, 12, 30)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_real_date(text: str) -> bool:
    """Does an ISO-shaped string name a day that exists? The regex only tests the shape."""
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


# Where a bare `--save-snapshot NAME` lands. A snapshot is the whole sheet -- delivery addresses and
# card last-4s included -- so it must not default to the CWD, where it is one `git add .` away from
# a commit. `data/` is gitignored. An explicit directory in the argument is always honoured.
SNAPSHOT_DIR = Path("data")


def _snapshot_path(arg: str) -> Path:
    """Resolve a snapshot argument: a bare filename goes under SNAPSHOT_DIR, anything with a
    directory component (relative or absolute) is used exactly as given."""
    path = Path(arg)
    if path.parent == Path("."):
        return SNAPSHOT_DIR / path
    return path

# Columns that must hold plain ISO text, not a date serial. Order Date is the dangerous one: it's in
# the primary upsert key AND the name-agnostic fallback key (ledger_sync.py:273, :297).
_DATE_COLUMNS = ("Order Date", "Delivery Date", "Payout Date", "Return Date")

# Sheets' error values, matched as a WHOLE cell. Never as a "starts with #" prefix: costco_mapping
# appends "(Item #1847785)" to item names to disambiguate Costco's truncated descriptions, so a prefix
# rule would flag real data on every Costco row.
_SHEET_ERRORS = {
    "#REF!", "#DIV/0!", "#NAME?", "#VALUE!", "#N/A", "#NUM!", "#NULL!", "#ERROR!",
}

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

    creds = settings.google_credentials(READONLY_SCOPES)
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


def _read_merges(worksheet):
    """Merged ranges on this worksheet, or None if they couldn't be determined.

    Captured here rather than inside a check so the checks keep operating on frozen data only, and so
    a saved snapshot carries the information with it. Best-effort: a metadata failure degrades the
    merge check to SKIP rather than taking the whole audit down over its lowest-priority item.
    """
    try:
        metadata = worksheet.spreadsheet.fetch_sheet_metadata()
        sheet_id = worksheet.id
        for entry in metadata.get("sheets", []):
            if entry.get("properties", {}).get("sheetId") == sheet_id:
                return entry.get("merges", [])
        return []
    except Exception:
        return None


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
            "merges": _read_merges(worksheet),
        },
    )


# --------------------------------------------------------------------------------------------------
# The in-memory sheet the checks operate on
# --------------------------------------------------------------------------------------------------


def _is_effectively_blank(sheet, grid, row_number: int) -> bool:
    """Is this row empty in every way that matters?

    A row whose ONLY content is an unticked `Tracking Submitted` checkbox is EMPTY. That column
    carries checkbox validation, so Sheets materialises a real `False` into every row the validation
    covers -- which is most of the grid, not just the data. Counting those as content would make
    `blank_order_id_rows` report hundreds of "orphans" and `content_outside_the_schema` warn about
    empty space, i.e. exactly the noise that gets an auditor ignored.

    Mirrors sheets.ledger_sync._last_occupied_row, which anchors appends on the same rule.
    """
    for index, value in enumerate(grid[row_number - 1] if row_number - 1 < len(grid) else []):
        if not str(value).strip():
            continue
        name = sheet.header[index] if index < len(sheet.header) else ""
        if name == "Tracking Submitted" and str(value).strip().lower() in ("false", "unchecked"):
            continue
        return False
    return True


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
    # How long an OPEN row may go un-rescraped before it's suspicious. The scheduler runs ~4x/day, so
    # 3 days is many missed runs, not a blip.
    stale_days: int = 3


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
    summary = f"{total} data rows ({ledger} with an Order ID)"

    # --expect-rows is evaluated FIRST and unconditionally. An earlier version returned the
    # height-disagreement WARN before ever comparing, so `--expect-rows N` reported success on the one
    # sheet state that most warrants a hard stop.
    if opts.expect_rows is not None and total != opts.expect_rows:
        return Result("row_count", "FAIL", f"{summary} -- expected {opts.expect_rows}")
    if len(heights) > 1:
        # sync_csv_to_sheet computes its append anchor as len(existing)+1 from the FORMATTED read
        # alone, so a height disagreement is an append-anchor signal, not a curiosity.
        return Result(
            "row_count", "FAIL",
            f"render modes disagree on height: {sorted(heights)} -- appends may land in the wrong row",
        )
    if opts.expect_rows is not None:
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
    # INFO, not WARN: a box holding several SKUs is NORMAL and permanent ("items boxed together share
    # a number"). Warning about it would nag forever on a healthy sheet and make --strict exit 1 for
    # good -- which would foreclose ever using this as a pre-flight gate. A noisy check gets skimmed.
    return Result(
        "duplicate_shipment_lines", "INFO",
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
    shared = [f"rows {v}: order {k[0]} tracking {k[1]}" for k, v in seen.items() if len(v) > 1]
    # One number under TWO Order IDs is a genuine defect, unlike several rows sharing one box.
    crossed = [
        f"tracking {number} appears under {len(orders)} different Order IDs: {sorted(orders)}"
        for number, orders in by_number.items() if len(orders) > 1
    ]
    if not shared and not crossed:
        return Result("duplicate_tracking_keys", "PASS", f"{len(seen)} tracked row(s), all 1:1")
    if crossed:
        return Result(
            "duplicate_tracking_keys", "WARN",
            "one tracking number spans several orders",
            _truncate(crossed + shared, opts.max_detail),
        )
    # INFO for the same reason as duplicate_shipment_lines: a multi-SKU box legitimately puts several
    # rows behind one tracking number. It only means the tracking-based reconciliation declines to
    # fire there (it is guarded to the unambiguous 1:1 case), which is correct behaviour, not a fault.
    return Result(
        "duplicate_tracking_keys", "INFO",
        f"{len(shared)} tracking number(s) cover several rows -- reconciliation won't fire on them",
        _truncate(shared, opts.max_detail),
    )


@check("blank_order_id_rows")
def check_blank_order_id_rows(sheet: Sheet, opts: Options) -> Result:
    """A row with no Order ID can never be updated again -- both ledger_sync.py:317 and :360 skip it.

    It's a permanent orphan: every future re-check appends alongside it instead of updating it.

    It has a SECOND consequence that's easy to miss, so it's reported here too: the newest-first sort
    covers the whole block from row 2 down to the last row that HAS an Order ID, so an orphan sitting
    inside that span gets shuffled around by every sort. It won't necessarily sink to the bottom
    either -- Sheets orders empty cells last, but a row blank only in Order ID still sorts on its
    Order Date and can land back in the middle of the orders. A note row below the last order is left
    alone, which is where one belongs.
    """
    grid = sheet.grids.formatted
    last_ledger_row = max((n for n, _ in sheet.ledger_rows(grid)), default=1)
    offenders, inside = [], 0
    for row_number, row in sheet.rows(grid):
        if _is_effectively_blank(sheet, grid, row_number):
            continue  # a wholly blank row is padding, not an orphan
        if not str(sheet.cell(grid, row_number, "Order ID")).strip():
            item = sheet.cell(grid, row_number, "Item Name")
            where = ""
            if row_number < last_ledger_row:
                inside += 1
                where = " -- inside the sorted block, so the sort will move it"
            offenders.append(f"row {row_number}: {item!r}{where}")
    if not offenders:
        return Result("blank_order_id_rows", "PASS", "every non-empty row carries an Order ID")
    return Result(
        "blank_order_id_rows", "FAIL",
        f"{len(offenders)} orphan row(s) with no Order ID -- they can never be updated"
        + (f" ({inside} of them inside the sorted block, so the sort moves them)" if inside else ""),
        _truncate(offenders, opts.max_detail),
    )


# --------------------------------------------------------------------------------------------------
# Formula integrity
# --------------------------------------------------------------------------------------------------


# The columns sheets.ledger_sync owns as LIVE FORMULAS, and the function that produces each. Driving
# the three checks below off one map is what keeps them honest: adding a formula column without
# teaching the auditor about it would otherwise make no_stray_formulas fail it as a hand edit while
# nothing checked it was correct.
_FORMULA_COLUMNS = {"COGS": _cogs_formula, "Total Profit": _profit_formula}


def _formula_coverage(sheet: Sheet, opts: Options, column: str, name: str) -> Result:
    """"23/23 formulas intact" -- a frozen cell looks normal and just stops updating.

    A FORMATTED read carries the formula's evaluated NUMBER forward, and the RAW row write freezes it
    into place. Only the FORMULA render mode can tell the difference.
    """
    missing, total = [], 0
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        total += 1
        value = sheet.cell(sheet.grids.formula, row_number, column)
        if not (isinstance(value, str) and value.startswith("=")):
            missing.append(f"row {row_number}: holds {value!r} instead of a formula")
    if not missing:
        return Result(name, "PASS", f"{total}/{total} rows carry a formula")
    return Result(name, "FAIL", f"{total - len(missing)}/{total} rows carry a formula",
                  _truncate(missing, opts.max_detail))


def _canonical_formula(text: str) -> str:
    """A formula as Sheets might re-serialise it, reduced to what actually matters: the cells it reads.

    Sheets rewrites a formula in the spreadsheet's LOCALE -- a European locale separates arguments
    with `;` -- and may re-space it. Neither changes which cells it points at, which is the only thing
    the literal check exists to catch (a stale formula after a reorder). Without this, changing the
    spreadsheet locale would fail every row at once and bury a real stale formula in the noise.
    Whitespace and case inside string literals are collapsed too; the builder's own literals are
    lower-case single words, so nothing is lost.
    """
    return re.sub(r"\s+", "", str(text)).replace(";", ",").upper()


def _formula_literal(sheet: Sheet, opts: Options, column: str, builder, name: str) -> Result:
    """Nothing else in the repo can catch a STALE formula after a column reorder.

    These formulas address columns by LETTER, and the design notes records those letters moving twice in one
    day. A stale formula still evaluates and still shows a plausible dollar figure -- it's just
    silently pointed at the wrong cells. Deriving the expectation from the builder rather than
    hardcoding it means this follows any future reorder automatically.
    """
    wrong, total = [], 0
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        value = sheet.cell(sheet.grids.formula, row_number, column)
        if not (isinstance(value, str) and value.startswith("=")):
            continue  # the coverage check owns that failure
        total += 1
        expected = builder(row_number)
        if _canonical_formula(value) != _canonical_formula(expected):
            wrong.append(
                f"row {row_number}:\n"
                f"      is:        {value}\n"
                f"      should be: {expected}"
            )
    if not wrong:
        return Result(name, "PASS", f"{total}/{total} match {builder.__name__}(row)")
    return Result(name, "FAIL",
                  f"{len(wrong)}/{total} formula(s) differ from the current schema -- likely stale "
                  "after a reorder",
                  _truncate(wrong, min(opts.max_detail, 4)))


@check("profit_formula_coverage")
def check_profit_formula_coverage(sheet: Sheet, opts: Options) -> Result:
    return _formula_coverage(sheet, opts, "Total Profit", "profit_formula_coverage")


@check("profit_formula_literal")
def check_profit_formula_literal(sheet: Sheet, opts: Options) -> Result:
    return _formula_literal(sheet, opts, "Total Profit", _profit_formula, "profit_formula_literal")


@check("cogs_formula_coverage")
def check_cogs_formula_coverage(sheet: Sheet, opts: Options) -> Result:
    return _formula_coverage(sheet, opts, "COGS", "cogs_formula_coverage")


@check("cogs_formula_literal")
def check_cogs_formula_literal(sheet: Sheet, opts: Options) -> Result:
    """COGS is the year-end cost figure, so a stale one misreports taxes rather than just a cell."""
    return _formula_literal(sheet, opts, "COGS", _cogs_formula, "cogs_formula_literal")


@check("no_stray_formulas")
def check_no_stray_formulas(sheet: Sheet, opts: Options) -> Result:
    """A hand-written formula anywhere but the derived columns gets flattened to text by the next
    RAW write."""
    owned = {sheet.col(name) for name in _FORMULA_COLUMNS}
    offenders = []
    for row_number, row in sheet.rows(sheet.grids.formula):
        for index, value in enumerate(row):
            if index in owned:
                continue
            if isinstance(value, str) and value.startswith("="):
                name = sheet.header[index] if index < len(sheet.header) else f"col {index + 1}"
                offenders.append(f"row {row_number}, {name}: {value}")
    if not offenders:
        return Result("no_stray_formulas", "PASS",
                      f"no hand-written formulas outside {'/'.join(_FORMULA_COLUMNS)}")
    return Result("no_stray_formulas", "FAIL",
                  f"{len(offenders)} formula(s) outside {'/'.join(_FORMULA_COLUMNS)} -- the next "
                  "sync flattens them to text",
                  _truncate(offenders, opts.max_detail))


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
        elif normalize_shipment(str(stored)) != str(stored).strip():
            # The pre-2026-08-12 "Shipment 2" spelling. normalize_shipment would reduce it, which is
            # exactly how we know the cell was never migrated -- and Shipment is in the upsert key, so
            # an incoming bare "2" won't match it and will append a duplicate.
            counts["text"] += 1
            offenders.append(f"row {row_number}: legacy label {stored!r} -- migrate to a bare number")
        else:
            # A genuinely non-numeric fallback label IS supported (normalize_shipment / _coerce), so it
            # must not also be failed by the display rule below. An earlier version counted it as
            # allowed here and then failed it two lines later, so "label" could never coexist with PASS.
            counts["label"] += 1
            continue
        if shown and not shown.isdigit():
            offenders.append(f"row {row_number}: displays {shown!r} -- the key wants bare digits")
    summary = " | ".join(f"{v} {k}" for k, v in counts.items() if v)
    if not offenders:
        return Result("shipment_is_int", "PASS", summary or "no rows")
    return Result("shipment_is_int", "FAIL", summary, _truncate(offenders, opts.max_detail))


@check("display_round_trips_to_stored")
def check_display_round_trips_to_stored(sheet: Sheet, opts: Options) -> Result:
    """For every numeric cell: does the DISPLAYED text read back as the STORED number?

    The generic form of the §8 invariant, which key_is_format_independent applies only to the four
    key columns. In the other numeric columns the same disagreement corrupts MONEY instead of
    identity: sync_csv_to_sheet reads the sheet FORMATTED (get_all_values), and _merge_row writes a
    preserved cell straight back -- so a 0-dp currency format turns a stored 1300.45 into 1300 on the
    next re-check, a 0-dp percent turns 0.0375 into 0.04, and a format that renders a number as BLANK
    is the worst case: blank-new + blank-old and the hand-typed Payout Amount is ERASED. This check
    is read-only; it names the cells so the FORMAT gets fixed before a run touches them.
    """
    offenders, checked = [], 0
    for field in sorted(_NUMERIC_FIELDS):
        column = _HEADER_FOR_FIELD.get(field)
        if column is None or sheet.col(column) is None:
            continue
        for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
            stored = sheet.cell(sheet.grids.unformatted, row_number, column)
            if isinstance(stored, bool) or not isinstance(stored, (int, float)):
                continue  # blanks and text are other checks' business (numeric_columns_are_numeric)
            checked += 1
            shown = str(sheet.cell(sheet.grids.formatted, row_number, column))
            if shown.strip() == "":
                offenders.append(f"row {row_number}, {column}: stores {stored!r} but DISPLAYS BLANK -- "
                                 "a re-check would erase it")
                continue
            back = _coerce(field, shown)
            if not isinstance(back, (int, float)) or isinstance(back, bool):
                offenders.append(f"row {row_number}, {column}: displays {shown!r}, which does not read "
                                 f"back as a number (stored {stored!r})")
            elif abs(float(back) - float(stored)) > 1e-9:
                offenders.append(f"row {row_number}, {column}: displays {shown!r} -> reads back as "
                                 f"{back!r}, but stores {stored!r} -- the format loses precision")
    if not offenders:
        return Result("display_round_trips_to_stored", "PASS",
                      f"{checked} numeric cell(s) read back exactly from their display text")
    return Result(
        "display_round_trips_to_stored", "FAIL",
        f"{len(offenders)} numeric cell(s) would be corrupted on the next re-check -- fix the column "
        "FORMAT (not the values) before a run touches them",
        _truncate(offenders, opts.max_detail),
    )


@check("shipment_numbers_contiguous")
def check_shipment_numbers_contiguous(sheet: Sheet, opts: Options) -> Result:
    """Every producer numbers an order's boxes 1..N, so a gap means a row went missing or an order was
    renumbered (the Costco unshipped-then-split caveat, the design notes). Neither breaks the upsert key,
    so this is WARN -- something to look at, not a corrupted sheet. Non-numeric labels and blank
    cells are legal (see shipment_is_int) and are left out of the arithmetic.
    """
    by_order: dict[tuple, set[int]] = {}
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        stored = sheet.cell(sheet.grids.unformatted, row_number, "Shipment")
        text = str(stored).strip()
        if not text or isinstance(stored, bool) or not text.isdigit():
            continue
        order_id = str(sheet.cell(sheet.grids.formatted, row_number, "Order ID")).strip()
        order_date = str(sheet.cell(sheet.grids.formatted, row_number, "Order Date")).strip()
        by_order.setdefault((order_id, order_date), set()).add(int(text))

    offenders = []
    for (order_id, _), numbers in by_order.items():
        expected = set(range(1, max(numbers) + 1))
        missing = sorted(expected - numbers)
        if missing:
            offenders.append(
                f"order {order_id}: shipments {sorted(numbers)} -- "
                f"{', '.join(str(n) for n in missing)} missing"
            )
    if not offenders:
        return Result("shipment_numbers_contiguous", "PASS",
                      f"{len(by_order)} order(s) numbered 1..N without gaps")
    return Result(
        "shipment_numbers_contiguous", "WARN",
        f"{len(offenders)} order(s) have a gap in their shipment numbers -- a lost row or a renumbered split",
        _truncate(offenders, opts.max_detail),
    )


@check("profit_value_matches_inputs")
def check_profit_value_matches_inputs(sheet: Sheet, opts: Options) -> Result:
    """Recompute Total Profit = Payout Amount - COGS - Insurance in Python and compare to the cell.

    The literal check compares formula TEXT, and text can be right while the number is wrong (a
    formula that survives a reorder syntactically but reads a neighbouring column) or wrong while the
    number is right (a locale re-serialisation). This is the number itself, from the same unformatted
    cells the formula reads. Rows without a payout, and cancelled rows, are skipped: the formula
    deliberately renders "" there, and profit_blank_despite_payout owns the blank-with-payout case.
    """
    wrong, checked = [], 0
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        status = str(sheet.cell(sheet.grids.formatted, row_number, "Status")).strip().lower()
        payout = _parse_display_number(sheet.cell(sheet.grids.unformatted, row_number, "Payout Amount"))
        if status == "cancelled" or payout is None:
            continue
        shown = _parse_display_number(sheet.cell(sheet.grids.unformatted, row_number, "Total Profit"))
        if shown is None:
            continue  # profit_blank_despite_payout reports that
        cogs = _parse_display_number(sheet.cell(sheet.grids.unformatted, row_number, "COGS")) or 0.0
        insurance = _parse_display_number(sheet.cell(sheet.grids.unformatted, row_number, "Insurance")) or 0.0
        expected = payout - cogs - insurance
        checked += 1
        if abs(float(shown) - expected) > 0.005:
            wrong.append(
                f"row {row_number}: Total Profit is {float(shown):.2f}, but "
                f"{payout:.2f} - {cogs:.2f} - {insurance:.2f} = {expected:.2f}"
            )
    if not wrong:
        return Result("profit_value_matches_inputs", "PASS",
                      f"{checked} paid-out row(s) recomputed from their own cells")
    return Result(
        "profit_value_matches_inputs", "FAIL",
        f"{len(wrong)}/{checked} Total Profit value(s) disagree with Payout - COGS - Insurance -- "
        "the formula reads the wrong cells",
        _truncate(wrong, opts.max_detail),
    )


def _configured_scopes() -> tuple[list[tuple[str, str]], set[str]]:
    """(profile label, retailer NAME) for every configured profile x retailer, plus every retailer
    name a scraper exists for. Isolated so the check can be tested without a config.json."""
    from config.profiles import load_profiles
    from main import SCRAPERS

    names = {cls.retailer_key: cls.retailer_name for cls in SCRAPERS.values()}
    scopes = [(p.label, names[key]) for p in load_profiles() if p.profile_id
              for key in (p.retailers or []) if key in names]
    return scopes, set(names.values())


@check("state_visibility")
def check_state_visibility(sheet: Sheet, opts: Options) -> Result:
    """What each scheduled run would SEE -- and which rows no run can see at all.

    Runs the same pure classifier a scrape starts with (ledger_sync.classify_order_state) once per
    configured profile x retailer, on the FORMATTED grid a real run reads. Per scope it reports
    terminal / open / needs-re-check counts -- the last one is the next run's browser bill. Then
    the finding no other check makes: a row whose (Profile, Retailer) matches no configured scope
    is invisible to every run forever. For a retailer a scraper exists for that is a FAIL (a typo'd
    or retired profile label); for one nothing scrapes (a hand-entered Newegg row) it is INFO.
    """
    from sheets.ledger_sync import classify_order_state

    try:
        scopes, scraped = _configured_scopes()
    except Exception as exc:  # noqa: BLE001 -- no config on this host is a SKIP, not a crash
        return Result("state_visibility", "SKIP", f"could not load the configured profiles ({exc})")

    if not scopes:
        return Result("state_visibility", "SKIP",
                      "no configured profile x retailer on this host -- nothing to compare the rows against")
    grid = sheet.grids.formatted
    lines = []
    for label, name in scopes:
        st = classify_order_state(grid, label, None, name)
        need = sum(1 for o in st["open_orders"] if o.get("needs_agent"))
        lines.append(f"{label}/{name}: {len(st['delivered_ids']) + len(st['cancelled_ids'])} terminal, "
                     f"{len(st['open_orders'])} open ({need} need a re-read)")

    configured = set(scopes)
    invisible, hand_entered = [], []
    for row_number, _ in sheet.ledger_rows(grid):
        profile = str(sheet.cell(grid, row_number, "Profile")).strip()
        retailer = str(sheet.cell(grid, row_number, "Retailer")).strip()
        if (profile, retailer) in configured:
            continue
        status = str(sheet.cell(grid, row_number, "Status")).strip().lower()
        where = f"row {row_number}: Profile {profile!r} / Retailer {retailer!r} [{status}]"
        # A TERMINAL row never needs a run again -- an imported delivered/paid order under a
        # profile that does not scrape that retailer is fine (INFO). An OPEN one is the finding:
        # nothing will ever re-check or close it.
        if retailer in scraped and status not in TERMINAL_STATUSES:
            invisible.append(where)
        else:
            hand_entered.append(where)

    summary = " | ".join(lines) if lines else "no configured profile x retailer"
    if invisible:
        return Result("state_visibility", "FAIL",
                      f"{len(invisible)} row(s) visible to NO configured run -- their orders can never be "
                      "re-checked or closed", _truncate(invisible + [f"scopes: {summary}"], opts.max_detail))
    if hand_entered:
        return Result("state_visibility", "INFO",
                      f"{summary}; {len(hand_entered)} terminal / hand-entered row(s) outside every configured run",
                      _truncate(hand_entered, opts.max_detail))
    return Result("state_visibility", "PASS", summary)


@check("return_columns_consistent")
def check_return_columns_consistent(sheet: Sheet, opts: Options) -> Result:
    """Return Qty feeds the COGS formula, so a malformed value silently corrupts the cost side.

    Return Qty must be a whole number, at most the row's Quantity (both are GROSS -- the bought
    count), and paired with an ISO Return Date; a date without a quantity records nothing and is
    flagged too. Blank both is the normal case for every row that never had a return.
    """
    offenders, checked = [], 0
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        ret = sheet.cell(sheet.grids.unformatted, row_number, "Return Qty")
        ret_date = str(sheet.cell(sheet.grids.formatted, row_number, "Return Date")).strip()
        blank = ret in ("", None)
        if blank and not ret_date:
            # A row the GROUP walked to `return` with no Return Qty typed yet: the payout side is
            # already clawed back while COGS still counts every unit, so the row shows a large loss
            # until the hand edit lands. This is the reminder that edit is owed.
            if str(sheet.cell(sheet.grids.formatted, row_number, "Status")).strip().lower() == "return":
                checked += 1
                offenders.append(f"row {row_number}: Status is `return` but Return Qty is blank -- "
                                 "COGS still counts the returned units; type Return Qty + Return Date")
            continue
        checked += 1
        qty = _parse_display_number(sheet.cell(sheet.grids.unformatted, row_number, "Quantity"))
        if blank and ret_date:
            offenders.append(f"row {row_number}: Return Date {ret_date!r} but no Return Qty -- nothing was netted")
            continue
        number = _parse_display_number(ret)
        if number is None or number != int(number) or number <= 0:
            offenders.append(f"row {row_number}: Return Qty {ret!r} is not a positive whole number")
        elif qty is not None and number > qty:
            offenders.append(f"row {row_number}: Return Qty {ret!r} exceeds Quantity {qty!r}")
        if not (_ISO_DATE.match(ret_date) and _is_real_date(ret_date)):
            offenders.append(f"row {row_number}: Return Qty set but Return Date {ret_date!r} is not an ISO date")
    if not offenders:
        return Result("return_columns_consistent", "PASS", f"{checked} row(s) with a return, all consistent")
    return Result("return_columns_consistent", "FAIL",
                  f"{len(offenders)} return cell(s) malformed -- the COGS formula is netting the wrong amount",
                  _truncate(offenders, opts.max_detail))


@check("order_level_cells_agree")
def check_order_level_cells_agree(sheet: Sheet, opts: Options) -> Result:
    """Retailer, Profile and Order Date are ORDER-level facts: every row of one order must agree.

    Nothing else looks at these values across rows, and a disagreement is how an order goes wrongly
    terminal: load_order_state scopes by Profile + Retailer, so a row whose Profile or Retailer differs
    from its siblings is invisible to that run -- and if the rows it CAN see are all delivered, the
    order is classed terminal with a real open shipment frozen. A differing Order Date
    is a different upsert key, so the row can never be updated by a re-check either.
    """
    fields = ("Retailer", "Profile", "Order Date")
    by_order: dict[str, dict[str, dict[str, list[int]]]] = {}
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        order_id = str(sheet.cell(sheet.grids.formatted, row_number, "Order ID")).strip()
        seen = by_order.setdefault(order_id, {f: {} for f in fields})
        status = str(sheet.cell(sheet.grids.formatted, row_number, "Status")).strip().lower()
        for f in fields:
            if f == "Order Date" and status == "return":
                # A return is its own event on the same order; the ledger has no return-date column,
                # so a return row carries the date it happened in Order Date (the hand-kept sheets
                # always did). Retailer and Profile must still agree.
                continue
            value = str(sheet.cell(sheet.grids.formatted, row_number, f)).strip()
            seen[f].setdefault(value, []).append(row_number)

    offenders = []
    for order_id, seen in by_order.items():
        for f in fields:
            if len(seen[f]) > 1:
                variants = ", ".join(f"{v!r} rows {rows}" for v, rows in seen[f].items())
                offenders.append(f"order {order_id}: {f} differs -- {variants}")
    if not offenders:
        return Result("order_level_cells_agree", "PASS",
                      f"{len(by_order)} order(s): one Retailer / Profile / Order Date each")
    return Result(
        "order_level_cells_agree", "FAIL",
        f"{len(offenders)} order-level disagreement(s) -- the odd row is invisible to its own run",
        _truncate(offenders, opts.max_detail),
    )


@check("quantity_is_int")
def check_quantity_is_int(sheet: Sheet, opts: Options) -> Result:
    """Quantity has to be checked HERE, because numeric_columns_are_numeric deliberately skips it.

    That exclusion (it drops `_INT_FIELDS`) once left Quantity with no type check at all. A text
    Quantity — a leading-apostrophe `'2`, or a hand edit — stops the column summing AND makes
    `total_cost_matches_quantity` silently skip the row while still reporting PASS.

    `*` is legal: it's the marker the undisclosed-split safety net writes (see
    `unresolved_split_quantity`, which is what chases it up).
    """
    counts = {"int": 0, "split_marker": 0, "float": 0, "text": 0, "blank": 0}
    offenders = []
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        stored = sheet.cell(sheet.grids.unformatted, row_number, "Quantity")
        if stored == "" or stored is None:
            counts["blank"] += 1
        elif isinstance(stored, bool):
            counts["text"] += 1
            offenders.append(f"row {row_number}: boolean {stored!r}")
        elif isinstance(stored, int):
            counts["int"] += 1
        elif isinstance(stored, float):
            counts["float"] += 1
            offenders.append(f"row {row_number}: stored as float {stored!r}, should be int")
        elif str(stored).strip() == "*":
            counts["split_marker"] += 1
        else:
            counts["text"] += 1
            offenders.append(f"row {row_number}: text {stored!r} -- the column won't sum and cost checks skip this row")
    summary = " | ".join(f"{v} {k}" for k, v in counts.items() if v)
    if not offenders:
        return Result("quantity_is_int", "PASS", summary or "no rows")
    return Result("quantity_is_int", "FAIL", summary, _truncate(offenders, opts.max_detail))


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
    # NB: _INT_FIELDS (quantity, shipment) are excluded here because they have their own dedicated
    # int-ness checks -- `quantity_is_int` and `shipment_is_int`. An earlier version of this check
    # carried a Quantity "*" carve-out that was DEAD CODE for exactly that reason, and the test
    # guarding it passed vacuously against a column this check never inspects.
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
                if _is_real_date(stored.strip()):
                    continue
                # Shape-correct but impossible: "2026-13-45" passes the regex, and every consumer
                # that parses the column properly (a Tax Summary SUMIFS, `since` trimming) would
                # choke on it or drop the row. The regex is the fast path, not the definition.
                message = f"row {row_number}, {name}: {stored!r} is not a real calendar date"
            elif isinstance(stored, (int, float)) and not isinstance(stored, bool):
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


@check("rows_are_date_descending")
def check_rows_are_date_descending(sheet: Sheet, opts: Options) -> Result:
    """The ledger is kept newest-first (Order Date DESC, then Order ID, then Shipment ASC).

    WARN, not FAIL: being out of order is a readability problem, never a data-integrity one -- the
    upsert matches on key, not position. Drift is expected and self-healing, because main.run_scrape
    only re-sorts when a sync APPENDED rows (an update rewrites a row in place and can't reorder
    anything). So a sheet that has only taken updates since its last append is legitimately stale here.

    Worth checking anyway because the two ways it goes wrong are silent: scripts/retag_buying_groups.py
    deletes rows without re-sorting, and a hand-edited Order Date moves a row's rightful position
    without moving the row.
    """
    keys = []
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        shipment = sheet.cell(sheet.grids.unformatted, row_number, "Shipment")
        ship_key = ((0, shipment) if isinstance(shipment, (int, float))
                    and not isinstance(shipment, bool) else (1, str(shipment)))
        keys.append((
            row_number,
            str(sheet.cell(sheet.grids.formatted, row_number, "Order Date")),
            str(sheet.cell(sheet.grids.formatted, row_number, "Order ID")),
            ship_key,
        ))
    if len(keys) < 2:
        return Result("rows_are_date_descending", "PASS", f"{len(keys)} row(s), nothing to order")

    offenders = []
    for (row_a, date_a, oid_a, ship_a), (row_b, date_b, oid_b, ship_b) in zip(keys, keys[1:]):
        if date_a < date_b:
            offenders.append(f"row {row_b} ({date_b}) is NEWER than row {row_a} ({date_a}) above it")
        elif date_a == date_b and (oid_a, ship_a) > (oid_b, ship_b):
            offenders.append(
                f"row {row_b} ({oid_b} ship {ship_b[1]}) sorts before row {row_a} "
                f"({oid_a} ship {ship_a[1]}) on the same date {date_a}"
            )
    if offenders:
        return Result(
            "rows_are_date_descending", "WARN",
            f"{len(offenders)} row(s) out of newest-first order -- "
            "run `python -m scripts.sort_ledger --apply`",
            _truncate(offenders, opts.max_detail),
        )
    return Result("rows_are_date_descending", "PASS", f"{len(keys)} row(s) in newest-first order")


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
    an invisible, guaranteed duplicate.

    Profile and Retailer are included even though they aren't in the key, because `load_order_state`
    FILTERS on them -- and it compares Profile UNSTRIPPED (`row[idx["Profile"]] != profile_label`)
    while stripping Retailer three lines below. So one invisible trailing space in a Profile cell
    hides that row from its own retailer's run; if the rows that remain visible are all delivered, the
    order is classified TERMINAL and silently stops being tracked, with its real open shipment frozen.
    """
    offenders = []
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        for name in ("Order ID", "Order Date", "Item Name", "Shipment", "Profile", "Retailer"):
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
        for name in ("Order Link", "Tracking Link", "Receipt Link"):
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
    # Report what was SKIPPED, not just what passed: this check silently declines to run on any row
    # whose factors aren't numeric, and a bare "PASS" over a shrinking sample hides that.
    skipped = sum(1 for _ in sheet.ledger_rows(sheet.grids.formatted)) - checked
    tail = f" ({skipped} skipped -- non-numeric factors)" if skipped else ""
    if not offenders:
        return Result("total_cost_matches_quantity", "PASS", f"{checked} row(s) reconcile{tail}")
    return Result("total_cost_matches_quantity", "WARN", f"{len(offenders)} row(s) don't reconcile{tail}", _truncate(offenders, opts.max_detail))


@check("shipping_is_cost_weighted")
def check_shipping_is_cost_weighted(sheet: Sheet, opts: Options) -> Result:
    """_reprorate_order_level (sheets/ledger_sync.py) rewrites each row's Shipping to its own
    cost-weighted SHARE of its order's raw shipping total -- so Shipping / Total Cost should be the
    SAME ratio across every row of one order. A row that disagrees was prorated against a different
    raw total than its siblings (stale from before a later box was discovered, or a manual edit), and
    Total Profit -- which reads Shipping directly, with no re-derivation of its own -- is quietly
    wrong for it with nothing else to surface that."""
    by_order: dict[str, list[tuple[int, float, float]]] = {}
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        order_id = str(sheet.cell(sheet.grids.formatted, row_number, "Order ID")).strip()
        shipping = _parse_display_number(sheet.cell(sheet.grids.unformatted, row_number, "Shipping"))
        cost = _parse_display_number(sheet.cell(sheet.grids.unformatted, row_number, "Total Cost"))
        if shipping is None or not cost:
            continue  # can't derive a ratio without a non-zero cost to divide by
        by_order.setdefault(order_id, []).append((row_number, shipping, cost))

    offenders = []
    for order_id, rows in by_order.items():
        if len(rows) < 2:
            continue
        ratios = {round(shipping / cost, 4) for _, shipping, cost in rows}
        if len(ratios) > 1:
            detail = ", ".join(f"row {n}: {shipping}/{cost}={shipping / cost:.4f}" for n, shipping, cost in rows)
            offenders.append(f"order {order_id}: rows disagree -- {detail}")
    if not offenders:
        return Result("shipping_is_cost_weighted", "PASS", f"{len(by_order)} order(s) checked")
    return Result(
        "shipping_is_cost_weighted", "WARN",
        f"{len(offenders)} order(s) carry an inconsistent shipping split -- Total Profit will be wrong there",
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
        return Result("buying_group_coverage", "SKIP", "config.json has no `warehouses` -- nothing to classify against")
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
        details.append(f"{unclassified} row(s) Unclassified -- a real warehouse may be missing from config.json `warehouses`")
    return Result("buying_group_coverage", status, counts or "no rows", _truncate(details, opts.max_detail))


@check("card_and_rate_coverage")
def check_card_and_rate_coverage(sheet: Sheet, opts: Options) -> Result:
    from config.cards import load_cards
    from config.settings import settings
    from scripts.backfill_profit_columns import plan_profit_backfill

    cards = load_cards()
    if not cards:
        return Result("card_and_rate_coverage", "SKIP", "config.json has no `cards` -- nothing to resolve against")
    plan = plan_profit_backfill(
        sheet.header, sheet.grids.unformatted[1:], cards, settings.default_cashback_rate, refresh=True
    )
    details, status = [], "PASS"
    for label, key in (("never filled", "fills"), ("disagree with config.json `cards`", "changes")):
        entries = plan.get(key) or []
        if entries:
            status = "WARN"
            details.append(f"{len(entries)} cell(s) {label} -- run `python -m scripts.backfill_profit_columns`")
    unresolved = plan.get("unresolved") or []
    if unresolved:
        status = "WARN"
        details.append(f"{len(unresolved)} card last-4(s) not in config.json `cards`")
    return Result("card_and_rate_coverage", status, "Card + Cashback Rate resolve cleanly" if status == "PASS" else "gaps found", _truncate(details, opts.max_detail))


@check("legacy_blank_shipment")
def check_legacy_blank_shipment(sheet: Sheet, opts: Options) -> Result:
    """A row with an Order ID but no Shipment number — written before that column existed.

    the design notes has carried "check the live sheet once for this" as an open item. It matters because
    such a row ORPHANS if its order later splits: the scraper emits Shipment 1..N, none of which match
    the blank, so the blank row goes stale and stays perpetually open while a duplicate is appended
    alongside it.
    """
    offenders = [
        f"row {n}: order {sheet.cell(sheet.grids.formatted, n, 'Order ID')}"
        for n, _ in sheet.ledger_rows(sheet.grids.formatted)
        if not str(sheet.cell(sheet.grids.formatted, n, "Shipment")).strip()
    ]
    if not offenders:
        return Result("legacy_blank_shipment", "PASS", "no pre-Shipment-column legacy rows")
    return Result(
        "legacy_blank_shipment", "WARN",
        f"{len(offenders)} row(s) have no Shipment number and will orphan if that order splits",
        _truncate(offenders, opts.max_detail),
    )


@check("content_outside_the_schema", requires_schema=False)
def check_content_outside_the_schema(sheet: Sheet, opts: Options) -> Result:
    """Anything living outside the 25-column x N-row data block.

    Two real hazards, not tidiness. (1) `append_rows` once auto-detected the "table" on the real sheet
    and anchored appends TEN COLUMNS RIGHT, landing rows in K:AB — content past column Y is that
    signature. (2) The append anchor is `len(existing) + 1`, so a stray note UNDER the data makes the
    next appended row land past it, leaving a gap and (worse) writing where nothing expects it.
    """
    grid = sheet.grids.formatted
    width = len(HEADER)
    wide, below, holes = [], [], []

    last_ledger_row = 0
    for row_number, _ in sheet.rows(grid):
        order_id = sheet.cell(grid, row_number, "Order ID") if sheet.schema_ok else ""
        if str(order_id).strip():
            last_ledger_row = row_number

    for row_number, row in sheet.rows(grid):
        if len(row) > width and any(str(c).strip() for c in row[width:]):
            extra = [c for c in row[width:] if str(c).strip()]
            wide.append(f"row {row_number}: {len(row) - width} cell(s) past column {_col_letter(width - 1)}: {extra[:3]}")
        populated = not _is_effectively_blank(sheet, grid, row_number)
        if last_ledger_row and row_number > last_ledger_row and populated:
            below.append(f"row {row_number}: content below the last ledger row ({last_ledger_row})")
        if last_ledger_row and row_number < last_ledger_row and not populated:
            holes.append(f"row {row_number}: blank row inside the data block")

    if wide:
        return Result(
            "content_outside_the_schema", "FAIL",
            f"{len(wide)} row(s) have content past column {_col_letter(width - 1)} -- appends may be mis-anchored",
            _truncate(wide + below + holes, opts.max_detail),
        )
    if below or holes:
        return Result(
            "content_outside_the_schema", "WARN",
            f"{len(below)} row(s) below the data block, {len(holes)} blank row(s) inside it",
            _truncate(below + holes, opts.max_detail),
        )
    return Result("content_outside_the_schema", "PASS", f"nothing outside the {width}-column data block")


@check("no_formula_errors", requires_schema=False)
def check_no_formula_errors(sheet: Sheet, opts: Options) -> Result:
    """Spreadsheet error values anywhere on the sheet.

    A `#REF!` is exactly what deleting a column leaves behind, and `profit_formula_coverage` would
    still see a formula there and pass. Matched as the WHOLE cell value, never as a prefix: Costco
    item names legitimately contain "#" (the mapping appends "(Item #1847785)" to disambiguate
    truncated descriptions), so a "starts with #" rule would flag real data on every Costco row.
    """
    offenders = []
    for row_number, row in sheet.rows(sheet.grids.unformatted):
        for index, value in enumerate(row):
            if isinstance(value, str) and value.strip() in _SHEET_ERRORS:
                name = sheet.header[index] if index < len(sheet.header) else f"col {index + 1}"
                offenders.append(f"row {row_number}, {name}: {value.strip()}")
    if not offenders:
        return Result("no_formula_errors", "PASS", "no #REF!/#VALUE!/#N/A cells")
    return Result(
        "no_formula_errors", "FAIL",
        f"{len(offenders)} cell(s) hold a spreadsheet error value",
        _truncate(offenders, opts.max_detail),
    )


@check("payout_is_cost_weighted")
def check_payout_is_cost_weighted(sheet: Sheet, opts: Options) -> Result:
    """A payout arrives per PACKAGE, but the ledger is one row per (shipment x item).

    So a box holding two items has two rows behind one tracking number, and `sync_tracking` splits the
    payout between them by each row's share of the package's Total Cost — writing the full amount to
    each would book the group's money twice. This asserts the split actually happened: rows sharing
    `(Order ID, Tracking Number)` must show the same Payout/Cost ratio.

    Nothing else can catch a double-booked payout. `Total Profit` reads Payout Amount straight from
    the cell and re-derives nothing, so a doubled payout just reads as a larger, plausible profit.
    """
    packages: dict[tuple, list[tuple[int, float, float]]] = {}
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        key = sheet.tracking_key(sheet.grids.formatted, row_number)
        if key is None:
            continue
        payout = _parse_display_number(sheet.cell(sheet.grids.unformatted, row_number, "Payout Amount"))
        cost = _parse_display_number(sheet.cell(sheet.grids.unformatted, row_number, "Total Cost"))
        if payout is None or not cost:
            continue
        packages.setdefault(key, []).append((row_number, payout, cost))

    offenders, checked = [], 0
    for key, rows in packages.items():
        if len(rows) < 2:
            continue
        checked += 1
        ratios = {round(payout / cost, 4) for _, payout, cost in rows}
        if len(ratios) > 1:
            detail = ", ".join(f"row {n}: {p}/{c}={p / c:.4f}" for n, p, c in rows)
            offenders.append(f"order {key[0]} package {key[1]}: {detail}")
    if not offenders:
        return Result("payout_is_cost_weighted", "PASS", f"{checked} multi-row package(s) split pro-rata")
    # WARN, not FAIL: the sync always splits by cost, but a hand-entered or imported package can carry
    # the group's REAL per-item payouts (MOD paid $74 on a $59.98 Echo Dot beside 1.005x on the
    # watches in the same box, 2026-08-30), which this cannot tell from a double-booking. The
    # tell-tale of a double-booking is every row carrying the SAME full amount -- read the ratios.
    return Result(
        "payout_is_cost_weighted", "WARN",
        f"{len(offenders)} package(s) don't split their payout by cost -- either real per-item payouts "
        "(hand-entered / imported) or the money booked twice; compare the rows",
        _truncate(offenders, opts.max_detail),
    )


@check("paid_rows_have_a_payout")
def check_paid_rows_have_a_payout(sheet: Sheet, opts: Options) -> Result:
    """A row the buying group reports as `paid` must carry the amount it was paid.

    `paid` is terminal, so the row is never revisited — and `Total Profit` reads BLANK until Payout
    Amount is filled. A paid row with no amount is therefore permanently missing from the P&L, which
    is the one number this ledger exists to produce, with nothing to announce it.
    """
    from config.warehouses import is_deliberately_unrouted

    zeros, blanks = [], []
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        status = str(sheet.cell(sheet.grids.formatted, row_number, "Status")).strip().lower()
        if status != "paid":
            continue
        payout = _parse_display_number(sheet.cell(sheet.grids.unformatted, row_number, "Payout Amount"))
        order_id = sheet.cell(sheet.grids.formatted, row_number, "Order ID")
        if payout is None:
            blanks.append(f"row {row_number}: order {order_id} is paid but has no Payout Amount")
        # A gift-card row is `paid` with a REAL $0 payout by rule: no buying
        # group is ever involved, the income arrives through the order the card funds.
        if is_deliberately_unrouted(sheet.cell(sheet.grids.formatted, row_number, "Buying Group")):
            continue
        elif payout == 0:
            # A ZERO is the worse case, and an earlier version of this check missed it by testing only
            # for blank. Blank makes Total Profit render blank; a literal 0 makes it compute
            # `0 - Total Cost - Insurance`, i.e. a large fictitious LOSS. Live BFMR reported
            # three packages as paid while amount_paid was still "0.00", and one booked -$1,678.46.
            cost = sheet.cell(sheet.grids.formatted, row_number, "Total Cost")
            zeros.append(f"row {row_number}: order {order_id} is paid for $0 against a cost of {cost} "
                         "-- Total Profit is showing a fictitious loss")
    if zeros:
        return Result(
            "paid_rows_have_a_payout", "FAIL",
            f"{len(zeros)} paid row(s) have a ZERO payout", _truncate(zeros + blanks, opts.max_detail),
        )
    if blanks:
        # WARN, not FAIL: a group can legitimately mark a package paid minutes before it settles, and
        # the next sync fills it. It only becomes wrong if it never does -- which staleness catches.
        return Result(
            "paid_rows_have_a_payout", "WARN",
            f"{len(blanks)} paid row(s) have no payout yet", _truncate(blanks, opts.max_detail),
        )
    return Result("paid_rows_have_a_payout", "PASS", "every paid row carries its payout")


@check("profit_blank_despite_payout")
def check_profit_blank_despite_payout(sheet: Sheet, opts: Options) -> Result:
    """A paid-out row whose Total Profit renders BLANK — the real signature of a broken formula.

    Scanning for `#REF!` mostly WON'T catch a broken profit formula, because `_profit_formula` wraps
    its body in `IFERROR(..., "")`. An error raised inside is swallowed and the cell renders blank —
    indistinguishable, to the eye, from the deliberate blank of a not-yet-paid-out row. So the whole
    profit column can silently go blank and an error scan sees nothing.

    The distinguishing signal is the pairing: `_profit_formula` returns "" only when Payout Amount is
    empty. Blank profit + non-blank payout therefore means the formula failed, and money that should
    be reported isn't.
    """
    offenders = []
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        payout = sheet.cell(sheet.grids.unformatted, row_number, "Payout Amount")
        profit = sheet.cell(sheet.grids.unformatted, row_number, "Total Profit")
        if payout == "" or payout is None:
            continue
        if profit == "" or profit is None:
            order_id = sheet.cell(sheet.grids.formatted, row_number, "Order ID")
            offenders.append(f"row {row_number}: order {order_id} has a payout of {payout!r} but no profit")
    if not offenders:
        return Result("profit_blank_despite_payout", "PASS", "every paid-out row reports a profit")
    return Result(
        "profit_blank_despite_payout", "FAIL",
        f"{len(offenders)} paid-out row(s) render a blank profit -- the formula is failing silently",
        _truncate(offenders, opts.max_detail),
    )


@check("status_is_present")
def check_status_is_present(sheet: Sheet, opts: Options) -> Result:
    """A BLANK Status keeps an order open forever, and costs money every run.

    `column_shape` only validates a status it can see (`if status and status not in STATUSES`), so
    blank slips through. `load_order_state` reads it as `... or "ordered"`, so the order stays in
    open_orders permanently; and if that shipment also lacks a tracking number, `needs_agent` stays
    True, so every scheduled run pays for an agent pass on an order that will never close. the design notes
    records a single such order costing $0.237 in one run.
    """
    offenders = [
        f"row {n}: order {sheet.cell(sheet.grids.formatted, n, 'Order ID')} has no status"
        for n, _ in sheet.ledger_rows(sheet.grids.formatted)
        if not str(sheet.cell(sheet.grids.formatted, n, "Status")).strip()
    ]
    if not offenders:
        return Result("status_is_present", "PASS", "every row has a status")
    return Result(
        "status_is_present", "FAIL",
        f"{len(offenders)} row(s) have a blank Status -- those orders stay open (and billable) forever",
        _truncate(offenders, opts.max_detail),
    )


@check("unresolved_split_quantity")
def check_unresolved_split_quantity(sheet: Sheet, opts: Options) -> Result:
    """The undisclosed-split safety net writes Quantity `*` + a blank Total Cost, awaiting the user.

    Left unresolved it is a money bomb, not just an untidy row: with Total Cost blank and a payout
    filled, the profit formula evaluates `payout + (0+0)*rate - 0 - 0 - ins`, so **the entire payout
    is booked as profit**. The blank Total Cost also drops out of _reprorate_order_level's cost-weighted
    split (sheets/ledger_sync.py), so that box absorbs none of the order's shipping and its sibling
    row absorbs all of it.

    Nothing else surfaces this after the one alert fired at creation time.
    """
    pending, billed = [], []
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        quantity = str(sheet.cell(sheet.grids.formatted, row_number, "Quantity")).strip()
        if quantity != "*":
            continue
        order_id = sheet.cell(sheet.grids.formatted, row_number, "Order ID")
        payout = sheet.cell(sheet.grids.unformatted, row_number, "Payout Amount")
        if payout not in ("", None):
            billed.append(f"row {row_number}: order {order_id} is paid out ({payout!r}) with no cost -- profit is overstated by the full payout")
        else:
            pending.append(f"row {row_number}: order {order_id} awaits per-box quantities")
    if billed:
        return Result("unresolved_split_quantity", "FAIL", f"{len(billed)} unresolved split row(s) already paid out", _truncate(billed + pending, opts.max_detail))
    if pending:
        return Result("unresolved_split_quantity", "WARN", f"{len(pending)} split row(s) still need per-box quantities", _truncate(pending, opts.max_detail))
    return Result("unresolved_split_quantity", "PASS", "no unresolved split rows")


@check("cogs_inputs_complete")
def check_cogs_inputs_complete(sheet: Sheet, opts: Options) -> Result:
    """COGS is the year-end cost figure, and every way it goes wrong is SILENT.

    `cashback_rate_sane` only asks whether a rate is plausible; it cannot see a MISSING one. But COGS
    is `(Total Cost - Return Qty x Cost Per Item - Gift Card + Shipping + Sales Tax) * (1 - Cashback
    Rate)`, so a blank rate quietly computes the FULL cost as cost of goods -- overstating COGS,
    understating income, and under-reporting tax. Nothing else in the audit looks at it, and the
    number stays perfectly plausible while being wrong.

    Three shapes, in decreasing severity:

    - **COGS with no Cashback Rate** -- FAIL. The cost side is overstated by the rebate.
    - **A payout with no COGS** -- FAIL. Income recorded with no cost against it, so profit is
      overstated. Usually a Total Cost that never landed.
    - **COGS with no payout** -- reported, not failed. It is the NORMAL state of an order that has
      shipped but not been paid yet, and at a year boundary it is exactly the straddle that makes the
      cost and income sides fall in different tax years. Worth seeing, never worth failing.

    Gift-card rows are exempt from the third shape entirely: a gift card is a real cost that will
    NEVER have a payout of its own, because the income arrives through the order it funded (whose own
    cost was netted down by the card, so nothing is double-counted).
    """
    from config.warehouses import is_deliberately_unrouted

    grid, unf = sheet.grids.formatted, sheet.grids.unformatted
    no_rate, no_cogs, unpaid, gift = [], [], [], 0
    for row_number, _ in sheet.ledger_rows(grid):
        status = str(sheet.cell(grid, row_number, "Status")).strip().lower()
        if status == "cancelled":
            continue  # carries no money by design -- see ledger_sync._blank_money_for_cancelled
        cogs = _parse_display_number(sheet.cell(unf, row_number, "COGS"))
        rate = _parse_display_number(sheet.cell(unf, row_number, "Cashback Rate"))
        payout = _parse_display_number(sheet.cell(unf, row_number, "Payout Amount"))
        order_id = sheet.cell(grid, row_number, "Order ID")

        if cogs and rate is None:
            card = sheet.cell(grid, row_number, "Card")
            no_rate.append(f"row {row_number}: order {order_id} (card {card!r}) -- COGS counts the "
                           "full cost because no rate resolved")
        # `cogs is None`, not `not cogs`: a referral bonus or credit has a real $0 cost, so its
        # COGS is a genuine 0, and 0 income-with-cost-0 is exactly right at year end.
        if payout and cogs is None:
            no_cogs.append(f"row {row_number}: order {order_id} paid {payout} with no COGS")
        if cogs and not payout:
            if is_deliberately_unrouted(sheet.cell(grid, row_number, "Buying Group")):
                gift += 1
            else:
                unpaid.append((row_number, cogs, status))

    note = []
    if unpaid:
        total = sum(c for _n, c, _s in unpaid)
        note.append(f"{len(unpaid)} row(s) hold {total:,.2f} of COGS with no payout yet -- at a year "
                    "boundary these land in a different tax year from their income")
    if gift:
        note.append(f"{gift} gift-card row(s) carry cost with no payout, as designed")

    if no_rate or no_cogs:
        return Result(
            "cogs_inputs_complete", "FAIL",
            f"{len(no_rate)} row(s) with COGS but no Cashback Rate, {len(no_cogs)} with a payout but "
            "no COGS -- the year-end totals are wrong",
            _truncate(no_rate + no_cogs + note, opts.max_detail),
        )
    return Result("cogs_inputs_complete", "PASS",
                  "every row's COGS has its cost and rate" + (f"; {note[0]}" if note else ""),
                  tuple(note[1:]) if len(note) > 1 else ())


@check("cashback_rate_sane")
def check_cashback_rate_sane(sheet: Sheet, opts: Options) -> Result:
    """A rate must be a fraction in [0, 1].

    models/card.py enforces this on the CONFIG side, but nothing enforces it on the sheet, and the
    profit formula multiplies by it directly — a 4 meaning "4%" overstates that row by 100x while
    still looking like a plausible number. There is deliberately NO "suspiciously high" warning band:
    a real 13% Costco rate is configured and confirmed, so such a band would be permanent noise.
    """
    offenders, rates = [], set()
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        value = sheet.cell(sheet.grids.unformatted, row_number, "Cashback Rate")
        if value == "" or value is None or isinstance(value, bool):
            continue
        if not isinstance(value, (int, float)):
            continue  # a text rate is numeric_columns_are_numeric's failure to report
        rates.add(value)
        if not 0 <= value <= 1:
            offenders.append(f"row {row_number}: rate {value!r} is outside [0,1] -- profit is off by ~100x")
    if not offenders:
        return Result("cashback_rate_sane", "PASS", f"rates in use: {sorted(rates) or 'none'}")
    return Result("cashback_rate_sane", "FAIL", f"{len(offenders)} implausible rate(s)", _truncate(offenders, opts.max_detail))


@check("open_row_staleness")
def check_open_row_staleness(sheet: Sheet, opts: Options) -> Result:
    """Non-terminal rows that stopped being re-scraped.

    This is the failure nobody notices: an order stuck open forever (a status the vocabulary doesn't
    recognise, a retailer that silently stopped discovering it) or the scheduler simply not running.
    Every other check looks at whether the data is well-formed; this one asks whether it's still alive.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    stale, open_count, unparsed = [], 0, 0
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        status = str(sheet.cell(sheet.grids.formatted, row_number, "Status")).strip().lower()
        if status in TERMINAL_STATUSES:
            continue
        open_count += 1
        raw = str(sheet.cell(sheet.grids.formatted, row_number, "Last Scraped At")).strip()
        if not raw:
            continue  # never scraped is a different (and visible) condition
        try:
            seen = datetime.fromisoformat(raw)
        except ValueError:
            unparsed += 1
            continue
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        age = (now - seen).days
        if age >= opts.stale_days:
            order_id = sheet.cell(sheet.grids.formatted, row_number, "Order ID")
            stale.append(f"row {row_number}: order {order_id} ({status}) last scraped {age} days ago")
    details = list(stale)
    if unparsed:
        details.append(f"{unparsed} row(s) have an unparseable Last Scraped At")
    if not stale and not unparsed:
        return Result("open_row_staleness", "PASS", f"{open_count} open row(s), all scraped within {opts.stale_days}d")
    return Result(
        "open_row_staleness", "WARN",
        f"{len(stale)}/{open_count} open row(s) not scraped in {opts.stale_days}+ days",
        _truncate(details, opts.max_detail),
    )


@check("no_merged_cells", requires_schema=False)
def check_no_merged_cells(sheet: Sheet, opts: Options) -> Result:
    """A merged cell reads as its top-left value and BLANKS its neighbours.

    That's uniquely nasty here: _merge_row's blank-never-overwrites rule would then preserve the old
    values as though the data were legitimately absent, so a merge quietly freezes those cells forever
    instead of erroring. Read from the sheet metadata captured at read time (see read_grids), so an
    older snapshot without it degrades to SKIP rather than a false PASS.
    """
    merges = sheet.grids.meta.get("merges")
    if merges is None:
        return Result("no_merged_cells", "SKIP", "snapshot predates merge capture")
    if not merges:
        return Result("no_merged_cells", "PASS", "no merged cells")
    details = [
        f"rows {m.get('startRowIndex', '?')}-{m.get('endRowIndex', '?')}, "
        f"cols {m.get('startColumnIndex', '?')}-{m.get('endColumnIndex', '?')}"
        for m in merges
    ]
    return Result(
        "no_merged_cells", "FAIL",
        f"{len(merges)} merged range(s) -- merged cells blank their neighbours on read",
        _truncate(details, opts.max_detail),
    )


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


# Changes on every touch, so including it would bury every real change under 25 lines of noise.
_DIFF_IGNORED_COLUMNS = ("Last Scraped At",)


def diff_snapshots(before: Grids, after: Grids, ignore=_DIFF_IGNORED_COLUMNS) -> dict:
    """What changed between two reads of the sheet, keyed by the primary upsert key.

    This is REPORTED, never asserted: a diff has no correct answer (a new order legitimately appends),
    and `duplicate_primary_keys` already owns the actual failure condition. Its job is to answer the
    question the audit alone can't -- "did that run UPDATE rows or DUPLICATE them?" -- which is the
    whole reason the audit is run before and after a live run.
    """
    old, new = Sheet(before), Sheet(after)

    def index(sheet: Sheet) -> dict:
        return {
            sheet.primary_key(sheet.grids.formatted, n): n
            for n, _ in sheet.ledger_rows(sheet.grids.formatted)
        }

    old_rows, new_rows = index(old), index(new)

    def describe(sheet: Sheet, row_number: int) -> str:
        get = lambda c: sheet.cell(sheet.grids.formatted, row_number, c)  # noqa: E731
        return (
            f"{get('Retailer')} {get('Order ID')} ship {get('Shipment')} "
            f"[{get('Status')}] {str(get('Item Name'))[:40]!r}"
        )

    added = [describe(new, n) for k, n in new_rows.items() if k not in old_rows]
    removed = [describe(old, n) for k, n in old_rows.items() if k not in new_rows]

    def record(sheet: Sheet, row_number: int, key) -> dict:
        get = lambda c: str(sheet.cell(sheet.grids.formatted, row_number, c)).strip()  # noqa: E731
        return {"key": key, "row": row_number, "order_id": key[0], "shipment": str(key[3]),
                "tracking": get("Tracking Number"), "status": get("Status").lower(),
                "item": get("Item Name"), "retailer": get("Retailer")}

    records = {
        "added": [record(new, n, k) for k, n in new_rows.items() if k not in old_rows],
        "removed": [record(old, n, k) for k, n in old_rows.items() if k not in new_rows],
        "changed": [],   # (record-after, column, was, now, status-before)
        "kept": [record(new, n, k) for k, n in new_rows.items() if k in old_rows],
    }

    changed, regressed = [], []
    columns = [c for c in new.header if c not in ignore]
    for key, new_row_number in new_rows.items():
        old_row_number = old_rows.get(key)
        if old_row_number is None:
            continue
        status_was = str(old.cell(old.grids.formatted, old_row_number, "Status")).strip().lower()
        for column in columns:
            was = old.cell(old.grids.formatted, old_row_number, column)
            now = new.cell(new.grids.formatted, new_row_number, column)
            if str(was) != str(now):
                changed.append(f"{key[0]} ship {key[3]} | {column}: {was!r} -> {now!r}")
                records["changed"].append((record(new, new_row_number, key), column, str(was), str(now), status_was))
        # STATUS MUST ONLY MOVE FORWARD. sync_tracking drops any write that would walk a row
        # backwards, and calls that guard load-bearing for MOD returns specifically: MOD publishes no
        # return signal, so a return is typed onto the sheet BY HAND while MOD keeps reporting that
        # package as received (= paid) forever. A regression here means the guard let one through and
        # a human correction was silently undone -- which no single-snapshot check can ever see.
        before = str(old.cell(old.grids.formatted, old_row_number, "Status")).strip().lower()
        after = str(new.cell(new.grids.formatted, new_row_number, "Status")).strip().lower()
        rank_before = _STATUS_RANK.get(before, -1)
        rank_after = _STATUS_RANK.get(after, -1)
        if before != after and rank_after < rank_before:
            regressed.append(f"{key[0]} ship {key[3]}: status went BACKWARDS {before!r} -> {after!r}")

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "status_regressed": regressed,
        "rows_before": len(old_rows),
        "rows_after": len(new_rows),
        "ignored_columns": list(ignore),
        "_records": records,
    }


# Scraped cost columns. A change here on a TERMINAL row cannot have come from a scraper (terminal
# rows are never re-read), so it is either a hand edit or a writer bug -- worth a look either way.
# Payout Amount / Payout Date / Insurance / Status / Tracking Submitted are deliberately NOT here:
# the buying-group sync writes those onto delivered rows every run, and that is the normal case.
_SCRAPED_MONEY_COLUMNS = ("Quantity", "Cost Per Item", "Total Cost", "Shipping", "Cashback Rate",
                          "Gift Card", "Sales Tax")


def classify_diff(diff: dict, opts: Options) -> list[Result]:
    """Turn a before/after diff into Results, so `--compare` composes with --strict and the exit code.

    A diff has no single right answer -- a new order legitimately appends -- but each KIND of change
    does: nothing in the system deletes rows, the upsert never rewrites its own key, and a new key
    that reuses a tracking number already on the sheet is the split-order duplicate the whole upsert
    design exists to prevent. Before this, all of that printed as prose that gated nothing.
    """
    rec = diff.get("_records") or {}
    added, removed, changed = rec.get("added", []), rec.get("removed", []), rec.get("changed", [])
    kept = rec.get("kept", [])
    out: list[Result] = []

    # --- identity changes: a removed key and an added key that are the SAME package -------------
    # The upsert never rewrites Order ID / Order Date / Item Name / Shipment; only a hand edit does,
    # and it orphans the row (every future re-check appends beside it). Match on the identity both
    # paths read identically -- (order, tracking) -- or (order, shipment) when untracked.
    def identity(r):
        return (r["order_id"], r["tracking"]) if r["tracking"] else (r["order_id"], "ship", r["shipment"])

    removed_by_id = {}
    for r in removed:
        removed_by_id.setdefault(identity(r), []).append(r)
    renamed, real_added, real_removed = [], [], []
    for r in added:
        twins = removed_by_id.get(identity(r))
        if twins:
            old = twins.pop(0)
            renamed.append(f"{r['order_id']} ship {r['shipment']}: key changed -- was {old['item'][:35]!r} "
                           f"(row {old['row']}), now {r['item'][:35]!r} (row {r['row']})")
        else:
            real_added.append(r)
    for rs in removed_by_id.values():
        real_removed.extend(rs)
    if renamed:
        out.append(Result("compare_identity_changed", "FAIL",
                          f"{len(renamed)} row(s) had a KEY cell edited -- future re-checks will append beside them",
                          _truncate(renamed, opts.max_detail)))

    # --- removed rows: nothing in the system deletes ------------------------------------------
    if real_removed:
        out.append(Result("compare_rows_removed", "FAIL",
                          f"{len(real_removed)} row(s) present before are gone -- nothing in the system deletes rows",
                          _truncate([f"row {r['row']} was {r['retailer']} {r['order_id']} ship {r['shipment']} "
                                     f"{r['item'][:35]!r}" for r in real_removed], opts.max_detail)))

    # --- appended rows: a real new order, or a duplicate of a row already there? -----------------
    existing_tracking = {(r["order_id"], r["tracking"]) for r in kept if r["tracking"]}
    duplicates, fresh = [], []
    for r in real_added:
        if r["tracking"] and (r["order_id"], r["tracking"]) in existing_tracking:
            duplicates.append(f"row {r['row']}: {r['order_id']} ship {r['shipment']} {r['item'][:35]!r} reuses "
                              f"tracking {r['tracking']} already on the sheet -- a re-keyed duplicate")
        else:
            fresh.append(r)
    if duplicates:
        out.append(Result("compare_appended_duplicate", "FAIL",
                          f"{len(duplicates)} appended row(s) reuse a tracking number an existing row of the "
                          "same order already has -- the split-order duplicate",
                          _truncate(duplicates, opts.max_detail)))
    if fresh:
        orders = {r["order_id"] for r in fresh}
        out.append(Result("compare_appended", "INFO",
                          f"{len(fresh)} row(s) appended across {len(orders)} order(s), none sharing a tracking "
                          "number with an existing row",
                          _truncate([f"row {r['row']}: {r['retailer']} {r['order_id']} ship {r['shipment']} "
                                     f"[{r['status']}] {r['item'][:35]!r}" for r in fresh], opts.max_detail)))

    # --- in-place changes ---------------------------------------------------------------------
    regressed = [f"{r['order_id']} ship {r['shipment']}: {was!r} -> {now!r}"
                 for r, col, was, now, _ in changed
                 if col == "Status" and _STATUS_RANK.get(now.lower(), -1) < _STATUS_RANK.get(was.lower(), -1)]
    if regressed:
        out.append(Result("compare_status_regressed", "FAIL",
                          f"{len(regressed)} row(s) moved BACKWARDS in status -- a hand correction was undone",
                          _truncate(regressed, opts.max_detail)))
    terminal_money = [f"row {r['row']}: {r['order_id']} ship {r['shipment']} [{before}] {col}: {was!r} -> {now!r}"
                      for r, col, was, now, before in changed
                      if col in _SCRAPED_MONEY_COLUMNS and before in TERMINAL_STATUSES]
    if terminal_money:
        out.append(Result("compare_terminal_money_changed", "WARN",
                          f"{len(terminal_money)} scraped cost cell(s) changed on TERMINAL row(s) -- no scraper "
                          "re-reads those, so this is a hand edit or a writer bug",
                          _truncate(terminal_money, opts.max_detail)))
    ordinary = [c for c in changed if not (c[1] == "Status" and _STATUS_RANK.get(c[3].lower(), -1) < _STATUS_RANK.get(c[2].lower(), -1))
                and not (c[1] in _SCRAPED_MONEY_COLUMNS and c[4] in TERMINAL_STATUSES)]
    touched = {c[0]["key"] for c in ordinary}
    out.append(Result("compare_updated", "PASS",
                      f"{len(ordinary)} cell(s) updated in place across {len(touched)} row(s); "
                      f"{diff['rows_before']} -> {diff['rows_after']} rows"))
    return out


def render_diff(diff: dict, source: str, max_detail: int) -> str:
    lines = ["", f"Changes since {source}:"]
    lines.append(
        f"  {diff['rows_before']} -> {diff['rows_after']} rows | "
        f"{len(diff['added'])} added | {len(diff['removed'])} removed | "
        f"{len(diff['changed'])} cell(s) changed"
        + (f"  (ignoring {', '.join(diff['ignored_columns'])})" if diff["ignored_columns"] else "")
    )
    if diff.get("status_regressed"):
        lines.append(f"  !! {len(diff['status_regressed'])} row(s) moved BACKWARDS in status")
    for label, entries in (
        ("REGRESSED", diff.get("status_regressed") or []),
        ("ADDED", diff["added"]),
        ("REMOVED", diff["removed"]),
        ("CHANGED", diff["changed"]),
    ):
        for entry in _truncate(entries, max_detail):
            lines.append(f"  {label:<9} {entry}")
    return "\n".join(lines)


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
    # INFO and SKIP must be in the tally or they vanish from the count and the totals stop adding up.
    tally = {s: sum(1 for r in results if r.status == s) for s in ("PASS", "INFO", "WARN", "FAIL", "SKIP")}
    lines.append(
        f"{tally['PASS']} passed | {tally['WARN']} warnings | {tally['FAIL']} failures"
        + (f" | {tally['INFO']} informational" if tally["INFO"] else "")
        + (f" | {tally['SKIP']} skipped" if tally["SKIP"] else "")
    )
    return "\n".join(lines)


def render_json(results: list[Result], meta: dict, strict: bool, diff: dict | None = None) -> str:
    payload = {
        "ok": exit_code(results, strict) == 0,
        "meta": meta,
        "checks": [
            {"name": r.name, "status": r.status, "summary": r.summary, "details": list(r.details)}
            for r in results
        ],
    }
    if diff is not None:
        payload["diff"] = {k: v for k, v in diff.items() if not k.startswith("_")}
    return json.dumps(payload, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only audit of the Google Sheet ledger. Writes nothing, ever.",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output (for before/after diffs)")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures in the exit code")
    parser.add_argument("--expect-rows", type=int, default=None, help="fail unless the sheet has exactly N data rows")
    parser.add_argument("-v", "--verbose", action="store_true", help="show detail lines for passing checks too")
    parser.add_argument("--save-snapshot", metavar="PATH", help="also write the raw grids to a local JSON file (a bare name lands under data/, which is gitignored)")
    parser.add_argument("--from-snapshot", metavar="PATH", help="audit a saved snapshot offline (no credentials, no API calls; bare names resolve under data/)")
    parser.add_argument("--compare", metavar="PATH", help="also report what changed vs an earlier --save-snapshot (added/removed/changed rows; bare names resolve under data/)")
    parser.add_argument("--stale-days", type=int, default=3, help="warn when an OPEN row hasn't been re-scraped in this many days (default 3)")
    args = parser.parse_args()

    if args.from_snapshot:
        from_path = _snapshot_path(args.from_snapshot)
        with open(from_path, encoding="utf-8") as f:
            grids = Grids.from_snapshot(json.load(f))
        grids.meta["source"] = f"snapshot {from_path}"
    else:
        worksheet, title = open_worksheet_readonly()
        grids = read_grids(worksheet, title)

    if not grids.formatted:
        print("The worksheet is empty -- nothing to audit. Nothing was created.", file=sys.stderr)
        raise SystemExit(2)

    if args.save_snapshot:
        save_path = _snapshot_path(args.save_snapshot)
        os.makedirs(save_path.parent, exist_ok=True)
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(grids.to_snapshot(), f, indent=2, default=str)
        print(f"Snapshot written to {save_path} (holds addresses and card digits -- do not commit).",
              file=sys.stderr)

    sheet = Sheet(grids)
    opts = Options(expect_rows=args.expect_rows, strict=args.strict, stale_days=args.stale_days)
    results = run_checks(sheet, opts)

    diff = None
    compare_path = _snapshot_path(args.compare) if args.compare else None
    if compare_path is not None:
        with open(compare_path, encoding="utf-8") as f:
            baseline = Grids.from_snapshot(json.load(f))
        diff = diff_snapshots(baseline, grids)
        results.extend(classify_diff(diff, opts))  # composes with --strict and the exit code

    if args.json:
        print(render_json(results, grids.meta, args.strict, diff))
    else:
        print(render_text(results, grids.meta, args.verbose))
        if diff is not None:
            print(render_diff(diff, str(compare_path), opts.max_detail))
    raise SystemExit(exit_code(results, args.strict))


if __name__ == "__main__":
    main()
