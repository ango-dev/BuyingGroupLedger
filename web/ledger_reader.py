"""The ONE way the web dashboard reads the ledger. Two backends, one shape, no write path.

    SnapshotReader  a CSV under data/sheet_backup_*.csv (the backups every --apply script writes),
                    newest by default. Development and tests run on this: no credentials, no network.
    SheetReader     the live Sheet through scripts.audit_sheet.open_worksheet_readonly -- the
                    spreadsheets.READONLY scope, so "read-only" is a capability Google enforces, not a
                    promise this module makes -- with an in-process cache (default 300 s) so a page
                    reload never becomes a Sheets API call.

Both hand back the same `Snapshot`: a list of `LedgerRow`s keyed by FIELDNAMES, read BY HEADER NAME
rather than by position. That matters more than it looks: a backup written before a column moved
(2026-09-10 moved Package ID beside Card Last 4) still has every value under the right name, and the
dashboard must never guess a column from its position the way the upsert legitimately does.

NOTHING HERE RE-TYPES THE LEDGER'S SEMANTICS. Column names come from models.order.FIELDNAMES and
sheets.ledger_sync.HEADER; the status vocabulary from models.order; money parsing from
sheets.ledger_sync._parse_display_number (the same parser the upsert runs on a formatted read); the
gift-card tag from config.warehouses. The one piece of arithmetic done here -- COGS and Total Profit
for a row whose cell holds a formula literal instead of a number, which is what a CSV backup carries
-- mirrors sheets.ledger_sync._cogs_formula / _profit_formula and is pinned against them by
tests/test_web.py so the two cannot drift apart silently.
"""

from __future__ import annotations

import csv
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from config.warehouses import is_deliberately_unrouted
from models.order import FIELDNAMES, MONEY_FREE_STATUSES
from sheets.ledger_sync import HEADER, _NUMERIC_FIELDS, _parse_checkbox, _parse_display_number

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
#: What every --apply script names its pre-write backup (scripts/sort_ledger.py and friends).
SNAPSHOT_GLOB = "sheet_backup_*.csv"

HEADER_TO_FIELD = dict(zip(HEADER, FIELDNAMES))
FIELD_TO_HEADER = dict(zip(FIELDNAMES, HEADER))

#: A payout is SETTLED when it carries its date or a buying-group outcome status -- never on the
#: amount alone, because since 2026-09-11 the sync fills Payout Amount with BFMR's COMMITTED price
#: while the package is still open (docs/data-model.md, "Payout Amount holds two kinds of number").
#: The same rule scripts/audit_sheet.check_cogs_inputs_complete counts a row settled by.
SETTLED_STATUSES = ("paid", "return")
#: The dashboard's OPEN rows: the group has not paid yet. Delivered counts --
#: the scrapers' TERMINAL_STATUSES is a different question (whether a row is ever re-read).
OPEN_STATUSES = ("ordered", "shipped", "delivered")


def _is_formula(text: str) -> bool:
    return text.startswith("=")


@dataclass(frozen=True)
class LedgerRow:
    """One ledger row, cells keyed by FIELDNAMES, values as the display text they were read as."""

    cells: dict[str, str]
    #: 1-based sheet row number (the header is row 1), for cross-referencing audit output.
    row_number: int
    #: The STORED value of each numeric cell (an UNFORMATTED read), when the source has one. A
    #: formatted read shows "$1,299.99" rounded to the cent while the cell may hold 1299.9875;
    #: summing displayed cents drifts from the sheet's own SUM by a few cents, which is exactly
    #: the kind of number a user compares. A CSV backup has no such grid: the text is parsed.
    numbers: dict[str, float | None] = field(default_factory=dict)

    # --- raw access -------------------------------------------------------------------------------
    def text(self, name: str) -> str:
        return str(self.cells.get(name, "") or "").strip()

    def number(self, name: str) -> float | None:
        """The cell as a number: the stored value when the source read one, else the display text
        through the upsert's own display-format parser. None when blank, non-numeric, or a formula
        LITERAL (a CSV backup stores "=IF(...)" for COGS / Total Profit; the parser would otherwise
        scrape digits out of the cell references)."""
        if name in self.numbers:
            return self.numbers[name]
        text = self.text(name)
        if not text or _is_formula(text):
            return None
        value = _parse_display_number(text)
        return float(value) if value is not None else None

    # --- identity ---------------------------------------------------------------------------------
    @property
    def order_id(self) -> str:
        return self.text("order_id")

    @property
    def order_date(self) -> str:
        return self.text("order_date")

    @property
    def item_name(self) -> str:
        return self.text("item_name")

    @property
    def shipment(self) -> str:
        return self.text("shipment")

    @property
    def retailer(self) -> str:
        return self.text("retailer")

    @property
    def profile(self) -> str:
        return self.text("profile_label")

    @property
    def buying_group(self) -> str:
        return self.text("buying_group")

    @property
    def status(self) -> str:
        return self.text("status").lower()

    @property
    def tracking_number(self) -> str:
        return self.text("tracking_number")

    @property
    def tracking_submitted(self) -> bool:
        return _parse_checkbox(self.text("tracking_submitted")) is True

    # --- lifecycle --------------------------------------------------------------------------------
    @property
    def is_open(self) -> bool:
        """Still waiting on the buying group: ordered, shipped OR delivered. This is the DASHBOARD's notion of open -- the money is
        not in yet -- and deliberately wider than models.order.TERMINAL_STATUSES, which is the
        scrapers' notion (a delivered row is never re-read). A gift-card row is never open: it is
        routed nowhere and nothing is pending on it."""
        return self.status in OPEN_STATUSES and not self.is_gift_card

    @property
    def is_money_free(self) -> bool:
        """cancelled / superseded: every amount cell is blank by design."""
        return self.status in MONEY_FREE_STATUSES

    @property
    def is_gift_card(self) -> bool:
        return is_deliberately_unrouted(self.buying_group)

    # --- money ------------------------------------------------------------------------------------
    @property
    def total_cost(self) -> float | None:
        return self.number("total_cost")

    @property
    def payout_amount(self) -> float | None:
        return self.number("payout_amount")

    @property
    def payout_date(self) -> str:
        return self.text("payout_date")

    @property
    def is_settled(self) -> bool:
        """The group's outcome is IN: a Payout Amount cell (any amount, $0.00 included -- a return
        or clawback that paid nothing is a settled LOSS, and the sheet's Total Profit shows it)
        together with its date, or a paid/return status (MOD's paid rows carry no date). See
        SETTLED_STATUSES. Found live: four $0.00 settlements (-822.39 of real losses)
        were being left out of realized profit, so the dashboard disagreed with the sheet's SUM."""
        return self.payout_amount is not None and (
            bool(self.payout_date) or self.status in SETTLED_STATUSES
        )

    @property
    def is_committed(self) -> bool:
        """A projected payout: the amount BFMR has committed to, not yet paid (a non-zero amount,
        date blank, status not a buying-group outcome -- the allocator never writes a zero
        commitment). Total Profit on such a row is PROJECTED."""
        return (
            bool(self.payout_amount)
            and not self.is_settled
            and not self.is_money_free
        )

    @property
    def is_unpaid(self) -> bool:
        """FLOATING money: the row carries cost and no settled payout -- open, committed, or anything
        else without a Payout Date / paid status. A gift-card row is not floating: it is routed
        nowhere and no payout is ever expected for it. Note this is NOT spend minus paid out:
        that gap also holds the settled rows' cost minus their payout (profit here comes from
        cashback, so a settled row is usually paid a little less than it cost)."""
        return not self.is_money_free and not self.is_settled and not self.is_gift_card

    @property
    def payout_state(self) -> str:
        """"settled" | "committed" | "none" -- the three states a Payout Amount cell can be in."""
        if self.is_settled:
            return "settled"
        if self.is_committed:
            return "committed"
        return "none"

    @property
    def cogs(self) -> float | None:
        """The COGS cell when it holds a number (a live formatted read), else the formula's
        arithmetic recomputed from the row (a CSV backup stores the formula text)."""
        stored = self.number("cogs")
        return stored if stored is not None else cogs_of(self)

    @property
    def profit(self) -> float | None:
        stored = self.number("total_profit")
        return stored if stored is not None else profit_of(self)


def cogs_of(row: LedgerRow) -> float | None:
    """sheets.ledger_sync._cogs_formula, in Python:

        COGS = (Total Cost - Return Qty x Cost Per Item - Gift Card + Shipping + Sales Tax
                - Rewards Used) x (1 - Cashback Rate) + Rewards Used

    Blank cells count as 0, a cancelled (money-free) row reports nothing, and so does a row with no
    Total Cost. tests/test_web.py pins the set of cells this reads against the formula's own
    references, so a formula change cannot leave this behind unnoticed.
    """
    if row.is_money_free:
        return None
    cost = row.number("total_cost")
    if cost is None:
        return None
    returned = row.number("return_quantity") or 0.0
    unit = row.number("cost_per_item") or 0.0
    gift = row.number("gift_card") or 0.0
    shipping = row.number("shipping") or 0.0
    tax = row.number("sales_tax") or 0.0
    rewards = row.number("rewards_used") or 0.0
    rate = row.number("cashback_rate") or 0.0
    return round((cost - returned * unit - gift + shipping + tax - rewards) * (1 - rate) + rewards, 2)


def profit_of(row: LedgerRow) -> float | None:
    """sheets.ledger_sync._profit_formula, in Python: Payout Amount - COGS - Insurance, blank until
    a payout exists and blank on a money-free row."""
    if row.is_money_free:
        return None
    payout = row.payout_amount
    if payout is None:
        return None
    return round(payout - (row.cogs or 0.0) - (row.number("insurance") or 0.0), 2)


@dataclass
class Snapshot:
    """What a reader hands the views: the rows plus enough about where they came from for /health."""

    rows: list[LedgerRow]
    header: list[str]
    backend: str  # "snapshot" | "sheet"
    source: str  # the file path, or "<spreadsheet> / <worksheet>"
    loaded_at: datetime
    #: Data rows skipped because their Order ID was blank -- note/spacer rows below the block, which
    #: the upsert ignores too (docs/data-model.md, "Adding a row by hand").
    skipped_rows: int = 0
    meta: dict = field(default_factory=dict)

    @property
    def schema_matches(self) -> bool:
        return list(self.header) == list(HEADER)

    @property
    def missing_columns(self) -> tuple[str, ...]:
        return tuple(name for name in HEADER if name not in self.header)

    @property
    def extra_columns(self) -> tuple[str, ...]:
        return tuple(name for name in self.header if name not in HEADER_TO_FIELD)

    def by_order(self, order_id: str) -> list[LedgerRow]:
        return [row for row in self.rows if row.order_id == order_id]


#: The columns whose STORED value a live read carries over (see LedgerRow.numbers).
NUMERIC_COLUMNS = frozenset(_NUMERIC_FIELDS) | {"cogs", "total_profit"}


def rows_from_grid(grid: list[list], *, backend: str, source: str,
                   loaded_at: datetime | None = None,
                   unformatted: list[list] | None = None) -> Snapshot:
    """Turn a header + rows grid (a CSV's rows, or a worksheet's get_values) into a Snapshot.

    Cells are found BY HEADER NAME. A column the header does not carry reads as blank; a column the
    schema does not know is ignored (and reported through Snapshot.extra_columns). Short rows read
    as blank past their end -- the same rule as the upsert and the audit. `unformatted`, when a
    live read supplies it, gives every numeric cell its stored value (LedgerRow.numbers).
    """
    if not grid:
        raise ValueError(f"{source}: empty -- no header row")
    header = [str(cell).strip() for cell in grid[0]]
    positions = {HEADER_TO_FIELD[name]: index for index, name in enumerate(header)
                 if name in HEADER_TO_FIELD}
    rows: list[LedgerRow] = []
    skipped = 0
    for offset, raw in enumerate(grid[1:], start=2):
        cells = {
            fld: (str(raw[idx]) if idx < len(raw) and raw[idx] is not None else "")
            for fld, idx in positions.items()
        }
        if not cells.get("order_id", "").strip():
            skipped += 1
            continue
        numbers: dict[str, float | None] = {}
        if unformatted is not None and offset - 1 < len(unformatted):
            stored = unformatted[offset - 1]
            for fld, idx in positions.items():
                if fld not in NUMERIC_COLUMNS:
                    continue
                value = stored[idx] if idx < len(stored) else ""
                if isinstance(value, bool) or value is None or value == "":
                    numbers[fld] = None
                elif isinstance(value, (int, float)):
                    numbers[fld] = float(value)
                else:  # text in a numeric column ("*" on an unresolved split): parse like display
                    parsed = _parse_display_number(str(value))
                    numbers[fld] = float(parsed) if parsed is not None else None
        rows.append(LedgerRow(cells=cells, row_number=offset, numbers=numbers))
    return Snapshot(
        rows=rows, header=header, backend=backend, source=source,
        loaded_at=loaded_at or datetime.now(timezone.utc), skipped_rows=skipped,
    )


class LedgerReader(Protocol):
    backend: str

    def load(self, force: bool = False) -> Snapshot: ...

    def health(self) -> dict: ...


# --------------------------------------------------------------------------------------------------
# Backend (a): a CSV backup
# --------------------------------------------------------------------------------------------------


def newest_snapshot(data_dir: Path = DATA_DIR) -> Path:
    """The newest data/sheet_backup_*.csv. The timestamp is in the name, so name order IS time
    order; mtime breaks a tie. A missing directory or no backups at all is a loud error."""
    candidates = sorted(Path(data_dir).glob(SNAPSHOT_GLOB),
                        key=lambda p: (p.name, p.stat().st_mtime))
    if not candidates:
        raise FileNotFoundError(
            f"No {SNAPSHOT_GLOB} under {data_dir}. Any --apply script writes one (e.g. "
            "`python -m scripts.sort_ledger --apply`), or point web.snapshot_path / "
            "WEB_SNAPSHOT_PATH at a CSV with the ledger's columns."
        )
    return candidates[-1]


class SnapshotReader:
    """Read a CSV backup. `path=None` means the newest backup, re-resolved on every load so a fresh
    backup is picked up without a restart; the file is small and re-read each time."""

    backend = "snapshot"

    def __init__(self, path: Path | str | None = None, data_dir: Path = DATA_DIR):
        self._explicit = Path(path) if path else None
        if self._explicit is not None and not self._explicit.is_absolute():
            self._explicit = ROOT / self._explicit
        self._data_dir = Path(data_dir)
        self._last: Snapshot | None = None

    def resolve(self) -> Path:
        return self._explicit if self._explicit is not None else newest_snapshot(self._data_dir)

    def load(self, force: bool = False) -> Snapshot:
        path = self.resolve()
        with path.open(newline="", encoding="utf-8-sig") as handle:
            grid = list(csv.reader(handle))
        snapshot = rows_from_grid(grid, backend=self.backend, source=str(path))
        snapshot.meta = {
            "path": str(path),
            "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        }
        self._last = snapshot
        return snapshot

    def health(self) -> dict:
        try:
            path = self.resolve()
            info = {
                "snapshot_path": str(path),
                "snapshot_modified_at": datetime.fromtimestamp(
                    path.stat().st_mtime, timezone.utc).isoformat(),
            }
        except FileNotFoundError as exc:
            info = {"snapshot_path": None, "error": str(exc)}
        return {"backend": self.backend, **info}


# --------------------------------------------------------------------------------------------------
# Backend (b): the live Sheet, read-only, cached
# --------------------------------------------------------------------------------------------------


def _open_readonly():
    """scripts.audit_sheet.open_worksheet_readonly -- the READONLY scope. Imported lazily so the
    snapshot backend never touches gspread's auth path."""
    from scripts.audit_sheet import open_worksheet_readonly

    return open_worksheet_readonly()


class SheetReader:
    """The live worksheet through the read-only scope, cached for `ttl_seconds`.

    One FORMATTED read per refresh (`get_values(value_render_option=formatted)`): every cell comes
    back as the text the sheet displays, which is exactly what the upsert keys rows on and what
    _parse_display_number was written for, and a formula cell shows its RESULT rather than its
    text. The worksheet is re-opened on every refresh rather than held, so a revoked credential or
    a renamed tab surfaces at the next refresh instead of on a stale handle.

    `opener` and `clock` are injection points for tests; the defaults are the real ones.
    """

    backend = "sheet"

    def __init__(self, ttl_seconds: float = 300.0,
                 opener: Callable[[], tuple] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.ttl_seconds = float(ttl_seconds)
        self._opener = opener or _open_readonly
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: Snapshot | None = None
        self._cached_at: float | None = None

    def cache_age(self) -> float | None:
        if self._cached_at is None:
            return None
        return max(0.0, self._clock() - self._cached_at)

    def load(self, force: bool = False) -> Snapshot:
        with self._lock:
            age = self.cache_age()
            if not force and self._cached is not None and age is not None and age < self.ttl_seconds:
                return self._cached
            from gspread.utils import ValueRenderOption

            worksheet, spreadsheet_title = self._opener()
            grid = worksheet.get_values(value_render_option=ValueRenderOption.formatted)
            # A second, UNFORMATTED read for the numeric cells' stored values (LedgerRow.numbers):
            # the displayed cents drift from the sheet's own SUM. Dates and keys still come from
            # the formatted grid, so a date column formatted as a Date can never turn into a serial.
            stored = worksheet.get_values(value_render_option=ValueRenderOption.unformatted)
            source = f"{spreadsheet_title} / {getattr(worksheet, 'title', '')}".strip(" /")
            snapshot = rows_from_grid(grid, backend=self.backend, source=source, unformatted=stored)
            snapshot.meta = {"spreadsheet": spreadsheet_title,
                             "worksheet": getattr(worksheet, "title", "")}
            self._cached, self._cached_at = snapshot, self._clock()
            return snapshot

    def health(self) -> dict:
        age = self.cache_age()
        return {
            "backend": self.backend,
            "sheet_cache_ttl_seconds": self.ttl_seconds,
            "sheet_cache_age_seconds": None if age is None else round(age, 1),
            "sheet_cache_loaded_at": (self._cached.loaded_at.isoformat()
                                      if self._cached is not None else None),
            "source": self._cached.source if self._cached is not None else None,
        }


# --------------------------------------------------------------------------------------------------
# Backend (c): the SQLite copy, refreshed from an upstream reader on the cache interval
# --------------------------------------------------------------------------------------------------


def _as_text(value) -> str:
    """A typed SQLite value back to the display text a LedgerRow carries: 1 -> "1", 1299.0 ->
    "1299", 0.04 -> "0.04", a bool column's 1 -> "TRUE" (handled by the caller), None -> ""."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class DbReader:
    """Serve the ledger from data/ledger.sqlite3 (ledger_db), refreshing it from `upstream` -- the
    read-only Sheet, or a CSV snapshot -- whenever the copy is older than `ttl_seconds`.

    The DB persists across restarts, so a page loads instantly on a cold start and the Sheet is
    read at most once per interval however many pages are opened. With no upstream the DB is served
    as it is (a host that only ever mirrors by script). The mirror is the ONLY writer of the file;
    it never touches the Sheet.
    """

    backend = "db"

    def __init__(self, db, upstream: LedgerReader | None = None, ttl_seconds: float = 300.0,
                 clock: Callable[[], float] = time.monotonic):
        from ledger_db.store import LedgerDb

        self.db = db if isinstance(db, LedgerDb) else LedgerDb(db)
        self.upstream = upstream
        self.ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._mirrored_at: float | None = None
        self.last_mirror: dict | None = None

    def mirror_age(self) -> float | None:
        if self._mirrored_at is None:
            return None
        return max(0.0, self._clock() - self._mirrored_at)

    def refresh(self, force: bool = False) -> bool:
        """Mirror from upstream if due. Returns True when a mirror ran."""
        if self.upstream is None:
            return False
        age = self.mirror_age()
        due = force or age is None or age >= self.ttl_seconds
        if not due:
            return False
        from ledger_db.mirror import mirror_snapshot

        snapshot = self.upstream.load(force=True)
        self.last_mirror = mirror_snapshot(snapshot, self.db)
        self._mirrored_at = self._clock()
        return True

    def load(self, force: bool = False) -> Snapshot:
        with self._lock:
            # On a cold start with a populated file, serve it as it is until the interval passes:
            # the copy's own age (from its mirror_runs log) decides, not this process's uptime.
            if self._mirrored_at is None and not force and self.upstream is not None:
                last = self.db.last_mirror() if self.db.path.is_file() else None
                if last is not None:
                    try:
                        at = datetime.fromisoformat(last["at"])
                        age = (datetime.now(timezone.utc) - at).total_seconds()
                        if 0 <= age < self.ttl_seconds:
                            self._mirrored_at = self._clock() - age
                    except (KeyError, ValueError):
                        pass
            self.refresh(force=force)
            records = self.db.fetch_rows()
            rows = []
            for rec in records:
                cells = {}
                for field in FIELDNAMES:
                    value = rec.get(field)
                    if field == "tracking_submitted" and value is not None and value != "":
                        cells[field] = "TRUE" if int(value) else "FALSE"
                    else:
                        cells[field] = _as_text(value)
                rows.append(LedgerRow(cells=cells, row_number=int(rec.get("sheet_row") or 0)))
            last = self.db.last_mirror() if self.db.path.is_file() else None
            snapshot = Snapshot(
                rows=rows, header=list(HEADER), backend=self.backend,
                source=f"{self.db.path}" + (f" (mirrored from {last['source']})" if last else ""),
                loaded_at=datetime.now(timezone.utc),
            )
            snapshot.meta = {"db_path": str(self.db.path), "last_mirror": last}
            return snapshot

    def health(self) -> dict:
        info = {"backend": self.backend, "db_mirror_ttl_seconds": self.ttl_seconds,
                "db_mirror_upstream": self.upstream.backend if self.upstream else None,
                **self.db.health()}
        age = self.mirror_age()
        info["db_mirror_age_seconds"] = None if age is None else round(age, 1)
        return info


# --------------------------------------------------------------------------------------------------
# Choosing a backend
# --------------------------------------------------------------------------------------------------

BACKENDS = ("snapshot", "sheet", "db")


def reader_from_settings(settings, *, source: str | None = None,
                         snapshot_path: str | None = None) -> LedgerReader:
    """The backend `config.json`'s `web` section (or WEB_LEDGER_SOURCE) asks for. Explicit
    arguments -- the CLI flags of `python -m web` -- win over both. Anything but the known names
    is refused loudly rather than defaulting: a typo must not quietly serve stale data.

    `db` mirrors from the live Sheet, or from a snapshot when one is named (development: a DB
    filled from a CSV, no credentials)."""
    chosen = (source or settings.web_ledger_source or "snapshot").strip().lower()
    snapshot_path = snapshot_path or settings.web_snapshot_path or None
    if settings.ledger_is_db() and source is None:
        # The database IS the ledger: serve it as it is, and never mirror anything INTO it (a
        # mirror from the deprecated Sheet would overwrite the ledger). web.ledger_source is
        # moot under this flag; an explicit CLI --source still wins for development.
        return DbReader(settings.ledger_db_path, upstream=None,
                        ttl_seconds=settings.web_sheet_cache_ttl_seconds)
    if chosen == "snapshot":
        return SnapshotReader(snapshot_path)
    if chosen == "sheet":
        return SheetReader(ttl_seconds=settings.web_sheet_cache_ttl_seconds)
    if chosen == "db":
        upstream = (SnapshotReader(snapshot_path) if snapshot_path
                    else SheetReader(ttl_seconds=0))
        return DbReader(settings.ledger_db_path, upstream=upstream,
                        ttl_seconds=settings.web_sheet_cache_ttl_seconds)
    raise ValueError(
        f"web.ledger_source / WEB_LEDGER_SOURCE must be one of {BACKENDS}, not {chosen!r}"
    )
