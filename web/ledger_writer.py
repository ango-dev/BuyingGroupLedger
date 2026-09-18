"""THE ONE PLACE THE DASHBOARD WRITES THE SHEET, because you asked it to on the Orders page:
one cell, the same cell across selected rows, a new row, or the deletion of selected rows. Nothing
automatic passes through here.

The Sheet is still the source of truth (the SQLite file is a mirror -- see ledger_db/), so an edit
made in the browser has to land on the Sheet, exactly as if it had been typed there, and the mirror
picks it up on the next read. When the cutover happens this module targets the database instead
and the page does not change.

WHAT MAY BE EDITED. Everything except: the four UPSERT KEY columns (Order ID, Order Date, Item Name,
Shipment -- changing one turns the row into a different row, and the next re-check appends a
duplicate beside it), the two FORMULA columns (COGS, Total Profit -- the sync re-stamps them, and a
typed number would be overwritten or, worse, freeze a formula), and Last Scraped At (a scraper's
stamp). Status must be one of the ledger's vocabulary; the date columns must be ISO text or blank
(a real Date in the cell would change a key and duplicate the row -- docs/data-model.md).

HOW A CELL IS WRITTEN, mirroring sheets/ledger_sync: the row is located BY KEY on a fresh read (the
sheet may have been re-sorted since the page loaded), the current display text must equal what the
page showed (`expected`) or the edit is refused as a conflict, a value goes RAW through the upsert's
own `_coerce` (a number stays a number, a checkbox a boolean, a date plain text), and a blank goes
USER_ENTERED "" -- the one combination that clears the value while keeping the cell's number format.

A NEW ROW lands where the upsert's own append would: after the last occupied row (ignoring the
checkbox column's materialised FALSEs), the grid grown first if needed, blanks sent as None so the
column formats survive, and the two formula cells stamped afterwards -- the same three helpers the
sync uses. The sort moves it into date order on the next append-triggered sort, as with any hand
row. A DELETE is a structural change: rows are removed bottom-up so the located numbers stay valid.

WHILE A SCHEDULED RUN HOLDS THE RUN LOCK, EVERY WRITE IS REFUSED (423). The sync caches sheet row
numbers from its pre-sync snapshot and writes to them; a row deleted or appended underneath it would
put its updates on the wrong rows, and an edit could be overwritten by its merge. main.py's lock
(logs/.run.lock, stale after 3h) is the signal; the page says "try again in a few minutes".
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable

from models.order import FIELDNAMES, STATUSES, normalize_shipment
from sheets.ledger_sync import (
    HEADER, SCOPES, _BOOL_FIELDS, _COL, _NUMERIC_FIELDS, _blank_to_none, _coerce,
    _ensure_grid_rows, _last_occupied_row, _parse_checkbox, _write_profit_formulas,
)

KEY_FIELDS = ("order_id", "order_date", "item_name", "shipment")
FORMULA_FIELDS = ("cogs", "total_profit")
NEVER_EDITABLE = frozenset(KEY_FIELDS) | frozenset(FORMULA_FIELDS) | {"last_scraped_at"}
EDITABLE_FIELDS = tuple(f for f in FIELDNAMES if f not in NEVER_EDITABLE)
DATE_FIELDS = ("delivery_date", "payout_date", "return_date")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
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
    """The worksheet with the WRITE scope. Deliberately not ledger_sync._get_worksheet, which
    creates a missing tab -- an edit must never create anything."""
    import gspread

    from config.settings import settings

    client = gspread.authorize(settings.google_credentials(SCOPES))
    spreadsheet = client.open_by_key(settings.google_sheet_id)
    name = settings.google_sheet_worksheet_name
    try:
        return spreadsheet.worksheet(name)
    except gspread.WorksheetNotFound as exc:
        raise EditError(f"no worksheet named {name!r}; nothing was written") from exc


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
                           " (a sheet formula)" if field in FORMULA_FIELDS else ""))
    text = (value or "").strip()
    if text == "":
        return ""
    if field == "status":
        if text.lower() not in STATUSES:
            raise EditError(f"Status must be one of {', '.join(STATUSES)}")
        return text.lower()
    if field in DATE_FIELDS:
        if not _ISO_DATE.match(text):
            raise EditError(f"{field} must be a date written as YYYY-MM-DD (text), or blank")
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
    """One fresh formatted read of the sheet, with key lookup."""

    def __init__(self, worksheet):
        from gspread.utils import ValueRenderOption

        self.rows = worksheet.get_values(value_render_option=ValueRenderOption.formatted)
        if not self.rows or [str(c).strip() for c in self.rows[0]] != list(HEADER):
            raise EditError("the sheet's header is not the ledger's column order; refusing to write")
        self.positions = {f: i for i, f in enumerate(FIELDNAMES)}

    def cell(self, row_number: int, name: str) -> str:
        row = self.rows[row_number - 1]
        i = self.positions[name]
        return str(row[i]).strip() if i < len(row) else ""

    def locate(self, key: dict) -> int:
        """The ONE sheet row carrying this key, or a ConflictError / EditError."""
        wanted = normalize_key(key)
        matches = [
            n for n in range(2, len(self.rows) + 1)
            if self.cell(n, "order_id") == wanted["order_id"]
            and self.cell(n, "order_date") == wanted["order_date"]
            and self.cell(n, "item_name") == wanted["item_name"]
            and normalize_shipment(self.cell(n, "shipment")) == wanted["shipment"]
        ]
        if not matches:
            raise ConflictError(f"{key_label(key)}: no longer on the sheet (re-keyed or deleted); "
                                "reload")
        if len(matches) > 1:
            raise EditError(f"{key_label(key)}: {len(matches)} rows share that key; the audit's "
                            "duplicate_primary_keys check names them -- fix the sheet first")
        return matches[0]


class SheetCellWriter:
    """Write cells, append a row, delete rows. `opener` and `logs_dir` are injection points."""

    backend = "sheet"

    def __init__(self, opener: Callable[[], object] | None = None,
                 logs_dir: Path | None = None):
        self._opener = opener or _open_for_writing
        self._logs_dir = Path(logs_dir) if logs_dir else LOGS_DIR

    def _guard(self) -> None:
        if run_in_progress(self._logs_dir):
            raise RunInProgress("a scheduled run is in progress (logs/.run.lock); the sheet's row "
                                "numbers are in use -- try again in a few minutes")

    # --- one cell -------------------------------------------------------------------------------
    def write_cell(self, key: dict, field: str, value: str, expected: str | None = None) -> dict:
        """Locate the row by `key` ({order_id, order_date, item_name, shipment}), check the cell
        still shows `expected`, write. Returns {"row_number", "field", "value"}."""
        coerced = validate(field, value)
        self._guard()
        worksheet = self._opener()
        grid = _Grid(worksheet)
        row_number = grid.locate(key)
        if expected is not None:
            current = grid.cell(row_number, field)
            if _display(current) != _display(expected):
                raise ConflictError(f"the cell now reads {current!r}, not {expected!r}; reload")
        a1 = f"{_COL[field]}{row_number}"
        if coerced == "":
            worksheet.update(a1, [[""]], value_input_option="USER_ENTERED")
        else:
            worksheet.update(a1, [[coerced]], value_input_option="RAW")
        return {"row_number": row_number, "field": field, "value": coerced}

    # --- the same cell across rows (bulk edit) --------------------------------------------------
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
        targets, errors = [], []
        for key in keys:
            try:
                targets.append(grid.locate(key))
            except EditError as exc:
                errors.append(str(exc))
        targets = sorted(set(targets))
        if targets:
            data = [{"range": f"{_COL[field]}{n}", "values": [[coerced]]} for n in targets]
            worksheet.batch_update(data, value_input_option="USER_ENTERED" if coerced == "" else "RAW")
        return {"written": len(targets), "errors": errors, "field": field, "value": coerced}

    # --- a new row ------------------------------------------------------------------------------
    def add_row(self, fields: dict) -> dict:
        """Add one row from {field: text}. Needs Order ID, Order Date (YYYY-MM-DD) and Item Name;
        Shipment defaults to 1, Status to ordered; Total Cost is computed from Quantity x Cost Per
        Item when both are given. Every other value is validated as an edit would be. The key
        must not already exist. Returns {"row_number", "key"}."""
        key = normalize_key(fields)
        if not key["order_id"]:
            raise EditError("Order ID is required")
        if not _ISO_DATE.match(key["order_date"]):
            raise EditError("Order Date must be written as YYYY-MM-DD")
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
            pass  # not on the sheet: good
        else:
            raise EditError(f"{key_label(key)} is already on the sheet")
        row_number = _last_occupied_row(grid.rows) + 1
        if row_number < 2:
            row_number = 2
        _ensure_grid_rows(worksheet, row_number)
        row = [values.get(f, "") for f in FIELDNAMES]
        for f in FORMULA_FIELDS:
            row[FIELDNAMES.index(f)] = ""
        worksheet.update(f"A{row_number}", [_blank_to_none(row)], value_input_option="RAW")
        _write_profit_formulas(worksheet, [row_number])
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
        return {"deleted": len(targets), "row_numbers": targets}
