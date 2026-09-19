"""The ONE way the web dashboard reads the ledger. Two backends, one shape, no write path.

    DbReader        the ledger itself, data/ledger.sqlite3 (ledger_db), read fresh on every load.
    SnapshotReader  a CSV under data/ledger_backup_*.csv (the backups every --apply script writes),
                    newest by default. Development and tests run on this: no credentials, no network.

Both hand back the same `Snapshot`: a list of `LedgerRow`s keyed by FIELDNAMES, read BY HEADER NAME
rather than by position. That matters more than it looks: a backup written before a column moved
(2026-09-10 moved Package ID beside Card Last 4) still has every value under the right name, and the
dashboard must never guess a column from its position the way the upsert legitimately does.

NOTHING HERE RE-TYPES THE LEDGER'S SEMANTICS. Column names come from models.order.FIELDNAMES and
ledger.sync.HEADER; the status vocabulary from models.order; money parsing from
ledger.sync._parse_display_number (the same parser the upsert runs on a formatted read); the
gift-card tag from config.warehouses. The one piece of arithmetic done here -- COGS and Total Profit
for a row whose cell holds a formula literal instead of a number, which is what a CSV backup carries
-- mirrors ledger.sync._cogs_formula / _profit_formula and is pinned against them by
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
from ledger.sync import HEADER, _NUMERIC_FIELDS, _parse_checkbox, _parse_display_number

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
#: What every --apply script names its pre-write backup (scripts/sort_ledger.py and friends).
SNAPSHOT_GLOB = "ledger_backup_*.csv"

#: Headings a ledger may still carry from before a rename; read as the field they became.
LEGACY_HEADERS = {"Payout Amount": "payout_amount"}  # -> "Actual Payout", 2026-09-18
HEADER_TO_FIELD = {**LEGACY_HEADERS, **dict(zip(HEADER, FIELDNAMES))}
FIELD_TO_HEADER = dict(zip(FIELDNAMES, HEADER))

#: A payout is SETTLED when it carries its date or a buying-group outcome status -- never on the
#: amount alone, because since 2026-09-11 the sync fills Actual Payout with BFMR's COMMITTED price
#: while the package is still open (docs/data-model.md, "Actual Payout holds two kinds of number").
#: The same rule scripts/audit_ledger.check_cogs_inputs_complete counts a row settled by.
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
    #: 1-based row number (the header is row 1), for cross-referencing audit output.
    row_number: int
    #: The STORED value of each numeric cell (an UNFORMATTED read), when the source has one. A
    #: formatted read shows "$1,299.99" rounded to the cent while the cell may hold 1299.9875;
    #: summing displayed cents drifts from the ledger's own SUM by a few cents, which is exactly
    #: the kind of number a user compares. A CSV backup has no such grid: the text is parsed.
    numbers: dict[str, float | None] = field(default_factory=dict)
    #: Fields the user typed by hand on the dashboard (ledger_db/hand_edits): a run keeps them.
    hand_edited: frozenset = frozenset()

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
    def expected_payout(self) -> float | None:
        """What the buying group COMMITTED to pay for this row (Expected Payout, filled by the
        sync from BFMR's tracker price; blank for MOD, which publishes none)."""
        return self.number("expected_payout")

    @property
    def is_settled(self) -> bool:
        """The group's outcome is IN: a Actual Payout cell (any amount, $0.00 included -- a return
        or clawback that paid nothing is a settled LOSS, and the ledger's Total Profit shows it)
        together with its date, or a paid/return status (MOD's paid rows carry no date). See
        SETTLED_STATUSES. Found live: four $0.00 settlements (-822.39 of real losses)
        were being left out of realized profit, so the dashboard disagreed with the ledger's SUM."""
        return self.payout_amount is not None and (
            bool(self.payout_date) or self.status in SETTLED_STATUSES
        )

    @property
    def is_committed(self) -> bool:
        """A projected payout: the group has committed to an amount (Expected Payout, since
        2026-09-18) and has not paid yet. A ledger from before the column (a CSV backup)
        carried the commitment IN Actual Payout with a blank date, and that
        legacy shape still reads as committed here. A zero is never a commitment (the allocator
        never writes one)."""
        if self.is_money_free or self.is_settled:
            return False
        return bool(self.expected_payout) or bool(self.payout_amount)

    @property
    def projected_payout(self) -> float | None:
        """The committed figure a projected profit is computed from, or None when not committed."""
        if not self.is_committed:
            return None
        return self.expected_payout if self.expected_payout else self.payout_amount

    @property
    def projected_profit(self) -> float | None:
        """Expected Payout - COGS - Insurance on a committed row (the formula's arithmetic with
        the commitment in place of the payout); None otherwise."""
        payout = self.projected_payout
        if payout is None:
            return None
        return round(payout - (self.cogs or 0.0) - (self.number("insurance") or 0.0), 2)

    @property
    def profit_or_projected(self) -> float | None:
        """Total Profit when the row has one (a payout is in), else the projected profit."""
        profit = self.profit
        return profit if profit is not None else self.projected_profit

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
        """"settled" | "committed" | "none" -- the three states a Actual Payout cell can be in."""
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
    """ledger.sync._cogs_formula, in Python:

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
    """ledger.sync._profit_formula, in Python: Actual Payout - COGS - Insurance, blank until
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
    backend: str  # "snapshot" | "db"
    source: str  # the file path
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
    """The newest data/ledger_backup_*.csv. The timestamp is in the name, so name order IS time
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
# Backend (b): the ledger file
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
    """Serve the ledger from data/ledger.sqlite3 (ledger_db). Every load reads the file: it is
    local and a few hundred rows, and a scheduled run or a dashboard edit may have changed it
    since the last page. `ttl_seconds` is reported by health() only.
    """

    backend = "db"

    def __init__(self, db, ttl_seconds: float = 300.0,
                 clock: Callable[[], float] = time.monotonic):
        from ledger_db.store import LedgerDb

        self.db = db if isinstance(db, LedgerDb) else LedgerDb(db)
        self.ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._loaded_at: float | None = None

    def refresh(self, force: bool = False) -> bool:
        """Nothing to pull from anywhere: the file IS the ledger, and load() reads it fresh."""
        return False

    def load(self, force: bool = False) -> Snapshot:
        with self._lock:
            records = self.db.fetch_rows()
            try:
                from ledger_db.hand_edits import protected

                hand = protected(self.db)
            except Exception:  # noqa: BLE001
                hand = {}
            rows = []
            for rec in records:
                cells = {}
                for field in FIELDNAMES:
                    value = rec.get(field)
                    if field == "tracking_submitted" and value is not None and value != "":
                        cells[field] = "TRUE" if int(value) else "FALSE"
                    else:
                        cells[field] = _as_text(value)
                key = tuple(cells.get(f, "").strip() for f in ("order_id", "order_date", "item_name", "shipment"))
                rows.append(LedgerRow(cells=cells, row_number=int(rec.get("sheet_row") or 0),
                                      hand_edited=frozenset(hand.get(key, ()))))
            self._loaded_at = self._clock()
            snapshot = Snapshot(
                rows=rows, header=list(HEADER), backend=self.backend,
                source=str(self.db.path), loaded_at=datetime.now(timezone.utc),
            )
            snapshot.meta = {"db_path": str(self.db.path)}
            return snapshot

    def health(self) -> dict:
        return {"backend": self.backend, "db_cache_ttl_seconds": self.ttl_seconds,
                **self.db.health()}


# --------------------------------------------------------------------------------------------------
# Choosing a backend
# --------------------------------------------------------------------------------------------------

BACKENDS = ("db", "snapshot")


def reader_from_settings(settings, *, source: str | None = None,
                         snapshot_path: str | None = None) -> LedgerReader:
    """The backend `config.json`'s `web` section (or WEB_LEDGER_SOURCE) asks for: `db` (the
    ledger file, the default) or `snapshot` (a CSV export, for offline work). Explicit arguments
    -- the CLI flags of `python -m web` -- win over both. Anything but the known names is refused
    loudly rather than defaulting: a typo must not quietly serve stale data."""
    chosen = (source or settings.web_ledger_source or "db").strip().lower()
    snapshot_path = snapshot_path or settings.web_snapshot_path or None
    if chosen == "db":
        return DbReader(settings.ledger_db_path, ttl_seconds=settings.web_ledger_cache_ttl_seconds)
    if chosen == "snapshot":
        return SnapshotReader(snapshot_path)
    raise ValueError(
        f"web.ledger_source / WEB_LEDGER_SOURCE must be one of {BACKENDS}, not {chosen!r}"
    )
