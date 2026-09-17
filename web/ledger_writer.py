"""THE ONE PLACE THE DASHBOARD WRITES THE SHEET: a single cell, on a row you named, because you typed
into it on the Orders page. Nothing automatic passes through here.

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
"""

from __future__ import annotations

import re
from typing import Callable

from models.order import FIELDNAMES, STATUSES, normalize_shipment
from sheets.ledger_sync import (
    HEADER, SCOPES, _BOOL_FIELDS, _COL, _NUMERIC_FIELDS, _coerce, _parse_checkbox,
)

KEY_FIELDS = ("order_id", "order_date", "item_name", "shipment")
FORMULA_FIELDS = ("cogs", "total_profit")
NEVER_EDITABLE = frozenset(KEY_FIELDS) | frozenset(FORMULA_FIELDS) | {"last_scraped_at"}
EDITABLE_FIELDS = tuple(f for f in FIELDNAMES if f not in NEVER_EDITABLE)
DATE_FIELDS = ("delivery_date", "payout_date", "return_date")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HEADER_TO_FIELD = dict(zip(HEADER, FIELDNAMES))


class EditError(ValueError):
    """The edit was refused; nothing was written. `.status` is the HTTP status the page uses."""

    status = 400


class ConflictError(EditError):
    """The cell no longer holds what the page showed; reload and try again."""

    status = 409


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


class SheetCellWriter:
    """Write one cell of one row. `opener` is the injection point for tests."""

    backend = "sheet"

    def __init__(self, opener: Callable[[], object] | None = None):
        self._opener = opener or _open_for_writing

    def write_cell(self, key: dict, field: str, value: str, expected: str | None = None) -> dict:
        """Locate the row by `key` ({order_id, order_date, item_name, shipment}), check the cell
        still shows `expected`, write. Returns {"row_number", "field", "value"}."""
        coerced = validate(field, value)
        from gspread.utils import ValueRenderOption

        worksheet = self._opener()
        grid = worksheet.get_values(value_render_option=ValueRenderOption.formatted)
        if not grid or [str(c).strip() for c in grid[0]] != list(HEADER):
            raise EditError("the sheet's header is not the ledger's column order; refusing to write")
        positions = {f: i for i, f in enumerate(FIELDNAMES)}

        def cell(row, name):
            i = positions[name]
            return str(row[i]).strip() if i < len(row) else ""

        wanted = {
            "order_id": str(key.get("order_id", "")).strip(),
            "order_date": str(key.get("order_date", "")).strip(),
            "item_name": str(key.get("item_name", "")).strip(),
            "shipment": normalize_shipment(str(key.get("shipment", ""))),
        }
        matches = [
            n for n, row in enumerate(grid[1:], start=2)
            if cell(row, "order_id") == wanted["order_id"]
            and cell(row, "order_date") == wanted["order_date"]
            and cell(row, "item_name") == wanted["item_name"]
            and normalize_shipment(cell(row, "shipment")) == wanted["shipment"]
        ]
        if not matches:
            raise ConflictError("that row is no longer on the sheet (re-keyed or deleted); reload")
        if len(matches) > 1:
            raise EditError(f"{len(matches)} rows share that key; the audit's duplicate_primary_keys "
                            "check names them -- fix the sheet first")
        row_number = matches[0]
        if expected is not None:
            current = cell(grid[row_number - 1], field)
            if _display(current) != _display(expected):
                raise ConflictError(f"the cell now reads {current!r}, not {expected!r}; reload")

        a1 = f"{_COL[field]}{row_number}"
        if coerced == "":
            worksheet.update(a1, [[""]], value_input_option="USER_ENTERED")
        else:
            worksheet.update(a1, [[coerced]], value_input_option="RAW")
        return {"row_number": row_number, "field": field, "value": coerced}
