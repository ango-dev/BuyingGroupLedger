"""The SQLite file: schema derived from the ledger's own definitions, and the few operations on it.

Two tables:

    ledger_rows   one row per ledger row, columns = FIELDNAMES in order (typed), plus `sheet_row`
                  (the row's 1-based position in the grid) and `mirrored_at` (when it was last
                  written). PRIMARY KEY is the upsert key. `cogs` / `total_profit` hold NUMBERS
                  (the derived result), never formula text.
    mirror_runs   one row per logged whole-table write: when, from what, how many rows. The newest
                  is what /health reports as the file's age.

A write REPLACES the table in one transaction: the file holds the grid as of that write, nothing
older survives, and a reader never sees a half-written state. The ledger is a few hundred rows;
there is no performance reason for anything cleverer, and "the file is exactly what the grid was
at time T" is the property that makes it trustworthy.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from models.order import FIELDNAMES
from ledger.sync import _BOOL_FIELDS, _INT_FIELDS, _NUMERIC_FIELDS

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "data" / "ledger.sqlite3"

#: The ledger's upsert key (ledger/sync._record_key): Order ID + Order Date + Item Name +
#: Shipment.
KEY_FIELDS = ("order_id", "order_date", "item_name", "shipment")
#: Derived columns whose RESULT is stored here as a number (the adapter computes them).
FORMULA_FIELDS = ("cogs", "total_profit")
#: Bookkeeping columns beyond FIELDNAMES.
EXTRA_COLUMNS = (("sheet_row", "INTEGER"), ("mirrored_at", "TEXT"))


def sql_type(field: str) -> str:
    """The column affinity for a ledger field, from ledger_sync's own field sets."""
    if field in _INT_FIELDS or field in _BOOL_FIELDS:
        return "INTEGER"
    if field in _NUMERIC_FIELDS or field in FORMULA_FIELDS:
        return "REAL"
    return "TEXT"


def columns() -> list[tuple[str, str]]:
    return [(f, sql_type(f)) for f in FIELDNAMES] + list(EXTRA_COLUMNS)


def ledger_rows_ddl() -> str:
    cols = ",\n    ".join(f'"{name}" {kind}' for name, kind in columns())
    key = ", ".join(f'"{k}"' for k in KEY_FIELDS)
    return f'CREATE TABLE "ledger_rows" (\n    {cols},\n    PRIMARY KEY ({key})\n)'


MIRROR_RUNS_DDL = """CREATE TABLE IF NOT EXISTS "mirror_runs" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT,
    "at" TEXT NOT NULL,
    "backend" TEXT NOT NULL,
    "source" TEXT NOT NULL,
    "rows" INTEGER NOT NULL,
    "skipped" INTEGER NOT NULL,
    "header_ok" INTEGER NOT NULL,
    "duration_ms" INTEGER NOT NULL
)"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LedgerDb:
    """One SQLite file. Every method opens its own short-lived connection: the web process and a
    scheduled run may touch the same file, and SQLite's own locking handles that better than a
    shared handle would."""

    def __init__(self, path: Path | str | None = None):
        path = Path(path) if path else DEFAULT_PATH
        self.path = path if path.is_absolute() else ROOT / path

    # --- connection + schema --------------------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_schema(conn)
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        """Create the tables, or MIGRATE ledger_rows when its columns are not FIELDNAMES in order.

        This file IS the ledger, so a schema change must carry every
        row across: when the table merely lacks columns (the schema rule is "append last", so a
        new column is the only legal change) it is rebuilt in the current column order with the
        rows copied over and the new cells NULL -- one transaction, nothing dropped. A table
        holding columns the schema no longer knows is rebuilt only when it is EMPTY; a populated
        one is refused loudly, because dropping it would be the "silently records nothing" outcome
        (restore a backup or migrate by hand). A schema that silently lagged the ledger's would be
        the positional-drift bug in a new coat, so the columns are compared in full, in order.
        """
        conn.execute(MIRROR_RUNS_DDL)
        from ledger_db.hand_edits import HAND_EDITS_DDL

        conn.execute(HAND_EDITS_DDL)
        existing = [r["name"] for r in conn.execute('PRAGMA table_info("ledger_rows")')]
        wanted = [name for name, _ in columns()]
        if existing == wanted:
            return
        conn.execute("BEGIN")
        try:
            if not existing:
                conn.execute(ledger_rows_ddl())
            else:
                unknown = [name for name in existing if name not in wanted]
                rows = int(conn.execute('SELECT COUNT(*) FROM "ledger_rows"').fetchone()[0])
                if unknown and rows:
                    raise RuntimeError(
                        f"{self.path}: ledger_rows holds {rows} row(s) and column(s) the schema no "
                        f"longer knows ({', '.join(unknown)}); refusing to rebuild it. Restore a "
                        "backup or migrate the table by hand.")
                kept = [name for name in wanted if name in existing]
                conn.execute('ALTER TABLE "ledger_rows" RENAME TO "ledger_rows__old"')
                conn.execute(ledger_rows_ddl())
                if kept and rows:
                    cols = ", ".join(f'"{name}"' for name in kept)
                    conn.execute(f'INSERT INTO "ledger_rows" ({cols}) SELECT {cols} FROM "ledger_rows__old"')
                conn.execute('DROP TABLE "ledger_rows__old"')
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # --- writes (the worksheet adapter is the only writer) --------------------------------------------------
    def replace_rows(self, records: list[dict], *, backend: str, source: str, skipped: int = 0,
                     header_ok: bool = True, duration_ms: int = 0, log_run: bool = True) -> int:
        """Replace every ledger row with `records` (dicts keyed by FIELDNAMES + `sheet_row`), in one
        transaction, and log the run in mirror_runs (unless `log_run` is False: the worksheet adapter
        writes the table on every cell write and a log line per write would be noise). Returns
        the number of rows written."""
        names = [name for name, _ in columns()]
        placeholders = ", ".join("?" for _ in names)
        stamp = _now()
        with self.connect() as conn:
            conn.execute("BEGIN")
            try:
                conn.execute('DELETE FROM "ledger_rows"')
                conn.executemany(
                    f'INSERT INTO "ledger_rows" ({", ".join(chr(34) + n + chr(34) for n in names)}) '
                    f"VALUES ({placeholders})",
                    [tuple(_storable(rec.get(n)) for n in names[:-1]) + (stamp,) for rec in records],
                )
                if log_run:
                    conn.execute(
                        'INSERT INTO "mirror_runs" ("at", "backend", "source", "rows", "skipped", '
                        '"header_ok", "duration_ms") VALUES (?, ?, ?, ?, ?, ?, ?)',
                        (stamp, backend, source, len(records), skipped, int(bool(header_ok)),
                         int(duration_ms)),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return len(records)

    # --- reads ----------------------------------------------------------------------------------
    def fetch_rows(self) -> list[dict]:
        """Every ledger row as {field: typed value, "sheet_row": int}, in grid order."""
        with self.connect() as conn:
            cursor = conn.execute('SELECT * FROM "ledger_rows" ORDER BY "sheet_row"')
            return [dict(r) for r in cursor.fetchall()]

    def last_mirror(self) -> dict | None:
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM "mirror_runs" ORDER BY "id" DESC LIMIT 1').fetchone()
            return dict(row) if row else None

    def row_count(self) -> int:
        with self.connect() as conn:
            return int(conn.execute('SELECT COUNT(*) FROM "ledger_rows"').fetchone()[0])

    def health(self) -> dict:
        last = self.last_mirror() if self.path.is_file() else None
        return {
            "db_path": str(self.path),
            "db_exists": self.path.is_file(),
            "db_rows": self.row_count() if self.path.is_file() else 0,
            "db_last_mirror": last,
        }


def _storable(value):
    """SQLite accepts None/int/float/str; a bool becomes 0/1 and "" becomes NULL (blank in the
    grid, blank here -- so a numeric column never holds an empty string)."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    return value
