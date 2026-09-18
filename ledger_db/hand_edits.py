"""Cells the user typed by hand, which no run may overwrite.

HOW A CELL BECOMES PROTECTED. The dashboard's editor (web/ledger_writer.py) is the one place a
hand edit happens under `ledger.backend` = `db`, so every cell it writes -- a cell edit, a bulk
edit, the fields of a hand-added row -- is recorded here, in a `hand_edits` table inside the
ledger database itself, keyed by the row's upsert key and the field. Nothing guesses: a value that
merely differs from what a scraper would write is not a hand edit, the record is.

WHO HONOURS IT. Every writer a scheduled run drives: the scraper upsert's merge
(sheets.ledger_sync._merge_row keeps the existing value of a protected field), the order-level
reproration (shipping / gift card / sales tax / rewards shares), and the buying-group sync's
payout / insurance / expected-payout writes (sync_tracking._drop_protected). The explicit repair
scripts (the backfills, retag, the profit-column refresh) are NOT gated: they are the user
choosing to rewrite a column, and each is a dry run by default.

WHEN IT ENDS. Editing the cell again re-records it (still protected, new value). Deleting the row
from the dashboard forgets the row's protections, so a row re-created by a scrape starts clean.
`python -m scripts.hand_edits --forget <order id> [--field <field>]` releases a cell on purpose.
The Orders page marks a protected cell so the reason a run "did not update it" is visible.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from models.order import FIELDNAMES

KEY_FIELDS = ("order_id", "order_date", "item_name", "shipment")
_KEY_INDEXES = tuple(FIELDNAMES.index(f) for f in KEY_FIELDS)

HAND_EDITS_DDL = """CREATE TABLE IF NOT EXISTS "hand_edits" (
    "order_id" TEXT NOT NULL,
    "order_date" TEXT NOT NULL,
    "item_name" TEXT NOT NULL,
    "shipment" TEXT NOT NULL,
    "field" TEXT NOT NULL,
    "value" TEXT,
    "edited_at" TEXT NOT NULL,
    PRIMARY KEY ("order_id", "order_date", "item_name", "shipment", "field")
)"""

RowKey = tuple[str, str, str, str]


def key_tuple(key: dict) -> RowKey:
    """A writer's key dict -> the normalised tuple every lookup here uses."""
    return tuple(str(key.get(f, "") or "").strip() for f in KEY_FIELDS)  # type: ignore[return-value]


def key_of_row(values: list) -> RowKey:
    """The key of a FIELDNAMES-ordered row (a grid row, the upsert's existing_row)."""
    return tuple(str(values[i] if i < len(values) and values[i] is not None else "").strip()
                 for i in _KEY_INDEXES)  # type: ignore[return-value]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record(db, key: dict | RowKey, field: str, value) -> None:
    """Remember that `field` of the row `key` was typed by hand (its current value, for the
    listing). Idempotent: editing again updates the value and the time."""
    if field not in FIELDNAMES or field in KEY_FIELDS:
        return
    k = key if isinstance(key, tuple) else key_tuple(key)
    with db.connect() as conn:
        conn.execute(
            'INSERT INTO "hand_edits" ("order_id", "order_date", "item_name", "shipment", "field", '
            '"value", "edited_at") VALUES (?, ?, ?, ?, ?, ?, ?) '
            'ON CONFLICT("order_id", "order_date", "item_name", "shipment", "field") '
            'DO UPDATE SET "value" = excluded."value", "edited_at" = excluded."edited_at"',
            (*k, field, "" if value is None else str(value), _now()),
        )
        conn.commit()


def forget(db, key: dict | RowKey, field: str | None = None) -> int:
    """Release one field of a row, or every field of it (`field` None). Returns how many."""
    k = key if isinstance(key, tuple) else key_tuple(key)
    with db.connect() as conn:
        if field is None:
            cursor = conn.execute(
                'DELETE FROM "hand_edits" WHERE "order_id" = ? AND "order_date" = ? AND '
                '"item_name" = ? AND "shipment" = ?', k)
        else:
            cursor = conn.execute(
                'DELETE FROM "hand_edits" WHERE "order_id" = ? AND "order_date" = ? AND '
                '"item_name" = ? AND "shipment" = ? AND "field" = ?', (*k, field))
        conn.commit()
        return cursor.rowcount


def forget_order(db, order_id: str, field: str | None = None) -> int:
    """Release every protected cell of an order's rows (or one field of them)."""
    with db.connect() as conn:
        if field is None:
            cursor = conn.execute('DELETE FROM "hand_edits" WHERE "order_id" = ?', (str(order_id).strip(),))
        else:
            cursor = conn.execute('DELETE FROM "hand_edits" WHERE "order_id" = ? AND "field" = ?',
                                  (str(order_id).strip(), field))
        conn.commit()
        return cursor.rowcount


def entries(db) -> list[dict]:
    """Every protected cell, newest first."""
    with db.connect() as conn:
        cursor = conn.execute('SELECT * FROM "hand_edits" ORDER BY "edited_at" DESC, "order_id"')
        return [dict(r) for r in cursor.fetchall()]


def protected(db) -> dict[RowKey, set[str]]:
    """{row key: the fields a run must not overwrite}."""
    out: dict[RowKey, set[str]] = {}
    for e in entries(db):
        key = (e["order_id"], e["order_date"], e["item_name"], e["shipment"])
        out.setdefault(key, set()).add(e["field"])
    return out


def ledger_db_of(worksheet):
    """The LedgerDb behind a worksheet when it is the SQLite adapter, else None. By TYPE, never by
    attribute: a gspread worksheet (and the tests' Sheet fakes) must not be probed."""
    from ledger_db.worksheet import DbWorksheet

    if isinstance(worksheet, DbWorksheet):
        return worksheet.db
    return None


def protected_fields(worksheet) -> dict[RowKey, set[str]]:
    """The protection map for whatever worksheet a writer holds: the SQLite ledger behind the
    adapter (ledger_db/worksheet.py) has one; a Google Sheet or a test fake has none."""
    db = ledger_db_of(worksheet)
    if db is None:
        return {}
    try:
        return protected(db)
    except Exception:  # noqa: BLE001 -- a missing table on an old file must never stop a run
        return {}


def drop_protected(cells: dict[str, object], fields: Iterable[str], *, by_header: dict[str, str]
                   ) -> tuple[dict[str, object], list[str]]:
    """Split a {column heading: value} write into (what may be written, the headings dropped)."""
    fields = set(fields)
    kept, dropped = {}, []
    for heading, value in cells.items():
        if by_header.get(heading) in fields:
            dropped.append(heading)
        else:
            kept[heading] = value
    return kept, dropped
