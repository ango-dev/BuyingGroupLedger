"""Copy a ledger Snapshot (from any web.ledger_reader backend) into the SQLite file.

Values are coerced with sheets.ledger_sync._coerce -- the SAME function the upsert runs on a
formatted read -- so "$1,299.00" lands as 1299.0, "4%" as 0.04, "TRUE" as 1, exactly as the sync
would have understood them. The two formula columns store the row's computed number (a live read
hands back the result; a CSV backup hands back formula text, which LedgerRow recomputes).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from models.order import FIELDNAMES
from sheets.ledger_sync import _BOOL_FIELDS, _INT_FIELDS, _NUMERIC_FIELDS, _coerce

from ledger_db.store import FORMULA_FIELDS, LedgerDb

if TYPE_CHECKING:  # web.ledger_reader imports this package's DbReader dependency the other way
    from web.ledger_reader import LedgerRow, Snapshot


def typed_record(row: "LedgerRow") -> dict:
    """{field: typed value} for one row, plus its sheet row number."""
    record: dict = {}
    for field in FIELDNAMES:
        if field in FORMULA_FIELDS:
            record[field] = row.cogs if field == "cogs" else row.profit
        elif field in _BOOL_FIELDS:
            text = row.text(field)
            record[field] = _coerce(field, text) if text else None
        elif field in _NUMERIC_FIELDS:
            # The STORED value when the source read one (a live sheet: 1299.9875, not the
            # displayed $1,299.99), else the display text through the upsert's own coercion. A
            # non-number in a numeric column ("*" on an unresolved split) is kept as text.
            number = row.number(field)
            text = row.text(field)
            if number is not None:
                record[field] = int(number) if field in _INT_FIELDS and number.is_integer() else number
            else:
                record[field] = _coerce(field, text) if text else None
        else:
            record[field] = row.text(field)
    record["sheet_row"] = row.row_number
    return record


def mirror_snapshot(snapshot: "Snapshot", db: LedgerDb) -> dict:
    """Replace the DB's rows with the snapshot's. Returns a summary for logs and /health."""
    started = time.perf_counter()
    records = [typed_record(row) for row in snapshot.rows]
    duration_ms = int((time.perf_counter() - started) * 1000)
    written = db.replace_rows(records, backend=snapshot.backend, source=snapshot.source,
                              skipped=snapshot.skipped_rows, header_ok=snapshot.schema_matches,
                              duration_ms=duration_ms)
    return {"rows": written, "skipped": snapshot.skipped_rows, "backend": snapshot.backend,
            "source": snapshot.source, "header_ok": snapshot.schema_matches,
            "db_path": str(db.path)}
