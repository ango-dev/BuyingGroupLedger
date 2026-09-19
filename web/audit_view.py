"""The Audit page: scripts/audit_sheet's checks run against the ledger the dashboard serves, and
every row a check names is mapped back to its order so the Orders view (table or cards) can show
the affected rows with what is wrong beside them.

The checks are the audit's own, unchanged: the same `run_checks` the CLI runs, over the same three
grids. What differs per backend is only where the grids come from -- the SQLite ledger through the
worksheet adapter (the CLI's path under `db`), the live Sheet through the read-only scope, or a
CSV snapshot re-read once. The Sheet-only checks (formulas, formats, merges) report SKIP unless the
backend IS the Sheet. A check's detail lines name rows ("row 157: order ... has no status", "rows
[2, 3]: ..."); those numbers are read off the audited grid's own key columns, so a finding lands
on the right order whatever numbering the dashboard's rows carry.
"""
from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass, field
from typing import Callable

from scripts.audit_sheet import Grids, Options, Result, Sheet, read_grids, run_checks

#: The row numbers a detail line names: "row 157: ...", "row 157, Card: ...", "rows [2, 3]: ...".
_ROWS = re.compile(r"^rows?\s+([\d,\s\[\]]+)")
#: Statuses whose details are findings (a PASS has none; a SKIP is a note, not a row problem).
FINDING_STATUSES = ("FAIL", "WARN", "INFO")

RowKey = tuple[str, str, str, str]


def rows_named(line: str) -> list[int]:
    match = _ROWS.match(line.strip())
    if not match:
        return []
    return [int(x) for x in re.findall(r"\d+", match.group(1))]


def key_of(order_id, order_date, item_name, shipment) -> RowKey:
    return (str(order_id or "").strip(), str(order_date or "").strip(),
            str(item_name or "").strip(), str(shipment or "").strip())


@dataclass(frozen=True)
class Finding:
    check: str
    status: str
    line: str


@dataclass
class AuditReport:
    results: list[Result]
    #: Every finding that names a row, by that row's upsert key.
    by_key: dict[RowKey, list[Finding]] = field(default_factory=dict)
    #: The keys each check flagged, in first-seen order.
    keys_by_check: dict[str, list[RowKey]] = field(default_factory=dict)
    #: Detail lines that name no row (counts, advice), by check.
    notes: dict[str, list[str]] = field(default_factory=dict)

    @property
    def checks(self) -> list[dict]:
        """One entry per check for the page's summary table."""
        out = []
        for r in self.results:
            out.append({"name": r.name, "status": r.status, "summary": r.summary,
                        "rows": len(self.keys_by_check.get(r.name, [])),
                        "notes": self.notes.get(r.name, [])})
        return out

    @property
    def flagged_checks(self) -> list[str]:
        return [name for name, keys in self.keys_by_check.items() if keys]

    def keys_for(self, checks: tuple[str, ...]) -> set[RowKey]:
        """The keys flagged by ANY of `checks`; every flagged key when `checks` is empty."""
        wanted = checks or tuple(self.keys_by_check)
        return {k for name in wanted for k in self.keys_by_check.get(name, [])}

    def counts(self) -> dict[str, int]:
        out = {"FAIL": 0, "WARN": 0, "INFO": 0, "PASS": 0, "SKIP": 0}
        for r in self.results:
            out[r.status] = out.get(r.status, 0) + 1
        return out


def run_audit(grids: Grids, *, stale_days: int = 3) -> AuditReport:
    """Every check over `grids`, its row-level findings keyed by the rows' upsert keys."""
    sheet = Sheet(grids)
    # No truncation: the page shows every affected row, not the CLI's first eight.
    results = run_checks(sheet, Options(max_detail=10_000_000, stale_days=stale_days))
    report = AuditReport(results=results)
    grid = grids.formatted
    for r in results:
        if r.status not in FINDING_STATUSES:
            continue
        for line in r.details:
            numbers = rows_named(line)
            if not numbers:
                report.notes.setdefault(r.name, []).append(line)
                continue
            for n in numbers:
                if n < 2 or n > len(grid):
                    continue
                key = key_of(*sheet.primary_key(grid, n))
                if not key[0]:
                    continue
                report.by_key.setdefault(key, []).append(Finding(r.name, r.status, line))
                keys = report.keys_by_check.setdefault(r.name, [])
                if key not in keys:
                    keys.append(key)
    return report


# --------------------------------------------------------------------------------------------------
# Where the grids come from, per backend
# --------------------------------------------------------------------------------------------------


def grids_from_snapshot(snapshot) -> Grids:
    """A CSV backup read three ways: the same grid, with the two formula columns carrying the
    values the reader computes for them (a backup stores the formula TEXT, which every money
    check would read as blank). Rows sit at their own row numbers; a note row is blank."""
    from sheets.ledger_sync import _INT_FIELDS
    from web.ledger_reader import HEADER_TO_FIELD, NUMERIC_COLUMNS

    header = list(snapshot.header)
    size = max((r.row_number for r in snapshot.rows), default=1)
    formatted: list[list] = [header] + [[""] * len(header) for _ in range(size - 1)]
    unformatted: list[list] = [header] + [[""] * len(header) for _ in range(size - 1)]
    for row in snapshot.rows:
        text_line = formatted[row.row_number - 1]
        typed_line = unformatted[row.row_number - 1]
        for i, name in enumerate(header):
            fld = HEADER_TO_FIELD.get(name)
            if fld is None:
                continue
            if fld == "cogs":
                value = row.cogs
            elif fld == "total_profit":
                value = row.profit
            elif fld in NUMERIC_COLUMNS:
                value = row.number(fld)
                if value is None and row.text(fld):
                    value = row.text(fld)  # text in a numeric column ("*"): the audit should see it
                elif fld in _INT_FIELDS and isinstance(value, float) and value.is_integer():
                    value = int(value)  # a count is stored as an int, as the Sheet and the db store it
            else:
                value = row.text(fld)
            typed_line[i] = "" if value is None else value
            text_line[i] = "" if value is None else (
                str(int(value)) if isinstance(value, float) and value.is_integer() else str(value))
    return Grids(formatted=formatted, unformatted=unformatted, formula=unformatted,
                 meta={"worksheet": snapshot.source, "rows": size - 1, "cols": len(header),
                       "merges": None})


def audit_grids(reader, snapshot) -> Grids:
    """The grids to audit for the dashboard's backend: the ledger file through the read-only
    worksheet adapter, or the CSV snapshot's rows."""
    if getattr(reader, "backend", "") == "db":
        from ledger_db.worksheet import DbWorksheet

        worksheet = DbWorksheet(reader.db, read_only=True)
        return read_grids(worksheet, worksheet.title)
    return grids_from_snapshot(snapshot)


def audit_key(reader, snapshot) -> tuple:
    """What the cached report is keyed on: a digest of the rows the dashboard just loaded, so a
    cell edit or a sync rebuilds the report and a page load plus its htmx swap share one. (Not the
    file's timestamp: NTFS updates it lazily.)"""
    digest = hashlib.blake2b(digest_size=16)
    for row in snapshot.rows:
        digest.update(str(row.row_number).encode())
        for name, value in row.cells.items():
            digest.update(b"" + name.encode() + b"=" + str(value).encode("utf-8", "replace"))
        digest.update(b"")
    return (getattr(reader, "backend", ""), snapshot.source, len(snapshot.rows), digest.hexdigest())


class AuditCache:
    """One report at a time, rebuilt when the ledger changes (a page load and its htmx swap
    audit once, not twice)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._key: tuple | None = None
        self._report: AuditReport | None = None

    def get(self, key: tuple, build: Callable[[], AuditReport]) -> AuditReport:
        with self._lock:
            if self._report is None or key != self._key:
                self._report = build()
                self._key = key
            return self._report

