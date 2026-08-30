"""Apply the ledger's number formats to the live sheet.

WHY THIS EXISTS. Number formats are attached to a cell POSITION, and `scripts/reorder_sheet.py`
moves VALUES between positions without touching formatting — so after a reorder every format is left
behind on whatever column now occupies that letter. The 2026-08-25 reorder, for example, moved
Cashback Rate from O to L, which left a PERCENT format sitting on Payout Amount and a CURRENCY format
on Cashback Rate. That is cosmetic rather than corrupting (`_parse_display_number` round-trips "386%"
back to 3.86 — see the design notes), but a sheet that shows costs as percentages is unusable.

Writing a row RAW with "" also STRIPS that cell's number format (measured; see
`ledger_sync._blank_to_none`), which is the second way the sheet loses formatting: reorder_sheet
writes every blank cell that way.

THE FORMAT MAP IS DERIVED FROM `HEADER`, by column NAME. That is the whole point — it is correct
after any future reorder without being edited, in the way a list of column letters would not be.

DRY RUN BY DEFAULT — reads the live sheet and writes NOTHING:
    python -m scripts.apply_sheet_formats

Apply for real:
    python -m scripts.apply_sheet_formats --apply
"""

import argparse
import logging

import gspread

from config.settings import settings
from sheets.ledger_sync import HEADER, SCOPES, _col_letter

log = logging.getLogger("apply_sheet_formats")

#: Columns that must display as money, by NAME. Everything the year-end totals are read off.
CURRENCY_COLUMNS = (
    "Cost Per Item", "Total Cost", "Shipping", "COGS", "Insurance", "Payout Amount", "Total Profit",
    "Gift Card", "Sales Tax",
)
#: Columns that must display as a percentage. Cashback Rate STORES a fraction (0.04) and shows "4%".
PERCENT_COLUMNS = ("Cashback Rate",)

_CURRENCY = {"type": "CURRENCY", "pattern": '"$"#,##0.00'}
_PERCENT = {"type": "PERCENT", "pattern": "0.##%"}

#: The sheet is a Google Sheets TABLE, and a table column's TYPE OVERRIDES the cell number format —
#: set a cell to PERCENT under a CURRENCY column and nothing happens, silently. So the table types are
#: the authority here and the cell formats above are only a fallback for if the table is ever removed.
#:
#: Like formats, a column type is bound to a POSITION, so reorder_sheet leaves every type behind on
#: whatever column inherits its letter. After the 2026-08-25 reorder that put PERCENT on Payout Amount
#: (a $631 payout displayed as "63100%") and, worse, moved the BOOLEAN checkbox off Tracking Submitted
#: onto Delivery Address.
#:
#: THE DATE COLUMNS ARE DELIBERATELY UNTYPED. Order Date is in the upsert key and MUST stay plain ISO
#: text — typing it as DATE makes Sheets store a serial, which changes the key and duplicates the row
#: on its next re-check. That is the design notes, and it cost a migration to undo the first time.
TABLE_COLUMN_TYPES = {
    "Cost Per Item": "CURRENCY", "Total Cost": "CURRENCY", "Shipping": "CURRENCY",
    "COGS": "CURRENCY", "Insurance": "CURRENCY", "Payout Amount": "CURRENCY",
    "Total Profit": "CURRENCY", "Gift Card": "CURRENCY", "Sales Tax": "CURRENCY",
    "Cashback Rate": "PERCENT",
    "Tracking Submitted": "BOOLEAN",   # the checkbox sync_tracking.py ticks
    "Status": "DROPDOWN",              # its dataValidationRule is preserved, never rebuilt
}


def plan_table_columns(table: dict, header: list[str]) -> list[dict]:
    """The full columnProperties list the table should have, by NAME.

    Rebuilt in full rather than patched, because updateTable replaces the whole list. Status's
    existing dataValidationRule is carried over verbatim: it holds the status vocabulary, and
    regenerating it here would put a second copy of STATUSES in the codebase to drift.
    """
    existing = {c.get("columnIndex", 0): c for c in table.get("columnProperties", [])}
    out = []
    for index, name in enumerate(header):
        prop = {"columnIndex": index, "columnName": name}
        wanted = TABLE_COLUMN_TYPES.get(name)
        if wanted:
            prop["columnType"] = wanted
            rule = existing.get(index, {}).get("dataValidationRule")
            if wanted == "DROPDOWN" and rule:
                prop["dataValidationRule"] = rule
        out.append(prop)
    return out


#: Cell text a Google Sheets checkbox leaves behind. An EMPTY cell under a BOOLEAN column materialises
#: as a real False, so these are values, not blanks.
_CHECKBOX_TEXT = {"true", "false"}


def plan_stale_checkbox_padding(grid: list[list], header: list[str], checkbox: str) -> list[str]:
    """A1 column ranges below the data block still holding checkbox padding from a FORMER position.

    THE FAILURE THIS FIXES. `Tracking Submitted` is a BOOLEAN column, and an empty cell under one
    materialises a real False all the way down the table -- harmless where it belongs. Move that column
    in a reorder and `reorder_sheet` rewrites only the DATA block, so every row below it keeps the
    False in the column the checkbox used to occupy.

    It used to clear itself by luck: the vacated column normally became untyped, and clearing a
    column's type clears its materialised values. The 2026-08-25 lifecycle reorder put `Total Profit`
    (CURRENCY) where the checkbox had been, and a typed column does NOT clear them -- so 941 rows kept
    a literal FALSE in the Total Profit column.

    That is not cosmetic. `ledger_sync._last_occupied_row` only forgives a row whose SOLE content is a
    checkbox False; two of them makes the row look occupied, so the append anchor jumped from row 43
    to 984 and the next scraped order would have landed ~940 rows below the ledger.

    Returns one range per offending column (never one per cell -- that would be ~940 requests).
    """
    if not grid:
        return []
    oid = header.index("Order ID") if "Order ID" in header else 0
    last_data = max(
        (n for n, row in enumerate(grid[1:], start=2)
         if oid < len(row) and str(row[oid]).strip()),
        default=1,
    )
    keep = header.index(checkbox) if checkbox in header else -1
    ranges = []
    for col in range(len(header)):
        if col == keep:
            continue  # where the checkbox lives now: its padding is expected
        if any(col < len(grid[n - 1]) and str(grid[n - 1][col]).strip().lower() in _CHECKBOX_TEXT
               for n in range(last_data + 1, len(grid) + 1)):
            letter = _col_letter(col)
            ranges.append(f"{letter}{last_data + 1}:{letter}{len(grid)}")
    return ranges


def plan_formats(header: list[str]) -> dict:
    """Read-only: which column gets which format, and which must be CLEARED.

    A column that is not money and not a rate is cleared rather than left alone: after a reorder it
    may be carrying a format inherited from whatever used to sit at its letter, and "leave it alone"
    would keep a tracking number formatted as currency forever.
    """
    wanted = {}
    for name in CURRENCY_COLUMNS:
        wanted[name] = _CURRENCY
    for name in PERCENT_COLUMNS:
        wanted[name] = _PERCENT
    missing = [n for n in wanted if n not in header]
    return {
        "set": [(n, header.index(n), wanted[n]) for n in wanted if n in header],
        "clear": [(n, i) for i, n in enumerate(header) if n not in wanted],
        "missing": missing,
    }


def _requests(sheet_id: int, plan: dict, last_row: int) -> list[dict]:
    def rng(index):
        # Row 1 is the header and stays plain text; formats apply to the data block only.
        return {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": last_row,
                "startColumnIndex": index, "endColumnIndex": index + 1}

    out = []
    for _name, index, fmt in plan["set"]:
        out.append({"repeatCell": {
            "range": rng(index),
            "cell": {"userEnteredFormat": {"numberFormat": fmt}},
            "fields": "userEnteredFormat.numberFormat",
        }})
    for _name, index in plan["clear"]:
        out.append({"repeatCell": {
            "range": rng(index),
            "cell": {"userEnteredFormat": {}},
            "fields": "userEnteredFormat.numberFormat",
        }})
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--apply", action="store_true",
                        help="Actually write the formats (default: dry run, read-only)")
    args = parser.parse_args()

    client = gspread.authorize(settings.google_credentials(SCOPES))
    spreadsheet = client.open_by_key(settings.google_sheet_id)
    worksheet = spreadsheet.worksheet(settings.google_sheet_worksheet_name)

    header = worksheet.row_values(1)
    if header != list(HEADER):
        raise SystemExit(
            "Sheet header doesn't match the current schema, so formats would land on the wrong "
            f"columns. Run `python -m scripts.reorder_sheet` first.\n  sheet:    {header}\n"
            f"  expected: {list(HEADER)}"
        )

    meta = spreadsheet.fetch_sheet_metadata(params={"fields": "sheets(properties/title,tables)"})
    sheet_meta = next(s for s in meta["sheets"]
                      if s["properties"]["title"] == settings.google_sheet_worksheet_name)
    tables = sheet_meta.get("tables", [])
    table = tables[0] if tables else None

    plan = plan_formats(header)
    mode = "APPLYING" if args.apply else "DRY RUN — nothing will be written"
    print(f"Sheet number formats ({mode}):\n")
    for name, index, fmt in plan["set"]:
        print(f"  {_col_letter(index):>3}  {name:<16} -> {fmt['type']}")
    print(f"\n  {len(plan['clear'])} other column(s) cleared to automatic")
    if plan["missing"]:
        raise SystemExit(f"\nColumns named in the format map are not on the sheet: {plan['missing']}")

    if table is None:
        print("\n  No Table on this tab -- cell formats are the only authority.")
        columns = None
    else:
        columns = plan_table_columns(table, header)
        current = {c.get("columnIndex", 0): c.get("columnType")
                   for c in table.get("columnProperties", [])}
        changes = [(header[c["columnIndex"]], current.get(c["columnIndex"]), c.get("columnType"))
                   for c in columns if current.get(c["columnIndex"]) != c.get("columnType")]
        print(f"\n  Table {table.get('name')!r} column TYPES (these OVERRIDE the formats "
              f"above) -- {len(changes)} change(s):")
        for name, was, now in changes:
            print(f"    {name:<20} {was or '(none)':<9} -> {now or '(none)'}")

    grid = worksheet.get_values()
    stale = plan_stale_checkbox_padding(grid, header, "Tracking Submitted")
    if stale:
        print(f"\n  Stale checkbox padding to clear (left by a former BOOLEAN column): "
              f"{', '.join(stale)}")

    if not args.apply:
        print("\nDry run only — nothing written. Re-run with --apply to make these changes.")
        return

    last_row = worksheet.row_count
    requests = _requests(worksheet.id, plan, last_row)
    if columns is not None:
        # Sent FIRST: the table column type is what actually governs display, so applying it
        # before the cell formats means the fallback formats land under the right types.
        requests.insert(0, {"updateTable": {
            "table": {"tableId": table["tableId"], "columnProperties": columns},
            "fields": "columnProperties",
        }})
    spreadsheet.batch_update({"requests": requests})
    if stale:
        # batch_clear, NOT batch_update: a 1x1 values array only clears the range's FIRST cell, and
        # sizing a full matrix for ~940 rows would be absurd. values.batchClear also clears values
        # WITHOUT touching formatting, which is exactly the scope wanted here.
        worksheet.batch_clear(stale)
        print(f"Cleared stale checkbox padding in {len(stale)} column(s).")
    print(f"\nApplied formats down to row {last_row}.")
    print("Verify with `python -m scripts.audit_sheet`.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    main()
