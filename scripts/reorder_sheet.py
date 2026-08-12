"""
One-off: rewrite the rows ALREADY on the Google Sheet ledger into the current column order.

Why this exists: rows are written to the sheet POSITIONALLY from column A, so `models.order.FIELDNAMES`
and `sheets.ledger_sync.HEADER` define where every value lands. Reordering those lists (done once on
2026-08-12, to put the columns in reading order) does NOT move the data already on the sheet — the
header would say one thing while every existing row still held the old arrangement. `sync_csv_to_sheet`
refuses to write to a sheet whose header order doesn't match, precisely so that mismatch can't silently
scramble rows; this script is what resolves it.

It remaps BY COLUMN NAME, so it doesn't care what the old order was and is safe to re-run (a sheet
already in the right order reports "nothing to do"). It also:

  - normalizes the Shipment column from the old "Shipment 1" wording to a bare "1" (that column is part
    of the upsert key, so old-style values would otherwise duplicate against newly-scraped rows), and
  - re-stamps the Total Profit formula on every data row, because the formula references columns by
    letter and every letter moved.

DRY RUN BY DEFAULT — reads the live sheet and writes NOTHING:
    python -m scripts.reorder_sheet

Apply for real (backs the sheet up to data/sheet_backup_<timestamp>.csv FIRST):
    python -m scripts.reorder_sheet --apply
"""

import argparse
import csv
import logging
from datetime import datetime, timezone
from pathlib import Path

from gspread.utils import ValueInputOption, ValueRenderOption

from models.order import normalize_shipment
from sheets.ledger_sync import HEADER, _get_worksheet, _write_profit_formulas

log = logging.getLogger("reorder_sheet")


def plan_reorder(header: list[str], data_rows: list[list]) -> dict:
    """Read-only: work out the rewritten sheet. Returns the new header+rows and what changed.

    Columns are matched by NAME. A column in HEADER that the sheet doesn't have is filled blank (that's
    how an older, shorter sheet gains the newer columns). A column the SHEET has that HEADER doesn't is
    reported as `dropped` — it would be lost, so the caller can refuse rather than silently discard it.
    """
    old_index = {name: i for i, name in enumerate(header)}
    dropped = [name for name in header if name and name not in HEADER]
    added = [name for name in HEADER if name not in old_index]

    def cell(row, name):
        i = old_index.get(name)
        if i is None or i >= len(row):
            return ""
        return row[i]

    ship_col = "Shipment"
    new_rows, reshipped, stray_formulas = [], 0, []
    for offset, row in enumerate(data_rows):
        new_row = [cell(row, name) for name in HEADER]
        # The Shipment cell is part of the upsert key, so old "Shipment 1" values must become "1" or
        # the next scrape appends a duplicate row instead of updating this one.
        si = HEADER.index(ship_col)
        before = str(new_row[si])
        after = normalize_shipment(before)
        if after != before:
            reshipped += 1
        new_row[si] = after
        # Any OTHER formula the user added by hand would be rewritten as literal text by the RAW write
        # below (Total Profit is re-stamped afterwards, so it's exempt). Flag rather than clobber.
        for i, value in enumerate(new_row):
            if isinstance(value, str) and value.startswith("=") and HEADER[i] != "Total Profit":
                stray_formulas.append((offset + 2, HEADER[i]))
        new_rows.append(new_row)

    return {
        "already_correct": header == list(HEADER),
        "new_rows": new_rows,
        "dropped": dropped,
        "added": added,
        "shipment_relabelled": reshipped,
        "stray_formulas": stray_formulas,
    }


def _print_plan(plan: dict, header: list[str], apply: bool) -> None:
    mode = "APPLYING" if apply else "DRY RUN — nothing will be written"
    print(f"Sheet column reorder ({mode}):\n")
    print(f"  current order: {header}")
    print(f"  target order:  {list(HEADER)}\n")
    if plan["already_correct"]:
        print("  Header is ALREADY in the target order.")
    else:
        moved = [f"{name}: col {header.index(name) + 1} -> {HEADER.index(name) + 1}"
                 for name in HEADER
                 if name in header and header.index(name) != HEADER.index(name)]
        print(f"  {len(moved)} column(s) move:")
        for line in moved:
            print(f"    {line}")
    if plan["added"]:
        print(f"  {len(plan['added'])} column(s) added (filled blank): {plan['added']}")
    if plan["dropped"]:
        print(f"  !! {len(plan['dropped'])} column(s) on the sheet are NOT in the schema and would be "
              f"LOST: {plan['dropped']}")
    print(f"  {len(plan['new_rows'])} data row(s) would be rewritten")
    print(f"  {plan['shipment_relabelled']} Shipment cell(s) would drop the 'Shipment ' prefix")
    print(f"  Total Profit formula would be re-stamped on {len(plan['new_rows'])} row(s)")
    if plan["stray_formulas"]:
        print(f"  !! {len(plan['stray_formulas'])} hand-written formula(s) outside Total Profit would "
              f"become plain text: {plan['stray_formulas'][:5]}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--apply", action="store_true",
                        help="Actually rewrite the live sheet (default: dry run, read-only)")
    parser.add_argument("--force", action="store_true",
                        help="Proceed even if columns would be dropped or hand-written formulas lost")
    args = parser.parse_args()

    worksheet = _get_worksheet()
    # FORMULA render so a formula cell comes back as its formula rather than its evaluated value —
    # otherwise the rewrite would freeze every Total Profit cell into a stale number.
    existing = worksheet.get_values(value_render_option=ValueRenderOption.formula)
    if not existing or not any(str(c).strip() for c in existing[0]):
        raise SystemExit("Sheet is empty — nothing to reorder.")

    header = [str(c) for c in existing[0]]
    plan = plan_reorder(header, existing[1:])
    _print_plan(plan, header, args.apply)

    if plan["already_correct"] and not plan["shipment_relabelled"]:
        print("\nNothing to do.")
        return

    blockers = []
    if plan["dropped"]:
        blockers.append(f"{len(plan['dropped'])} column(s) would be lost")
    if plan["stray_formulas"]:
        blockers.append(f"{len(plan['stray_formulas'])} hand-written formula(s) would become text")
    if blockers and not args.force:
        raise SystemExit(
            "\nRefusing to proceed: " + "; ".join(blockers) +
            ". Re-run with --force if that's genuinely what you want."
        )

    if not args.apply:
        print("\nDry run only — nothing written. Re-run with --apply to make these changes.")
        return

    backup_dir = Path("data")
    backup_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"sheet_backup_{stamp}.csv"
    with backup_path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(existing)
    print(f"\nBacked the whole sheet up -> {backup_path}")

    # One RAW write of header + every row. RAW (not USER_ENTERED) so numeric-looking text such as a
    # long tracking number stays text instead of being reinterpreted into scientific notation.
    block = [list(HEADER)] + plan["new_rows"]
    worksheet.update(range_name="A1", values=block,
                     value_input_option=ValueInputOption.raw)
    print(f"Rewrote {len(plan['new_rows'])} row(s) into the new column order.")

    # Re-stamp AFTER the rewrite: the formula references columns by letter, and every letter moved.
    _write_profit_formulas(worksheet, list(range(2, len(plan["new_rows"]) + 2)))
    print(f"Re-stamped the Total Profit formula on {len(plan['new_rows'])} row(s).")
    print("\nDone.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    main()
