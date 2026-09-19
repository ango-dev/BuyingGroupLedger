"""
One-off: sort the Google Sheet ledger newest-first (Order Date descending).

Why this exists separately from the automatic sort: `main.run_scrape` only re-sorts when a sync
actually APPENDED rows, since an update rewrites a row where it already sits and can't change the
order. That keeps the common re-check run (typically "N updated, 0 appended") from paying a full
formula re-stamp every time — but it also means rows already on the sheet when this feature landed
would never get sorted on their own. This script is that first sort, and the manual fix if the order
ever drifts (e.g. after `scripts/retag_buying_groups.py --apply` deletes rows).

Sort order is Order Date DESC, then Order ID, then Shipment ASC — the tie-breakers keep a
multi-shipment order's rows adjacent and in shipment order rather than scattered among other orders
placed the same day.

Re-stamps every Total Profit formula afterwards, which is mandatory rather than tidy: the formula
uses same-row relative references, so a row that moves needs the formula for its NEW position.
`scripts/audit_ledger.py`'s check_profit_formula_literal is the tripwire for getting that wrong.

DRY RUN BY DEFAULT — reads the live sheet and writes NOTHING, showing the order it would produce:
    python -m scripts.sort_ledger

Apply for real (backs the sheet up to data/ledger_backup_<timestamp>.csv FIRST):
    python -m scripts.sort_ledger --apply
"""

import argparse
import csv
import logging
from datetime import datetime, timezone
from pathlib import Path

from ledger.sync import HEADER, _get_worksheet, sort_ledger_by_date_desc

log = logging.getLogger("sort_ledger")


def plan_sort(header: list[str], data_rows: list[list]) -> dict:
    """Read-only: the row order this sort would produce. Mirrors sort_ledger_by_date_desc's key so the
    dry run shows the real outcome, not an approximation."""
    oid_idx = header.index("Order ID")
    date_idx = header.index("Order Date")
    ship_idx = header.index("Shipment")

    def cell(row, i):
        return row[i] if i < len(row) else ""

    ledger_rows = [r for r in data_rows if str(cell(r, oid_idx)).strip()]

    def ship_key(row):
        shipment = cell(row, ship_idx)
        # Numbers before text, matching Sheets' own type ordering (and tolerating a stray text cell
        # without raising the way a bare mixed-type sort would).
        if isinstance(shipment, (int, float)) and not isinstance(shipment, bool):
            return (0, shipment)
        return (1, str(shipment))

    # Least-significant key first, relying on sort stability — same composition the real sort uses,
    # so this preview is the actual outcome rather than an approximation.
    ordered = sorted(ledger_rows, key=ship_key)
    ordered = sorted(ordered, key=lambda r: str(cell(r, oid_idx)))
    ordered = sorted(ordered, key=lambda r: str(cell(r, date_idx)), reverse=True)

    return {
        "rows": ledger_rows,
        "ordered": ordered,
        "already_sorted": ordered == ledger_rows,
        "non_ledger_rows": len(data_rows) - len(ledger_rows),
    }


def _print_plan(plan: dict, header: list[str], apply: bool) -> None:
    mode = "APPLYING" if apply else "DRY RUN — nothing will be written"
    print(f"Ledger sort, newest first ({mode}):\n")
    print(f"  {len(plan['rows'])} ledger row(s)")
    if plan["non_ledger_rows"]:
        print(f"  {plan['non_ledger_rows']} row(s) with a blank Order ID (not sorted, not counted)")
    if plan["already_sorted"]:
        print("\n  Already in the target order.")

    date_i, oid_i, ship_i, item_i = (header.index(c) for c in
                                     ("Order Date", "Order ID", "Shipment", "Item Name"))
    # Sheet row numbers are only predictable when every row in the block is a ledger row. A row with
    # no Order ID still sorts on whatever it DOES hold — a stray Order Date lands it mid-block rather
    # than at the bottom — which shifts every row beneath it. Fall back to a position index rather
    # than print a row number that would turn out to be wrong; a preview that lies about where rows
    # land is worse than one that declines to say.
    exact_rows = plan["non_ledger_rows"] == 0
    print(f"\n  Resulting order (first 15 of {len(plan['ordered'])}):")
    if not exact_rows:
        print("    (position within the sorted ledger, NOT the sheet row number — the blank-Order-ID "
              "row(s)\n     noted above sort in among these and shift the rows below them)")
    print(f"    {'row' if exact_rows else '#':>4}  {'Order Date':<12}{'Order ID':<22}{'Ship':>5}  Item")
    print("    " + "-" * 76)
    for n, r in enumerate(plan["ordered"][:15], start=2 if exact_rows else 1):
        def cell(i):
            return str(r[i]) if i < len(r) else ""
        print(f"    {n:>4}  {cell(date_i):<12}{cell(oid_i)[:20]:<22}{cell(ship_i):>5}  {cell(item_i)[:34]}")
    if len(plan["ordered"]) > 15:
        print(f"    ... and {len(plan['ordered']) - 15} more")
    print(f"\n  Total Profit formula would be re-stamped on all {len(plan['ordered'])} row(s).")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--apply", action="store_true",
                        help="Actually sort the live sheet (default: dry run, read-only)")
    args = parser.parse_args()

    worksheet = _get_worksheet()
    existing = worksheet.get_all_values()
    if not existing or not any(str(c).strip() for c in existing[0]):
        raise SystemExit("Sheet is empty — nothing to sort.")

    header = [str(c) for c in existing[0]]
    if header != list(HEADER):
        raise SystemExit(
            "The ledger's header doesn't match the current schema, so sorting would target the wrong "
            f"columns (the file migrates its own columns on open, so this should not happen).\n"
            f"  ledger:   {header}\n  expected: {list(HEADER)}"
        )

    plan = plan_sort(header, existing[1:])
    _print_plan(plan, header, args.apply)

    if not args.apply:
        print("\nDry run only — nothing written. Re-run with --apply to make these changes.")
        return

    backup_dir = Path("data")
    backup_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"ledger_backup_{stamp}.csv"
    with backup_path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(existing)
    print(f"\nBacked the whole sheet up -> {backup_path}")

    result = sort_ledger_by_date_desc(worksheet)
    print(f"Sorted {result['sorted_rows']} row(s) and re-stamped their Total Profit formulas.")
    print("\nDone. Verify with `python -m scripts.audit_ledger`.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    main()
