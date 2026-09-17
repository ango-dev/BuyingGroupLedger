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
from models.order import FIELDNAMES, MONEY_FREE_STATUSES, TERMINAL_STATUSES
from sheets.ledger_sync import HEADER, _parse_checkbox, _parse_display_number

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


def _is_formula(text: str) -> bool:
    return text.startswith("=")


@dataclass(frozen=True)
class LedgerRow:
    """One ledger row, cells keyed by FIELDNAMES, values as the display text they were read as."""

    cells: dict[str, str]
    #: 1-based sheet row number (the header is row 1), for cross-referencing audit output.
    row_number: int

    # --- raw access -------------------------------------------------------------------------------
    def text(self, name: str) -> str:
        return str(self.cells.get(name, "") or "").strip()

    def number(self, name: str) -> float | None:
        """The cell as a number, through the upsert's own display-format parser. None when blank,
        non-numeric, or a formula LITERAL (a CSV backup stores "=IF(...)" for COGS / Total Profit;
        the parser would otherwise scrape digits out of the cell references)."""
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
        """Still in the retailer lifecycle: not one of models.order.TERMINAL_STATUSES."""
        return self.status not in TERMINAL_STATUSES

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
        """Real money received: a payout WITH its date, or a paid/return status (MOD's paid rows
        carry no date). See SETTLED_STATUSES."""
        return bool(self.payout_amount) and (
            bool(self.payout_date) or self.status in SETTLED_STATUSES
        )

    @property
    def is_committed(self) -> bool:
        """A projected payout: the amount BFMR has committed to, not yet paid (amount present, date
        blank, status not a buying-group outcome). Total Profit on such a row is PROJECTED."""
        return (
            bool(self.payout_amount)
            and not self.is_settled
            and not self.is_money_free
        )

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


def rows_from_grid(grid: list[list], *, backend: str, source: str,
                   loaded_at: datetime | None = None) -> Snapshot:
    """Turn a header + rows grid (a CSV's rows, or a worksheet's get_values) into a Snapshot.

    Cells are found BY HEADER NAME. A column the header does not carry reads as blank; a column the
    schema does not know is ignored (and reported through Snapshot.extra_columns). Short rows read
    as blank past their end -- the same rule as the upsert and the audit.
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
        rows.append(LedgerRow(cells=cells, row_number=offset))
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
            source = f"{spreadsheet_title} / {getattr(worksheet, 'title', '')}".strip(" /")
            snapshot = rows_from_grid(grid, backend=self.backend, source=source)
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
# Choosing a backend
# --------------------------------------------------------------------------------------------------

BACKENDS = ("snapshot", "sheet")


def reader_from_settings(settings, *, source: str | None = None,
                         snapshot_path: str | None = None) -> LedgerReader:
    """The backend `config.json`'s `web` section (or WEB_LEDGER_SOURCE) asks for. Explicit
    arguments -- the CLI flags of `python -m web` -- win over both. Anything but the two known
    names is refused loudly rather than defaulting: a typo must not quietly serve stale data."""
    chosen = (source or settings.web_ledger_source or "snapshot").strip().lower()
    if chosen == "snapshot":
        return SnapshotReader(snapshot_path or settings.web_snapshot_path or None)
    if chosen == "sheet":
        return SheetReader(ttl_seconds=settings.web_sheet_cache_ttl_seconds)
    raise ValueError(
        f"web.ledger_source / WEB_LEDGER_SOURCE must be one of {BACKENDS}, not {chosen!r}"
    )
