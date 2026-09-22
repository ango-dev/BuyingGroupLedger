"""THE ONE PLACE THE DASHBOARD WRITES THE LEDGER, because you asked it to on the Orders page:
one cell, the same cell across selected rows, a new row, or the deletion of selected rows. Nothing
automatic passes through here.

The ledger is the SQLite file (ledger_db/), addressed through the same worksheet face every other
writer uses, so an edit made in the browser lands exactly as if the upsert had written it -- and
is then PROTECTED (ledger_db/hand_edits) from the next run's merge.

WHAT MAY BE EDITED. Everything except: the four UPSERT KEY columns (Order ID, Order Date, Item Name,
Shipment -- changing one turns the row into a different row, and the next re-check appends a
duplicate beside it), the two FORMULA columns (COGS, Total Profit -- the sync re-stamps them, and a
typed number would be overwritten or, worse, freeze a formula), and Last Scraped At (a scraper's
stamp). Status must be one of the ledger's vocabulary; the date columns must be ISO text or blank
(a real Date in the cell would change a key and duplicate the row -- docs/data-model.md).

HOW A CELL IS WRITTEN, mirroring ledger/sync: the row is located BY KEY on a fresh read (the
ledger may have been re-sorted since the page loaded), the current display text must equal what the
page showed (`expected`) or the edit is refused as a conflict, a value goes RAW through the upsert's
own `_coerce` (a number stays a number, a checkbox a boolean, a date plain text), and a blank goes
USER_ENTERED "" -- the one combination that clears the value while keeping the cell's number format.

A NEW ROW lands where the upsert's own append would: after the last occupied row (ignoring the
checkbox column's materialised FALSEs), the grid grown first if needed, blanks sent as None so the
column formats survive, and the two formula cells stamped afterwards -- the same three helpers the
sync uses. The sort moves it into date order on the next append-triggered sort, as with any hand
row. A DELETE is a structural change: rows are removed bottom-up so the located numbers stay valid.

WHILE A SCHEDULED RUN HOLDS THE RUN LOCK, EVERY WRITE IS REFUSED (423). The sync caches row
numbers from its pre-sync snapshot and writes to them; a row deleted or appended underneath it would
put its updates on the wrong rows, and an edit could be overwritten by its merge. main.py's lock
(logs/.run.lock, stale after 3h) is the signal; the page says "try again in a few minutes".
"""

from __future__ import annotations

import re
import time
import logging
from datetime import date
from pathlib import Path
from typing import Callable

from models.order import FIELDNAMES, STATUSES, normalize_shipment
from ledger.sync import (
    HEADER, _BOOL_FIELDS, _COL, _NUMERIC_FIELDS, _blank_to_none, _coerce,
    _ensure_grid_rows, _last_occupied_row, _parse_checkbox, _write_profit_formulas,
)

log = logging.getLogger(__name__)

KEY_FIELDS = ("order_id", "order_date", "item_name", "shipment")
FORMULA_FIELDS = ("cogs", "total_profit")
NEVER_EDITABLE = frozenset(KEY_FIELDS) | frozenset(FORMULA_FIELDS) | {"last_scraped_at"}
EDITABLE_FIELDS = tuple(f for f in FIELDNAMES if f not in NEVER_EDITABLE)
DATE_FIELDS = ("delivery_date", "payout_date", "return_date")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def valid_iso_date(text: str) -> bool:
    """YYYY-MM-DD and a real calendar day: the shape alone let `2026-13-45` and `2026-02-31` onto
    the ledger from the add row and the importer, where every
    date comparison downstream -- the audit's staleness, the tax year, the pager -- trips on it."""
    if not _ISO_DATE.match(text or ""):
        return False
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True
_HEADER_TO_FIELD = dict(zip(HEADER, FIELDNAMES))

#: main.py's run lock. The staleness window is main._LOCK_STALE_SECONDS (3h); restated here rather
#: than imported because importing main configures logging and pulls every scraper in.
LOCK_FILE_NAME = ".run.lock"
LOCK_STALE_SECONDS = 3 * 60 * 60
ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = ROOT / "logs"


class EditError(ValueError):
    """The edit was refused; nothing was written. `.status` is the HTTP status the page uses."""

    status = 400


class ConflictError(EditError):
    """The cell no longer holds what the page showed; reload and try again."""

    status = 409


class RunInProgress(EditError):
    """A scheduled run holds the run lock; writing now could land on the wrong rows."""

    status = 423


def run_in_progress(logs_dir: Path | None = None, *, now: float | None = None) -> bool:
    """Is main.py's run lock live (present and younger than its stale window)?"""
    lock = Path(logs_dir or LOGS_DIR) / LOCK_FILE_NAME
    if not lock.is_file():
        return False
    age = (now if now is not None else time.time()) - lock.stat().st_mtime
    return age < LOCK_STALE_SECONDS


def _open_for_writing():
    """The ledger file behind its worksheet face (the same object ledger_sync._get_worksheet hands
    out; an edit never creates anything)."""
    from config.settings import settings
    from ledger_db.store import LedgerDb
    from ledger_db.worksheet import DbWorksheet

    return DbWorksheet(LedgerDb(settings.ledger_db_path))


def _display(value) -> str:
    """Normalise a display value for the conflict check: whitespace, and the checkbox spellings."""
    text = str(value if value is not None else "").strip()
    parsed = _parse_checkbox(text)
    if parsed is True:
        return "TRUE"
    if parsed is False:
        return "FALSE"
    return text


def validate(field: str, value: str):
    """The coerced value to write, or raise EditError. "" means clear."""
    if field not in EDITABLE_FIELDS:
        raise EditError(f"{field} is not editable"
                        + (" (part of the row's key)" if field in KEY_FIELDS else
                           " (a derived column)" if field in FORMULA_FIELDS else ""))
    text = (value or "").strip()
    if text == "":
        return ""
    if field == "status":
        if text.lower() not in STATUSES:
            raise EditError(f"Status must be one of {', '.join(STATUSES)}")
        return text.lower()
    if field in DATE_FIELDS:
        if not valid_iso_date(text):
            raise EditError(f"{field} must be a real calendar date written as YYYY-MM-DD (text), or blank")
        return text
    if field in _BOOL_FIELDS:
        parsed = _parse_checkbox(text)
        if parsed not in (True, False):
            raise EditError(f"{field} must be TRUE or FALSE")
        return parsed
    if field in _NUMERIC_FIELDS:
        coerced = _coerce(field, text)
        if isinstance(coerced, str):
            raise EditError(f"{field} must be a number")
        return coerced
    return text


def normalize_key(key: dict) -> dict:
    return {
        "order_id": str(key.get("order_id", "")).strip(),
        "order_date": str(key.get("order_date", "")).strip(),
        "item_name": str(key.get("item_name", "")).strip(),
        "shipment": normalize_shipment(str(key.get("shipment", ""))),
    }


def key_label(key: dict) -> str:
    k = normalize_key(key)
    return f"{k['order_id']} / shipment {k['shipment'] or '?'} / {k['item_name'][:40]}"


class _Grid:
    """One fresh formatted read of the ledger, with key lookup."""

    def __init__(self, worksheet):
        from ledger_db.worksheet import ValueRenderOption

        self.rows = worksheet.get_values(value_render_option=ValueRenderOption.formatted)
        if not self.rows or [str(c).strip() for c in self.rows[0]] != list(HEADER):
            raise EditError("the header row is not the ledger's column order; refusing to write")
        self.positions = {f: i for i, f in enumerate(FIELDNAMES)}

    def cell(self, row_number: int, name: str) -> str:
        row = self.rows[row_number - 1]
        i = self.positions[name]
        return str(row[i]).strip() if i < len(row) else ""

    def locate(self, key: dict) -> int:
        """The ONE row carrying this key, or a ConflictError / EditError."""
        wanted = normalize_key(key)
        matches = [
            n for n in range(2, len(self.rows) + 1)
            if self.cell(n, "order_id") == wanted["order_id"]
            and self.cell(n, "order_date") == wanted["order_date"]
            and self.cell(n, "item_name") == wanted["item_name"]
            and normalize_shipment(self.cell(n, "shipment")) == wanted["shipment"]
        ]
        if not matches:
            raise ConflictError(f"{key_label(key)}: no longer on the ledger (re-keyed or deleted); "
                                "reload")
        if len(matches) > 1:
            raise EditError(f"{key_label(key)}: {len(matches)} rows share that key; the audit's "
                            "duplicate_primary_keys check names them -- fix the ledger first")
        return matches[0]


def _protect(worksheet, key: dict, field: str, value, previous=None) -> None:
    """Record a hand edit (ledger_db/hand_edits) with what the cell held before it (`previous`,
    kept from the FIRST hand edit); a test fake has no database and needs no record. Never fails
    the edit that succeeded."""
    from ledger_db.hand_edits import ledger_db_of

    db = ledger_db_of(worksheet)
    if db is None:
        return
    try:
        from ledger_db.hand_edits import record

        record(db, normalize_key(key), field, value, previous=previous)
    except Exception:  # noqa: BLE001
        log.exception("the edit was written but could not be marked as hand-edited")


def _release(worksheet, key: dict, field: str) -> str:
    """The user CLEARED the cell: release it and hand back what a run had written before the hand
    edit. "" when the cell was blank before, or never
    hand-edited."""
    from ledger_db.hand_edits import ledger_db_of

    db = ledger_db_of(worksheet)
    if db is None:
        return ""
    try:
        from ledger_db.hand_edits import forget, previous_value

        before = previous_value(db, normalize_key(key), field)
        forget(db, normalize_key(key), field)
        return before or ""
    except Exception:  # noqa: BLE001
        log.exception("the cell was cleared but its hand-edit mark could not be released")
        return ""


def _text(value) -> str:
    return "" if value is None else str(value)


def _forget(worksheet, key: dict) -> None:
    from ledger_db.hand_edits import ledger_db_of

    db = ledger_db_of(worksheet)
    if db is None:
        return
    try:
        from ledger_db.hand_edits import forget

        forget(db, normalize_key(key))
    except Exception:  # noqa: BLE001
        log.exception("the row was deleted but its hand-edit marks could not be cleared")


class LedgerCellWriter:
    """Write cells, append a row, delete rows. `opener` and `logs_dir` are injection points.
    Every cell it writes under the `db` backend is recorded as hand-edited, so no scheduled run
    overwrites it (ledger_db/hand_edits); a deleted row's marks are cleared."""

    backend = "sheet"

    def __init__(self, opener: Callable[[], object] | None = None,
                 logs_dir: Path | None = None):
        self._opener = opener or _open_for_writing
        self._logs_dir = Path(logs_dir) if logs_dir else LOGS_DIR

    def _guard(self) -> None:
        if run_in_progress(self._logs_dir):
            raise RunInProgress("a scheduled run is in progress (logs/.run.lock); the ledger's row "
                                "numbers are in use -- try again in a few minutes")

    # --- one cell -------------------------------------------------------------------------------
    def write_cell(self, key: dict, field: str, value: str, expected: str | None = None,
                   protect: bool = True) -> dict:
        """Locate the row by `key` ({order_id, order_date, item_name, shipment}), check the cell
        still shows `expected`, write. With `protect` the cell is recorded as hand-edited, so no
        run overwrites it; without it the write is a CORRECTION the runs may overwrite later, and
        any protection the cell had is released. Returns
        {"row_number", "field", "value", "restored"}."""
        coerced = validate(field, value)
        self._guard()
        worksheet = self._opener()
        grid = _Grid(worksheet)
        row_number = grid.locate(key)
        current = grid.cell(row_number, field)
        if expected is not None and _display(current) != _display(expected):
            raise ConflictError(f"the cell now reads {current!r}, not {expected!r}; reload")
        a1 = f"{_COL[field]}{row_number}"
        restored = False
        if coerced == "":
            back = _release(worksheet, key, field)  # the run's value from before the hand edit, or ""
            if back != "":
                coerced, restored = back, True
        if coerced == "":
            worksheet.update(a1, [[""]], value_input_option="USER_ENTERED")
        else:
            worksheet.update(a1, [[coerced]], value_input_option="RAW")
            if not restored and protect:
                _protect(worksheet, key, field, coerced, previous=_text(current))
            elif not restored:
                _release(worksheet, key, field)  # a correction: the run may write here again
        return {"row_number": row_number, "field": field, "value": coerced, "restored": restored}

    # --- the same cell across rows (bulk edit) --------------------------------------------------
    def release_cell(self, key: dict, field: str) -> dict:
        """Drop the hand-edit mark on one cell and KEEP its value: the runs may write it again.
       Returns {"row_number", "field"}."""
        self._guard()
        worksheet = self._opener()
        row_number = _Grid(worksheet).locate(key)
        _release(worksheet, key, field)
        return {"row_number": row_number, "field": field}

    def protect_cell(self, key: dict, field: str) -> dict:
        """Mark one cell's CURRENT value as a hand edit, so no run overwrites it -- the other half
        of the Ctrl+Shift+H toggle. Recorded
        with no previous value: clearing the cell later clears it (and releases it). An empty cell
        is refused: a mark on nothing would only stop the run from filling it."""
        self._guard()
        worksheet = self._opener()
        grid = _Grid(worksheet)
        row_number = grid.locate(key)
        current = _text(grid.cell(row_number, field))
        if current == "":
            raise EditError("an empty cell cannot be marked as a hand edit")
        _protect(worksheet, key, field, current, previous=None)
        return {"row_number": row_number, "field": field, "value": current}

    def write_cells(self, keys: list[dict], field: str, value: str) -> dict:
        """Set `field` to `value` on every row in `keys`, in one read and at most two batched
        writes (RAW values, USER_ENTERED blanks). A key that cannot be located is reported, not
        fatal: the others are still written. Returns {"written", "errors"}."""
        coerced = validate(field, value)
        if not keys:
            raise EditError("no rows selected")
        self._guard()
        worksheet = self._opener()
        grid = _Grid(worksheet)
        targets, errors, located = [], [], []
        for key in keys:
            try:
                targets.append(grid.locate(key))
                located.append(key)
            except EditError as exc:
                errors.append(str(exc))
        seen: set[int] = set()
        blanks, values = [], []
        for key, n in zip(located, targets):
            if n in seen:
                continue
            seen.add(n)
            current = _text(grid.cell(n, field))
            if coerced == "":
                back = _release(worksheet, key, field)  # the run's value, or "" -- never re-protected
                (values if back != "" else blanks).append({"range": f"{_COL[field]}{n}", "values": [[back]]})
            else:
                values.append({"range": f"{_COL[field]}{n}", "values": [[coerced]]})
                _protect(worksheet, key, field, coerced, previous=current)
        if blanks:
            worksheet.batch_update(blanks, value_input_option="USER_ENTERED")
        if values:
            worksheet.batch_update(values, value_input_option="RAW")
        return {"written": len(seen), "errors": errors, "field": field, "value": coerced}

    # --- a new row ------------------------------------------------------------------------------
    def add_row(self, fields: dict) -> dict:
        """Add one row from {field: text}. Needs Order ID, Order Date (YYYY-MM-DD) and Item Name;
        Shipment defaults to 1, Status to ordered; Total Cost is computed from Quantity x Cost Per
        Item when both are given. Every other value is validated as an edit would be. The key
        must not already exist. Returns {"row_number", "key"}."""
        key = normalize_key(fields)
        if not key["order_id"]:
            raise EditError("Order ID is required")
        if not valid_iso_date(key["order_date"]):
            raise EditError("Order Date must be a real calendar date written as YYYY-MM-DD")
        if not key["item_name"]:
            raise EditError("Item Name is required")
        key["shipment"] = key["shipment"] or "1"
        if not key["shipment"].isdigit():
            raise EditError("Shipment must be a number (1, 2, ...)")
        values: dict = dict(key)
        values["shipment"] = int(key["shipment"])
        for field in EDITABLE_FIELDS:
            raw = str(fields.get(field, "") or "")
            if field == "status" and not raw.strip():
                raw = "ordered"
            values[field] = validate(field, raw)
        if values.get("quantity") != "" and values.get("cost_per_item") != "":
            values["total_cost"] = round(values["quantity"] * values["cost_per_item"], 2)
        self._guard()
        worksheet = self._opener()
        grid = _Grid(worksheet)
        try:
            grid.locate(key)
        except ConflictError:
            pass  # not on the ledger: good
        else:
            raise EditError(f"{key_label(key)} is already on the ledger")
        row_number = _last_occupied_row(grid.rows) + 1
        if row_number < 2:
            row_number = 2
        _ensure_grid_rows(worksheet, row_number)
        row = [values.get(f, "") for f in FIELDNAMES]
        for f in FORMULA_FIELDS:
            row[FIELDNAMES.index(f)] = ""
        worksheet.update(f"A{row_number}", [_blank_to_none(row)], value_input_option="RAW")
        _write_profit_formulas(worksheet, [row_number])
        # Every field the user typed on the new row is theirs: a scrape that later finds the
        # order may fill the blanks, never rewrite these.
        for field in EDITABLE_FIELDS:
            if field in FORMULA_FIELDS or field == "total_cost":
                continue
            if str(fields.get(field, "") or "").strip():
                _protect(worksheet, key, field, values.get(field, ""))
        return {"row_number": row_number, "key": key}

    # --- delete rows ----------------------------------------------------------------------------
    def remove_rows(self, keys: list[dict]) -> dict:
        """Remove the rows carrying `keys`, bottom-up so earlier deletions never shift a later
        target. Every key must locate cleanly first: with a structural change there is no partial
        success worth having. Returns {"deleted", "row_numbers"}."""
        if not keys:
            raise EditError("no rows selected")
        self._guard()
        worksheet = self._opener()
        grid = _Grid(worksheet)
        targets = sorted({grid.locate(key) for key in keys}, reverse=True)
        for n in targets:
            worksheet.delete_rows(n)
        for key in keys:
            _forget(worksheet, key)  # a row re-created by a scrape starts clean
        return {"deleted": len(targets), "row_numbers": targets}
