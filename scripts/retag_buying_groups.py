"""
One-off: retroactively apply buying-group classification (config.warehouses) to rows ALREADY on the
Ledger, and remove rows that classify as Personal.

Why this exists: the classifier only tags NEW or re-checked rows at scrape time
(main.run_scrape -> config.warehouses.tag_and_filter_personal). Rows recorded before the Buying Group
column existed, or before warehouses.json had an entry that now matches them, are never revisited
automatically. This script is the one-time backfill.

DRY RUN BY DEFAULT — reads the ledger but writes NOTHING. Just reports what it would do:
    python -m scripts.retag_buying_groups

Apply the changes for real (updates Buying Group cells, DELETES personal rows from the ledger). Deleted
rows are backed up to data/purged_personal_<timestamp>.csv FIRST, so nothing is lost:
    python -m scripts.retag_buying_groups --apply

Order of operations on --apply (deliberate): header migration, then cell UPDATES (using the row numbers
read at the start — safe, since a cell write never shifts row positions), then the backup CSV, then
DELETIONS from the bottom up (so a delete never invalidates a row number still to be processed).
"""

import argparse
import csv
import logging
from datetime import datetime, timezone
from pathlib import Path

from config.warehouses import load_warehouses
from ledger.sync import HEADER, _get_worksheet, plan_buying_group_retag

log = logging.getLogger("retag_buying_groups")


def _col_letter(index0: int) -> str:
    """0-based column index -> its A1 column letter(s)."""
    n = index0 + 1
    letters = ""
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _print_plan(plan: dict, apply: bool) -> None:
    print(f"Buying-group retag plan ({'APPLYING' if apply else 'DRY RUN — nothing will be written'}):")
    if plan["needs_header_migration"]:
        print("  Ledger predates the 'Buying Group' column; it will be added.")
    print(f"  {len(plan['updates'])} row(s) would get a new/changed Buying Group tag")
    print(f"  {len(plan['deletions'])} row(s) classify as Personal and would be REMOVED from the ledger")
    print(f"  {plan['unchanged']} row(s) unchanged")
    if plan["group_counts"]:
        print("  Resulting group counts (excluding deletions):")
        for group, count in sorted(plan["group_counts"].items()):
            print(f"    {group}: {count}")
    if plan["deletions"]:
        print("\n  Rows that would be removed (Personal):")
        for row_number, oid, name, addr, old_tag in plan["deletions"]:
            was = f" (was tagged {old_tag!r})" if old_tag else ""
            print(f"    row {row_number}: order {oid} / {name!r}{was} -> {addr}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually write/delete on the ledger (default: dry run, read-only)",
    )
    args = parser.parse_args()

    warehouses = load_warehouses()
    if not warehouses:
        raise SystemExit(
            "config.json has no `warehouses` section (or it is empty) — nothing to classify. "
            "config.example.json shows the shape; configure your jigs there first."
        )

    worksheet = _get_worksheet()
    existing = worksheet.get_all_values()
    if not existing or not any(cell.strip() for cell in existing[0]):
        raise SystemExit("Ledger is empty — nothing to retag.")

    header = existing[0]
    plan = plan_buying_group_retag(header, existing[1:], warehouses)
    _print_plan(plan, args.apply)

    if not args.apply:
        print("\nDry run only — nothing written. Re-run with --apply to make these changes.")
        return

    if not plan["updates"] and not plan["deletions"]:
        print("\nNothing to change.")
        return

    if plan["needs_header_migration"]:
        worksheet.update(range_name="A1", values=[HEADER])
        header = list(HEADER)
        print("Added the Buying Group column.")

    # UPDATES FIRST: a cell write never changes row positions, so the original row numbers stay valid
    # regardless of what deletions happen afterward. Doing deletions first would invalidate any
    # not-yet-applied update whose row number sits below a deleted row.
    if plan["updates"]:
        bg_col = _col_letter(header.index("Buying Group"))
        for row_number, _oid, _name, _old_tag, new_tag in plan["updates"]:
            worksheet.update(range_name=f"{bg_col}{row_number}", values=[[new_tag]])
        print(f"Updated Buying Group on {len(plan['updates'])} row(s).")

    if plan["deletions"]:
        backup_dir = Path("data")
        backup_dir.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = backup_dir / f"purged_personal_{stamp}.csv"
        row_by_number = {i + 2: r for i, r in enumerate(existing[1:])}
        with backup_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            for row_number, *_ in plan["deletions"]:
                writer.writerow(row_by_number[row_number])
        print(f"Backed up {len(plan['deletions'])} row(s) about to be removed -> {backup_path}")

        # Bottom-to-top by ORIGINAL row number: deleting the highest row first only shifts rows BELOW
        # it (none remain to process), so every row number in this list stays valid as we go.
        for row_number, *_ in sorted(plan["deletions"], key=lambda t: t[0], reverse=True):
            worksheet.delete_rows(row_number)
        print(f"Deleted {len(plan['deletions'])} personal row(s) from the ledger.")

    print("\nDone.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    main()
