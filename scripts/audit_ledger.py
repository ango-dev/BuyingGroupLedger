"""Read-only audit of the ledger (data/ledger.sqlite3). WRITES NOTHING, EVER.

Why this exists: every schema change ended with the same manual ritual -- check the row count, look for duplicate upsert
keys, spot-check that Shipment is still an int and Card Last 4 still has its leading zeros. Those
results were written up as PROSE in the design notes and the audit itself was thrown away each time. This is
that ritual as a runnable artifact.

    python -m scripts.audit_ledger                        # audit the ledger
    python -m scripts.audit_ledger --expect-rows 23       # ... and assert the row count
    python -m scripts.audit_ledger --save-snapshot before.json   # lands in data/ (gitignored: it holds PII)
    python -m scripts.audit_ledger --from-snapshot before.json   # re-audit offline
    python -m scripts.audit_ledger --json                 # machine-readable, for before/after diffs

READ-ONLY IS ENFORCED IN TWO LAYERS, because an auditor that can mutate what it audits is worse
than no auditor at all:
  1. It opens the ledger through the READ-ONLY worksheet adapter (ledger_db/worksheet.py,
     read_only=True): every write method raises, so the capability simply isn't there. It never
     calls ledger.sync._get_worksheet(), the writers' opener.
  2. The checks never receive a worksheet -- only the frozen `Grids` value read once up front. No
     check *can* call a mutator because no check holds anything mutable.

ON READING FORMATTED VALUES: the adapter renders the grid three ways (formatted
text, the stored values, and the formula view), and the checks read all of them -- the upsert
builds its key from the formatted read, so to answer "will the next run duplicate a row?" the audit
has to see exactly the strings the writer will see.
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from models.order import (
    FIELDNAMES, MONEY_FREE_STATUSES, RETIRED_STATUSES, STATUSES, TERMINAL_STATUSES,
    normalize_shipment,
)
from ledger.sync import (
    HEADER,
    _INT_FIELDS,
    _NUMERIC_FIELDS,
    _STATUS_RANK,
    _SUPERSEDED_BLANK_FIELDS,
    _coerce,
    _parse_display_number,
)

# The date-serial epoch (1899-12-30), for reporting what day a stray date serial actually means.
_SHEETS_EPOCH = date(1899, 12, 30)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_real_date(text: str) -> bool:
    """Does an ISO-shaped string name a day that exists? The regex only tests the shape."""
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


# Where a bare `--save-snapshot NAME` lands. A snapshot is the whole ledger -- delivery addresses and
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

# Formula error values (#REF! and friends), matched as a WHOLE cell. Never as a "starts with #" prefix: costco_mapping
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
    """The same ledger read three ways. The disagreements between them are the point.

    formatted   FORMATTED_VALUE   -- always str. "4%", "$1,299.00"; a formula cell shows its RESULT.
                                    This is what sync_csv_to_ledger sees and keys rows on.
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


def open_ledger_readonly():
    """The ledger as a READ-ONLY worksheet (ledger_db/worksheet.py): every write refused.
    Deliberately NOT ledger.sync._get_worksheet(): the audit must never hold a writable handle on
    what it audits."""
    from config.settings import settings
    from ledger_db.store import LedgerDb
    from ledger_db.worksheet import DbWorksheet

    worksheet = DbWorksheet(LedgerDb(settings.ledger_db_path), read_only=True)
    return worksheet, worksheet.title


def read_grids(worksheet, spreadsheet_title: str = "") -> Grids:
    """Read the worksheet once per render mode. The ONLY function that touches the ledger."""
    from ledger_db.worksheet import ValueRenderOption

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
# The in-memory grid the checks operate on
# --------------------------------------------------------------------------------------------------


def _is_effectively_blank(sheet, grid, row_number: int) -> bool:
    """Is this row empty in every way that matters?

    A row whose ONLY content is an unticked `Tracking Submitted` checkbox is EMPTY. That column
    carried checkbox validation, which materialised a real `False` into every row the validation
    covers -- which is most of the grid, not just the data. Counting those as content would make
    `blank_order_id_rows` report hundreds of "orphans" and `content_outside_the_schema` warn about
    empty space, i.e. exactly the noise that gets an auditor ignored.

    Mirrors ledger.sync._last_occupied_row, which anchors appends on the same rule.
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
        """(row_number, row) for every data row, row_number being the 1-based row."""
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
    details.append("Fix: the ledger file migrates its columns on open (ledger_db/store.py); "
                   "an unknown column means the file was written by newer code")
    return Result("header_matches_schema", "FAIL", "header does not match the schema", _truncate(details, 20))


@check("row_count", requires_schema=False)
def check_row_count(sheet: Sheet, opts: Options) -> Result:
    total = max(0, len(sheet.grids.formatted) - 1)
    ledger = sum(1 for _ in sheet.ledger_rows(sheet.grids.formatted)) if sheet.schema_ok else total
    heights = {len(sheet.grids.formatted), len(sheet.grids.unformatted), len(sheet.grids.formula)}
    summary = f"{total} data rows ({ledger} with an Order ID)"

    # --expect-rows is evaluated FIRST and unconditionally. An earlier version returned the
    # height-disagreement WARN before ever comparing, so `--expect-rows N` reported success on the one
    # ledger state that most warrants a hard stop.
    if opts.expect_rows is not None and total != opts.expect_rows:
        return Result("row_count", "FAIL", f"{summary} -- expected {opts.expect_rows}")
    if len(heights) > 1:
        # sync_csv_to_ledger computes its append anchor as len(existing)+1 from the FORMATTED read
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
    # a number"). Warning about it would nag forever on a healthy ledger and make --strict exit 1 for
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
    # One number under several Order IDs is a COMBINED BOX: the buying group ships several orders'
    # units in one carton under one label, and the sync pairs each order's rows to it (history
    # 2026-09-14/15). Normal and permanent, so INFO.
    crossed = [
        f"tracking {number} is a combined box for {len(orders)} orders: {sorted(orders)}"
        for number, orders in by_number.items() if len(orders) > 1
    ]
    if not shared and not crossed:
        return Result("duplicate_tracking_keys", "PASS", f"{len(seen)} tracked row(s), all 1:1")
    # INFO for the same reason as duplicate_shipment_lines: a multi-SKU box legitimately puts several
    # rows behind one tracking number. It only means the tracking-based reconciliation declines to
    # fire there (it is guarded to the unambiguous 1:1 case), which is correct behaviour, not a fault.
    parts = []
    if crossed:
        parts.append(f"{len(crossed)} combined box(es) carry several orders")
    if shared:
        parts.append(f"{len(shared)} tracking number(s) cover several rows of one order")
    return Result(
        "duplicate_tracking_keys", "INFO",
        "; ".join(parts) + " -- normal; the tracking-based reconciliation won't fire on them",
        _truncate(crossed + shared, opts.max_detail),
    )


@check("package_id_per_shipment")
def check_package_id_per_shipment(sheet: Sheet, opts: Options) -> Result:
    """One package id under ONE Shipment number per order -- the exact shape of the 2026-08-22 bug.

    Package ID (column 33, beside Card Last 4) is the retailer's own identity for a physical package, and
    sync_csv_to_ledger matches on (Order ID, Package ID) before anything else. A multi-SKU carton is
    several rows sharing one id AND one Shipment number (fine, INFO). One id under TWO Shipment
    numbers is the same package booked twice (history 1f) -- FAIL. Retired rows are skipped: a
    superseded row keeps its id under an N+1 number by design. A cell stored as a NUMBER is a FAIL
    too: the mapping emits text (Costco ids carry leading zeros), so a numeric cell no longer equals
    what the next scrape sends and the match silently stops working.
    """
    grid = sheet.grids.formatted
    shipments_by_id: dict[tuple, dict[str, list[int]]] = {}
    numeric: list[str] = []
    for row_number, _ in sheet.ledger_rows(grid):
        stored = sheet.cell(sheet.grids.unformatted, row_number, "Package ID")
        if isinstance(stored, (int, float)) and not isinstance(stored, bool):
            numeric.append(f"row {row_number}: Package ID stored as {type(stored).__name__} {stored!r} -- text expected")
        package_id = str(sheet.cell(grid, row_number, "Package ID")).strip()
        if not package_id:
            continue
        if str(sheet.cell(grid, row_number, "Status")).strip().lower() in RETIRED_STATUSES:
            continue
        key = (str(sheet.cell(grid, row_number, "Order ID")), package_id)
        shipment = str(sheet.cell(grid, row_number, "Shipment")).strip()
        shipments_by_id.setdefault(key, {}).setdefault(shipment, []).append(row_number)
    split = [
        f"order {k[0]} package {k[1]} sits under shipments {sorted(v)}: rows "
        f"{sorted(n for rows in v.values() for n in rows)}"
        for k, v in shipments_by_id.items() if len(v) > 1
    ]
    cartons = [
        f"order {k[0]} package {k[1]} covers rows {rows}"
        for k, v in shipments_by_id.items() if len(v) == 1
        for rows in v.values() if len(rows) > 1
    ]
    if split or numeric:
        return Result(
            "package_id_per_shipment", "FAIL",
            f"{len(split)} package id(s) under several Shipment numbers, {len(numeric)} numeric cell(s)",
            _truncate(split + numeric + cartons, opts.max_detail),
        )
    if cartons:
        return Result(
            "package_id_per_shipment", "INFO",
            f"{len(shipments_by_id)} package id(s); {len(cartons)} cover several rows (multi-SKU cartons)",
            _truncate(cartons, opts.max_detail),
        )
    return Result("package_id_per_shipment", "PASS", f"{len(shipments_by_id)} package id(s), one Shipment number each")


@check("blank_order_id_rows")
def check_blank_order_id_rows(sheet: Sheet, opts: Options) -> Result:
    """A row with no Order ID can never be updated again -- both ledger_sync.py:317 and :360 skip it.

    It's a permanent orphan: every future re-check appends alongside it instead of updating it.

    It has a SECOND consequence that's easy to miss, so it's reported here too: the newest-first sort
    covers the whole block from row 2 down to the last row that HAS an Order ID, so an orphan sitting
    inside that span gets shuffled around by every sort. It won't necessarily sink to the bottom
    either -- the sort orders empty cells last, but a row blank only in Order ID still sorts on its
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


@check("shipment_numbers_contiguous")
def check_shipment_numbers_contiguous(sheet: Sheet, opts: Options) -> Result:
    """Every producer numbers an order's boxes 1..N, so a gap means a row went missing or an order was
    renumbered (the Costco unshipped-then-split caveat, the design notes). Neither breaks the upsert key,
    so this is WARN -- something to look at, not a corrupted ledger. Non-numeric labels and blank
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
                # so a return row carries the date it happened in Order Date (the hand-kept ledgers
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
    next re-check. That was already true on the ledger once (§8). Nothing in the codebase defends
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
    anything). So a ledger that has only taken updates since its last append is legitimately stale here.

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
            # A Receipt Link is dashboard-relative since 2026-09-18 (`/receipts/...`, served by the
            # dashboard wherever it is opened); the other links are absolute URLs.
            ok = value.startswith(("http://", "https://")) or (name == "Receipt Link" and value.startswith("/receipts/"))
            if value and not ok:
                fails.append(f"row {row_number}, {name}: {value!r} is not a URL"
                             + (" or a /receipts/ path" if name == "Receipt Link" else ""))
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
    """_reprorate_order_level (ledger/sync.py) rewrites each row's Shipping to its own
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
        # The share each row SHOULD carry: the order's shipping total by the row's share of its cost.
        # Compared to the cent (the reproration rounds each share to cents, so two rows' ratios can
        # differ in the fourth decimal without anything being wrong -- a false alarm until 2026-09-18).
        total_shipping = sum(shipping for _, shipping, _ in rows)
        total_cost = sum(cost for _, _, cost in rows)
        off = [(n, shipping, cost, round(total_shipping * cost / total_cost, 2)) for n, shipping, cost in rows
               if abs(shipping - total_shipping * cost / total_cost) > 0.011]
        if off:
            detail = ", ".join(f"row {n}: {shipping} (share would be {want})" for n, shipping, _, want in off)
            offenders.append(f"order {order_id}: {detail}")
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
    from ledger.sync import plan_buying_group_retag

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
    fills = plan.get("fills") or []
    if fills:
        status = "WARN"
        details.append(f"{len(fills)} cell(s) never filled though the card is known -- run `python -m scripts.backfill_profit_columns`")
    # A rate that disagrees with the cards list is not a fault: rate cells record the rate AT
    # PURCHASE TIME and the list holds only the current one (standing ruling, the design notes) -- information.
    # A last 4 the list does not know IS worth a warning: its rows earn the default
    # rate and count against no spend limit until the card is added on Settings > Cards.
    changes = plan.get("changes") or []
    if changes:
        status = status if status == "WARN" else "INFO"
        details.append(f"{len(changes)} rate cell(s) differ from the cards list -- rates are era-specific, leave them")
    unresolved = plan.get("unresolved") or []
    if unresolved:
        status = "WARN"
        last4s = sorted({str(u[-1] if isinstance(u, (tuple, list)) else u) for u in unresolved})
        details.append(f"{len(unresolved)} row(s) on card(s) not in the cards list ({', '.join('...' + x for x in last4s)}) -- "
                       "add them on Settings > Cards, or they earn the default rate and count against no spend limit")
    summary = ("Card + Cashback Rate resolve cleanly" if status == "PASS"
               else ("cards on the ledger not in the cards list" if unresolved and not fills
                     else "cells the backfill could fill") if status == "WARN" else "known differences from the cards list")
    return Result("card_and_rate_coverage", status, summary, _truncate(details, opts.max_detail))


@check("legacy_blank_shipment")
def check_legacy_blank_shipment(sheet: Sheet, opts: Options) -> Result:
    """A row with an Order ID but no Shipment number — written before that column existed.

    the design notes has carried "check the ledger once for this" as an open item. It matters because
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

    Two real hazards, not tidiness. (1) `append_rows` once auto-detected the "table" on the live grid
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


@check("payout_is_cost_weighted")
def check_payout_is_cost_weighted(sheet: Sheet, opts: Options) -> Result:
    """A payout arrives per PACKAGE, but the ledger is one row per (shipment x item).

    So a box holding two items has two rows behind one tracking number, and `sync_tracking` splits the
    payout between them by each row's share of the package's Total Cost — writing the full amount to
    each would book the group's money twice. This asserts the split actually happened: rows sharing
    `(Order ID, Tracking Number)` must show the same Payout/Cost ratio.

    Nothing else can catch a double-booked payout. `Total Profit` reads Actual Payout straight from
    the cell and re-derives nothing, so a doubled payout just reads as a larger, plausible profit.
    """
    packages: dict[tuple, list[tuple[int, float, float]]] = {}
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        key = sheet.tracking_key(sheet.grids.formatted, row_number)
        if key is None:
            continue
        payout = _parse_display_number(sheet.cell(sheet.grids.unformatted, row_number, "Actual Payout"))
        cost = _parse_display_number(sheet.cell(sheet.grids.unformatted, row_number, "Total Cost"))
        if payout is None or not cost:
            continue
        packages.setdefault(key, []).append((row_number, payout, cost))

    doubled, uneven, checked = [], [], 0
    for key, rows in packages.items():
        if len(rows) < 2:
            continue
        checked += 1
        total_payout = sum(p for _, p, _ in rows)
        total_cost = sum(c for _, _, c in rows)
        # Compared to the cent: the sync rounds each share to cents.
        off = [n for n, p, c in rows if abs(p - total_payout * c / total_cost) > 0.011]
        if not off:
            continue
        detail = ", ".join(f"row {n}: {p} on {c}" for n, p, c in rows)
        amounts = {round(p, 2) for _, p, _ in rows}
        costs = {round(c, 2) for _, _, c in rows}
        # The tell-tale of a double-booking: every row carries the SAME amount over DIFFERENT costs
        # -- the package's whole payout written to each row. Anything else is a real per-item
        # payout (hand-entered or imported: MOD paid $74 on a $59.98 Echo Dot beside 1.005x on the
        # watches in the same box, 2026-08-30), which is information, not a fault.
        if len(amounts) == 1 and len(costs) > 1 and next(iter(amounts)) > 0:
            doubled.append(f"order {key[0]} package {key[1]}: {detail} -- the same amount on every row")
        else:
            uneven.append(f"order {key[0]} package {key[1]}: {detail}")
    if not doubled and not uneven:
        return Result("payout_is_cost_weighted", "PASS", f"{checked} multi-row package(s) split pro-rata")
    if doubled:
        return Result(
            "payout_is_cost_weighted", "WARN",
            f"{len(doubled)} package(s) carry the same payout on every row -- the money may be booked twice",
            _truncate(doubled + uneven, opts.max_detail),
        )
    return Result(
        "payout_is_cost_weighted", "INFO",
        f"{len(uneven)} package(s) hold per-item payouts rather than a cost-weighted split (hand-entered or imported)",
        _truncate(uneven, opts.max_detail),
    )


#: What every row must carry, and what each stage must (or must not) carry on top. A cancelled / superseded row carries no money by design, so only its identity
#: is mandatory there.
MANDATORY_ALWAYS = ("Order Date", "Status", "Retailer", "Item Name", "Shipment", "Order ID")
# ordered onward: the order link, where it ships to, which card paid and its
# last 4, and COGS (computed from the cost inputs -- blank only when they are)
MANDATORY_COSTED = ("Quantity", "Cost Per Item", "Total Cost", "Profile", "Order Link", "Delivery Address",
                    "Card", "Card Last 4", "COGS", "Buying Group")
#: Cells that must hold TRUE from a stage on: a shipped row's tracking number has been submitted to
#: the buying group.
MANDATORY_TICKED_BY_STAGE = {
    "shipped": ("Tracking Submitted",), "delivered": ("Tracking Submitted",),
    "paid": ("Tracking Submitted",), "return": ("Tracking Submitted",),
}
MANDATORY_BY_STAGE = {
    "shipped": ("Tracking Number",),
    # delivered: the retailer's receipt too -- from then on the order is a cost
    # to substantiate, and the capture has had every chance to run
    "delivered": ("Tracking Number", "Delivery Date", "Receipt Link"),
    "paid": ("Tracking Number", "Actual Payout", "Payout Date", "Receipt Link", "Insurance"),
    "return": ("Tracking Number", "Return Qty", "Return Date", "Receipt Link"),
}
#: Cells a stage should NOT have yet: a value there means the status is stale (WARN, not FAIL).
UNEXPECTED_BY_STAGE = {
    # Delivery Date is NOT here: the scraper fills it with the retailer's estimate before delivery.
    #
    "ordered": ("Tracking Number", "Payout Date", "Actual Payout"),
    "shipped": ("Payout Date", "Actual Payout"),
    "delivered": ("Payout Date", "Actual Payout"),
}
#: A gift-card row (Buying Group = the gift-card marker, or an item name that says so) has no
#: package: no tracking number, no delivery address, no delivery date. One SOLD to a buying group
#: (AI, for example) must still have Tracking Submitted ticked from shipped on -- the tick says the
#: gift card was submitted.
GIFT_CARD_HINTS = ("gift card", "egift", "e-gift", "balance reload")
GIFT_CARD_EXEMPT = ("Tracking Number", "Delivery Address", "Delivery Date")


def mandatory_gaps(cells: "Mapping[str, str]") -> tuple[list[str], list[str]]:
    """The mandatory rule as ONE function, so the audit and the dashboard's importer (web/importer.py)
    cannot drift: `cells` keyed by FIELD name (models.order.FIELDNAMES) with display text as values;
    returns (missing, unticked) as DISPLAY names -- the cells that must hold a value for the row's
    status but are blank, and the cells that must be ticked but are not. MANDATORY_ALWAYS,
    MANDATORY_COSTED (skipped for cancelled / superseded), MANDATORY_BY_STAGE and
    MANDATORY_TICKED_BY_STAGE, with GIFT_CARD_EXEMPT and the unrouted / gift-card tick rule."""
    from config.warehouses import is_deliberately_unrouted

    def cell(name: str) -> str:
        return str(cells.get(_FIELD_FOR_HEADER.get(name, name), "") or "").strip()

    status = cell("Status").lower()
    required = list(MANDATORY_ALWAYS)
    if status not in ("cancelled", "superseded"):
        required += MANDATORY_COSTED
    required += MANDATORY_BY_STAGE.get(status, ())
    ticked = list(MANDATORY_TICKED_BY_STAGE.get(status, ()))
    group = cell("Buying Group")
    unrouted = is_deliberately_unrouted(group)
    gift_card = unrouted or any(h in cell("Item Name").lower() for h in GIFT_CARD_HINTS)
    if gift_card:
        required = [name for name in required if name not in GIFT_CARD_EXEMPT]
        if unrouted or not group:
            ticked = []  # nothing to submit; a card sold to a group keeps the tick requirement
    missing = [name for name in required if not cell(name)]
    unticked = [name for name in ticked if cell(name).lower() not in ("true", "1", "yes", "checked")]
    return missing, unticked


_FIELD_FOR_HEADER = {h: f for f, h in zip(FIELDNAMES, HEADER)}


@check("mandatory_by_stage")
def check_mandatory_by_stage(sheet: Sheet, opts: Options) -> Result:
    """Every row carries its identity and, unless cancelled / superseded, its cost inputs, profile,
    order link, delivery address, card, card last 4 and COGS; each stage carries what that stage implies (a shipped row a tracking number, a
    delivered row a delivery date and the retailer's receipt, a paid row an amount, a date and its insurance, a
    return its quantity and date). A cell that should still be blank
    at a stage (a tracking number on an `ordered` row, a payout on an open one) is a stale
    status, a WARN. An IMPOSSIBLE combination -- Tracking Submitted ticked with no tracking number
    or on an ordered row, a payout date with no amount, a delivery / payout / return date before the
    order date -- is a FAIL. The one place a cell deleted by accident from the table is caught."""
    from config.warehouses import is_deliberately_unrouted

    grid = sheet.grids.formatted
    fails, warns = [], []
    for row_number, _ in sheet.ledger_rows(grid):
        def cell(name: str) -> str:
            return str(sheet.cell(grid, row_number, name)).strip()

        status = cell("Status").lower()
        group = cell("Buying Group")
        unrouted = is_deliberately_unrouted(group)
        gift_card = unrouted or any(h in cell("Item Name").lower() for h in GIFT_CARD_HINTS)
        submitted = cell("Tracking Submitted").lower() in ("true", "1", "yes", "checked")
        missing, unticked = mandatory_gaps({f: cell(h) for f, h in zip(FIELDNAMES, HEADER)})
        # The remedy beside the finding: the receipts backfill under Tools -> Checks captures a receipt a
        # terminal order never got.
        missing = ["Receipt Link (the receipts backfill under Tools -> Checks captures it)" if name == "Receipt Link" else name
                   for name in missing]
        missing += [f"{name} (not ticked)" for name in unticked]
        if missing:
            fails.append(f"row {row_number} ({status or 'no status'}): missing {', '.join(missing)}")
        # IMPOSSIBLE combinations. A ticked Tracking
        # Submitted needs a number to have been submitted, unless the row is a gift card sold to a
        # group (the tick is the card's submission); an ordered row has nothing to submit at all.
        impossible = []
        if submitted and status == "ordered":
            impossible.append("Tracking Submitted ticked on an ordered row -- nothing has shipped")
        elif submitted and not cell("Tracking Number") and not (gift_card and group and not unrouted):
            impossible.append("Tracking Submitted ticked with no Tracking Number")
        if cell("Payout Date") and not cell("Actual Payout"):
            impossible.append("a Payout Date with no Actual Payout")
        # The other way round, at ANY status (a hand-entered payout on a row left `delivered` is
        # the usual case): the tax report keys income on Payout Date, so this money is income in
        # no year until the date is filled.
        paid_amount = _parse_display_number(cell("Actual Payout"))
        if paid_amount not in (None, 0) and not cell("Payout Date"):
            impossible.append("an Actual Payout with no Payout Date -- income in no tax year until the date is filled")
        order_date = cell("Order Date")
        for name in ("Delivery Date", "Payout Date", "Return Date"):
            when = cell(name)
            if order_date and when and len(when) >= 10 and len(order_date) >= 10 and when[:10] < order_date[:10]:
                impossible.append(f"{name} {when} is before the Order Date {order_date}")
        if impossible:
            fails.append(f"row {row_number} ({status or 'no status'}): {'; '.join(impossible)}")
        stale = [name for name in UNEXPECTED_BY_STAGE.get(status, ()) if cell(name)]
        if stale:
            warns.append(f"row {row_number} ({status}): carries {', '.join(stale)} -- is the status stale?")
    if fails:
        return Result("mandatory_by_stage", "FAIL",
                      f"{len(fails)} row(s) miss a mandatory cell for their stage",
                      _truncate(fails + warns, opts.max_detail))
    if warns:
        return Result("mandatory_by_stage", "WARN",
                      f"{len(warns)} row(s) carry a cell their stage should not have yet -- a stale status?",
                      _truncate(warns, opts.max_detail))
    return Result("mandatory_by_stage", "PASS", "every row carries what its stage requires")


#: What each retailer's identifiers look like: the shape of its order numbers, the domain its
#: order links live on, and the slug its receipt files are keyed under (receipts/sources.py).
RETAILER_SHAPES = {
    "amazon": (r"^\d{3}-\d{7}-\d{7}$", ("amazon.com",), "amazon"),
    "amazon business": (r"^\d{3}-\d{7}-\d{7}$", ("amazon.com",), "amazon-business"),
    "best buy": (r"^BBY\d+-\d+$", ("bestbuy.com",), "bestbuy"),  # BBY01-, BBY02-, ...
    "costco": (r"^\d+$", ("costco.com",), "costco"),
}
#: Amounts that can never be negative (a payout can: a clawback nets below zero, so it is a WARN).
NEVER_NEGATIVE = ("Quantity", "Cost Per Item", "Total Cost", "Shipping", "Sales Tax", "Gift Card",
                  "Rewards Used", "Insurance", "Expected Payout", "Return Qty")


@check("impossible_values")
def check_impossible_values(sheet: Sheet, opts: Options) -> Result:
    """Values and combinations that cannot be true of a real order, whatever its stage. mandatory_by_stage says what each stage must and must not carry; this is the
    rest: amounts below zero or above what they are part of, a quantity under one, dates in the
    future or before the order, identifiers that do not fit their retailer, links without what
    they link, money on a cancelled row, a payment from nobody. Each FAIL names the row and the
    contradiction; the few that have an innocent reading are WARNs."""
    import re
    from datetime import date

    from config.warehouses import is_deliberately_unrouted

    grid, raw = sheet.grids.formatted, sheet.grids.unformatted
    today = date.today().isoformat()
    fails, warns = [], []
    for row_number, _ in sheet.ledger_rows(grid):
        def cell(name: str) -> str:
            return str(sheet.cell(grid, row_number, name)).strip()

        def number(name: str):
            return _parse_display_number(sheet.cell(raw, row_number, name))

        status = cell("Status").lower()
        bad, odd = [], []
        # --- amounts ---
        for name in NEVER_NEGATIVE:
            value = number(name)
            if value is not None and value < 0:
                bad.append(f"{name} {value} is negative")
        payout = number("Actual Payout")
        if payout is not None and payout < 0:
            odd.append(f"Actual Payout {payout} is negative (a clawback, or a typo?)")
        qty = number("Quantity")
        if qty is not None and status not in MONEY_FREE_STATUSES and qty < 1:
            bad.append(f"Quantity {qty} is under one")
        cost, ship, tax = number("Total Cost") or 0.0, number("Shipping") or 0.0, number("Sales Tax") or 0.0
        gift, rewards = number("Gift Card"), number("Rewards Used")
        if gift is not None and cost and gift > cost + ship + tax + 0.01:
            bad.append(f"Gift Card {gift} exceeds the order's cost, shipping and tax ({round(cost + ship + tax, 2)})")
        if rewards is not None and cost and rewards > cost + 0.01:
            bad.append(f"Rewards Used {rewards} exceeds Total Cost {cost}")
        if status == "cancelled":
            money = [n for n in ("Actual Payout", "Expected Payout", "Insurance", "Total Cost") if number(n)]
            if money:
                bad.append("a cancelled row carries " + ", ".join(money))
        # --- dates ---
        order_date = cell("Order Date")[:10]
        for name in ("Order Date", "Delivery Date", "Payout Date", "Return Date"):
            when = cell(name)[:10]
            # A Delivery Date ahead of today on an open row is the retailer's ESTIMATE (the
            # dashboard tags it "est."); the run flips the row to delivered when the package
            # lands. Once delivered, a future date is impossible.
            if name == "Delivery Date" and status in ("ordered", "shipped"):
                continue
            if len(when) == 10 and when > today:
                bad.append(f"{name} {when} is in the future")
        scraped = cell("Last Scraped At")[:10]
        if len(scraped) == 10 and len(order_date) == 10 and scraped < order_date:
            bad.append(f"Last Scraped At {scraped} is before the Order Date {order_date}")
        delivered, returned = cell("Delivery Date")[:10], cell("Return Date")[:10]
        if len(delivered) == 10 and len(returned) == 10 and returned < delivered:
            odd.append(f"Return Date {returned} is before the Delivery Date {delivered} (refused at the door?)")
        # --- identity and links ---
        retailer = cell("Retailer").lower()
        shape = RETAILER_SHAPES.get(retailer)
        order_id, order_link, receipt = cell("Order ID"), cell("Order Link"), cell("Receipt Link")
        if shape:
            pattern, domains, slug = shape
            if order_id and not re.match(pattern, order_id):
                bad.append(f"Order ID {order_id!r} is not the shape of a {cell('Retailer')} order number")
            if order_link.startswith("http") and not any(f".{d}/" in order_link or f"//{d}/" in order_link or order_link.endswith(d) for d in domains):
                bad.append(f"Order Link points at another site than {cell('Retailer')}")
            if receipt.startswith("/receipts/") and not receipt.startswith(f"/receipts/{slug}/"):
                bad.append(f"Receipt Link is filed under another retailer than {cell('Retailer')}")
        # On Amazon (both) the Tracking Link is the package-tracking page the scraper hops to FOR
        # the number, so a link with no number yet is the normal shape of an open Amazon row;
        #elsewhere the link is derived from the number.
        if cell("Tracking Link") and not cell("Tracking Number") and not retailer.startswith("amazon"):
            bad.append("a Tracking Link with no Tracking Number")
        last4 = cell("Card Last 4")
        if last4 and not (last4.isdigit() and len(last4) == 4):
            bad.append(f"Card Last 4 {last4!r} is not four digits")
        # --- who paid ---
        group = cell("Buying Group")
        # A gift-card row (Buying Group = the marker) is paid when its value is spent in other
        # orders, with no group of its own, so
        # only a BLANK group is nobody.
        if status == "paid" and not group:
            bad.append("paid, but no buying group to have paid it")
        if number("Insurance") and group.lower().replace(" ", "") in ("mod", "maxoutdeals"):
            odd.append("Insurance on a MaxOutDeals row (only BFMR files insurance)")
        if bad:
            fails.append(f"row {row_number} ({status or 'no status'}): " + "; ".join(bad))
        if odd:
            warns.append(f"row {row_number} ({status or 'no status'}): " + "; ".join(odd))
    if fails:
        return Result("impossible_values", "FAIL", f"{len(fails)} row(s) hold a value that cannot be true",
                      _truncate(fails + warns, opts.max_detail))
    if warns:
        return Result("impossible_values", "WARN", f"{len(warns)} row(s) hold a value worth a second look",
                      _truncate(warns, opts.max_detail))
    return Result("impossible_values", "PASS", "no impossible value on any row")


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
        payout = _parse_display_number(sheet.cell(sheet.grids.unformatted, row_number, "Actual Payout"))
        order_id = sheet.cell(sheet.grids.formatted, row_number, "Order ID")
        if payout is None:
            blanks.append(f"row {row_number}: order {order_id} is paid but has no Actual Payout")
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
    split (ledger/sync.py), so that box absorbs none of the order's shipping and its sibling
    row absorbs all of it.

    Nothing else surfaces this after the one alert fired at creation time.
    """
    pending, billed = [], []
    for row_number, _ in sheet.ledger_rows(sheet.grids.formatted):
        quantity = str(sheet.cell(sheet.grids.formatted, row_number, "Quantity")).strip()
        if quantity != "*":
            continue
        order_id = sheet.cell(sheet.grids.formatted, row_number, "Order ID")
        payout = sheet.cell(sheet.grids.unformatted, row_number, "Actual Payout")
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
    is `(Total Cost - Return Qty x Cost Per Item - Gift Card + Shipping + Sales Tax - Rewards Used)
    * (1 - Cashback Rate) + Rewards Used`, so a blank rate quietly computes the FULL cost as cost of goods -- overstating COGS,
    understating income, and under-reporting tax. Nothing else in the audit looks at it, and the
    number stays perfectly plausible while being wrong.

    Three shapes, in decreasing severity:

    - **COGS with no Cashback Rate** -- FAIL. The cost side is overstated by the rebate.
    - **A payout with no COGS** -- FAIL. Income recorded with no cost against it, so profit is
      overstated. Usually a Total Cost that never landed.
    - **COGS with no SETTLED payout** -- reported, not failed. It is the NORMAL state of an order
      that has shipped but not been paid yet, and at a year boundary it is exactly the straddle that
      makes the cost and income sides fall in different tax years. Worth seeing, never worth
      failing. "Settled" is a payout WITH its Payout Date or a paid/return status -- not the amount
      alone, because since 2026-09-11 the sync fills Actual Payout with the group's COMMITTED price
      while the package is still open (see sync_tracking), and a commitment is not income.

    Gift-card rows are exempt from the third shape entirely: a gift card is a real cost that will
    NEVER have a payout of its own, because the income arrives through the order it funded (whose own
    cost was netted down by the card, so nothing is double-counted).
    """
    from config.warehouses import is_deliberately_unrouted

    grid, unf = sheet.grids.formatted, sheet.grids.unformatted
    no_rate, no_cogs, unpaid, gift = [], [], [], 0
    for row_number, _ in sheet.ledger_rows(grid):
        status = str(sheet.cell(grid, row_number, "Status")).strip().lower()
        if status in MONEY_FREE_STATUSES:
            continue  # carries no money by design -- see ledger_sync._blank_money_for_status
        cogs = _parse_display_number(sheet.cell(unf, row_number, "COGS"))
        rate = _parse_display_number(sheet.cell(unf, row_number, "Cashback Rate"))
        payout = _parse_display_number(sheet.cell(unf, row_number, "Actual Payout"))
        order_id = sheet.cell(grid, row_number, "Order ID")

        if cogs and rate is None:
            card = sheet.cell(grid, row_number, "Card")
            no_rate.append(f"row {row_number}: order {order_id} (card {card!r}) -- COGS counts the "
                           "full cost because no rate resolved")
        # `cogs is None`, not `not cogs`: a referral bonus or credit has a real $0 cost, so its
        # COGS is a genuine 0, and 0 income-with-cost-0 is exactly right at year end.
        if payout and cogs is None:
            no_cogs.append(f"row {row_number}: order {order_id} paid {payout} with no COGS")
        payout_date = str(sheet.cell(unf, row_number, "Payout Date") or "").strip()
        settled = bool(payout) and (bool(payout_date) or status in ("paid", "return"))
        if cogs and not settled:
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


@check("superseded_rows_carry_no_money")
def check_superseded_rows_carry_no_money(sheet: Sheet, opts: Options) -> Result:
    """THE one non-negotiable of a kept superseded row: it carries NO money.

    A superseded row records a tracking number Amazon re-issued for a package that lives on under
    its live row. Keeping it is only safe while every amount cell is blank -- Quantity included,
    since it is the multiplier that booked the re-labelled package's cost twice in the first place.
    COGS and Total Profit blank themselves ONLY because Total Cost and Payout are blank, so those two
    are checked as well: a value in either means an input crept back in.
    """
    columns = [HEADER[FIELDNAMES.index(f)] for f in _SUPERSEDED_BLANK_FIELDS] + ["COGS", "Total Profit"]
    offenders, count = [], 0
    grid, unf = sheet.grids.formatted, sheet.grids.unformatted
    for row_number, _ in sheet.ledger_rows(grid):
        if str(sheet.cell(grid, row_number, "Status")).strip().lower() not in RETIRED_STATUSES:
            continue
        count += 1
        order_id = sheet.cell(grid, row_number, "Order ID")
        for column in columns:
            value = sheet.cell(unf, row_number, column)
            if value not in ("", None):
                offenders.append(f"row {row_number}: order {order_id} -- {column} holds {value!r}")
    if offenders:
        return Result(
            "superseded_rows_carry_no_money", "FAIL",
            f"{len(offenders)} money cell(s) on superseded row(s) are not blank -- the re-labelled "
            "package's cost or payout is being counted twice",
            _truncate(offenders, opts.max_detail),
        )
    return Result("superseded_rows_carry_no_money", "PASS",
                  f"{count} superseded row(s), every money cell blank")


@check("cashback_rate_sane")
def check_cashback_rate_sane(sheet: Sheet, opts: Options) -> Result:
    """A rate must be a fraction in [0, 1].

    models/card.py enforces this on the CONFIG side, but nothing enforces it on the ledger, and the
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


# --------------------------------------------------------------------------------------------------
# Running and rendering
# --------------------------------------------------------------------------------------------------


def run_checks(sheet: Sheet, opts: Options) -> list[Result]:
    """Every check, in registration order."""
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
    """What changed between two reads of the ledger, keyed by the primary upsert key.

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
        # return signal, so a return is typed onto the ledger BY HAND while MOD keeps reporting that
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
# Actual Payout / Payout Date / Insurance / Status / Tracking Submitted are deliberately NOT here:
# the buying-group sync writes those onto delivered rows every run, and that is the normal case.
_SCRAPED_MONEY_COLUMNS = ("Quantity", "Cost Per Item", "Total Cost", "Shipping", "Cashback Rate",
                          "Gift Card", "Sales Tax", "Rewards Used")


def classify_diff(diff: dict, opts: Options) -> list[Result]:
    """Turn a before/after diff into Results, so `--compare` composes with --strict and the exit code.

    A diff has no single right answer -- a new order legitimately appends -- but each KIND of change
    does: nothing in the system deletes rows, the upsert never rewrites its own key, and a new key
    that reuses a tracking number already on the ledger is the split-order duplicate the whole upsert
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
    renamed, retired, real_added, real_removed = [], [], [], []
    for r in added:
        twins = removed_by_id.get(identity(r))
        if twins:
            old = twins.pop(0)
            if r["status"] in RETIRED_STATUSES:
                # The ONE key change the system makes on purpose: fix_superseded_shipments renumbers
                # a dead row after the live boxes while marking it superseded.
                retired.append(f"{r['order_id']}: row {r['row']} marked superseded, shipment "
                               f"{old['shipment']} -> {r['shipment']} ({r['tracking']})")
            else:
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
    if retired:
        out.append(Result("compare_rows_retired", "INFO",
                          f"{len(retired)} row(s) marked superseded (key changed by the repair, as designed)",
                          _truncate(retired, opts.max_detail)))

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
                              f"tracking {r['tracking']} already on the ledger -- a re-keyed duplicate")
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
    # A terminal row's cost changing is a hand edit or a writer bug -- EXCEPT the repair that retires
    # it: marking a row superseded blanks its money by design, so an after-status in
    # RETIRED_STATUSES is the expected transition, not an anomaly.
    terminal_money = [f"row {r['row']}: {r['order_id']} ship {r['shipment']} [{before}] {col}: {was!r} -> {now!r}"
                      for r, col, was, now, before in changed
                      if col in _SCRAPED_MONEY_COLUMNS and before in TERMINAL_STATUSES
                      and r["status"] not in RETIRED_STATUSES]
    if terminal_money:
        out.append(Result("compare_terminal_money_changed", "WARN",
                          f"{len(terminal_money)} scraped cost cell(s) changed on TERMINAL row(s) -- no scraper "
                          "re-reads those, so this is a hand edit or a writer bug",
                          _truncate(terminal_money, opts.max_detail)))
    ordinary = [c for c in changed if not (c[1] == "Status" and _STATUS_RANK.get(c[3].lower(), -1) < _STATUS_RANK.get(c[2].lower(), -1))
                and not (c[1] in _SCRAPED_MONEY_COLUMNS and c[4] in TERMINAL_STATUSES
                         and c[0]["status"] not in RETIRED_STATUSES)]
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
        description="Read-only audit of the ledger. Writes nothing, ever.",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output (for before/after diffs)")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures in the exit code")
    parser.add_argument("--expect-rows", type=int, default=None, help="fail unless the ledger has exactly N data rows")
    parser.add_argument("-v", "--verbose", action="store_true", help="show detail lines for passing checks too")
    parser.add_argument("--save-snapshot", metavar="PATH", help="also write the raw grids to a local JSON file (a bare name lands under data/, which is gitignored)")
    parser.add_argument("--from-snapshot", metavar="PATH", help="audit a saved snapshot offline (bare names resolve under data/)")
    parser.add_argument("--compare", metavar="PATH", help="also report what changed vs an earlier --save-snapshot (added/removed/changed rows; bare names resolve under data/)")
    parser.add_argument("--stale-days", type=int, default=3, help="warn when an OPEN row hasn't been re-scraped in this many days (default 3)")
    args = parser.parse_args()

    if args.from_snapshot:
        from_path = _snapshot_path(args.from_snapshot)
        with open(from_path, encoding="utf-8") as f:
            grids = Grids.from_snapshot(json.load(f))
        grids.meta["source"] = f"snapshot {from_path}"
    else:
        worksheet, title = open_ledger_readonly()
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
