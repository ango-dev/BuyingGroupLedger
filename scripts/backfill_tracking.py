"""Backfill blank Tracking Number cells from the buying groups, matched by ORDER NUMBER. Dry run by default.

    python -m scripts.backfill_tracking            # show what would be filled
    python -m scripts.backfill_tracking --apply    # write it

WHY. Amazon stops showing tracking numbers on old orders, so a historical sweep records the order
with everything but the number -- and without a number the buying-group sync can never match the
row to its payout. But BFMR's My Tracker keeps the number forever, keyed by the retailer order
number we entered at purchase time, so the join is exact: the order id on our row IS the
`order_id` on their purchase.

MOD is NOT covered: its received-items report carries tracking numbers and item names but no
retailer order number, so there is nothing exact to join on (a fuzzy item+date match could book the
wrong package's payout onto a row, which is worse than a blank).

HOW A NUMBER LANDS ON A ROW. An order's untracked rows are taken in Shipment order and its BFMR
numbers in tracker order. One number and N rows: every row gets it (one box holding several items).
N numbers and N rows: one each. Anything else is ambiguous and is REPORTED, not guessed. Cancelled
rows are skipped. Every write is guarded: the cell must still be blank when it is written.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict

from buying_groups.bfmr import BFMRClient, _tracking_of
from ledger.sync import HEADER, _col_letter, _get_worksheet


def plan_backfill(grid: list[list], bfmr_rows: list[dict]) -> tuple[list[tuple[int, str, str]], list[str]]:
    """(row_number, order_id, tracking) to write, and the ambiguous cases as text. Pure."""
    header = grid[0]
    idx = {h: i for i, h in enumerate(header)}
    cell = lambda row, name: (str(row[idx[name]]).strip() if name in idx and idx[name] < len(row) else "")  # noqa: E731

    numbers: dict[str, list[str]] = defaultdict(list)
    for r in bfmr_rows:
        oid, trk = str(r.get("order_id") or "").strip(), _tracking_of(r)
        if oid and trk and trk not in numbers[oid]:
            numbers[oid].append(trk)

    # A sibling row of the same order that already carries exactly one number: that order shipped
    # as one box (the common case for a multi-unit line the sweep split by shipment), so the blank
    # rows get the same number. Two different sibling numbers say nothing about which box is which.
    siblings: dict[str, set[str]] = defaultdict(set)
    for row in grid[1:]:
        if cell(row, "Order ID") and cell(row, "Tracking Number"):
            siblings[cell(row, "Order ID")].add(cell(row, "Tracking Number"))
    for oid, trks in siblings.items():
        if len(trks) == 1 and not numbers.get(oid):
            numbers[oid] = [next(iter(trks))]

    untracked: dict[str, list[tuple[int, int]]] = defaultdict(list)   # order -> [(shipment, row)]
    for n, row in enumerate(grid[1:], start=2):
        if not cell(row, "Order ID") or cell(row, "Tracking Number"):
            continue
        if cell(row, "Status").lower() == "cancelled" or cell(row, "Buying Group") != "BFMR":
            continue
        ship = cell(row, "Shipment")
        untracked[cell(row, "Order ID")].append((int(ship) if ship.isdigit() else 0, n))

    writes, ambiguous = [], []
    for oid, rows in untracked.items():
        rows.sort()
        found = numbers.get(oid, [])
        if not found:
            ambiguous.append(f"order {oid}: rows {[n for _, n in rows]} -- BFMR has no tracking number for it")
        elif len(found) == 1:
            writes += [(n, oid, found[0]) for _, n in rows]
        elif len(found) == len(rows):
            writes += [(n, oid, trk) for (_, n), trk in zip(rows, found)]
        else:
            ambiguous.append(f"order {oid}: {len(rows)} untracked row(s) but BFMR holds {len(found)} number(s) "
                             f"{found} -- assign by hand")
    return writes, ambiguous


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Backfill blank tracking numbers from BFMR by order number. Dry run by default.")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    worksheet = _get_worksheet()
    grid = worksheet.get_all_values()
    bfmr = BFMRClient(dry_run=True).fetch_tracker()
    writes, ambiguous = plan_backfill(grid, bfmr)

    col = _col_letter(HEADER.index("Tracking Number"))
    for n, oid, trk in writes:
        print(f"  {col}{n}  {oid:<24} <- {trk}")
    for line in ambiguous:
        print(f"  ?? {line}")
    print(f"\n{len(writes)} cell(s) to fill, {len(ambiguous)} order(s) left alone")
    if not args.apply:
        print("DRY RUN -- nothing written. Re-run with --apply, then `python -m sync_tracking --apply --payouts-only --group BFMR`.")
        return 0
    live = worksheet.get_all_values()
    written = 0
    for n, oid, trk in writes:
        current = live[n - 1][HEADER.index("Tracking Number")] if n - 1 < len(live) and HEADER.index("Tracking Number") < len(live[n - 1]) else ""
        if str(current).strip():
            print(f"  {col}{n} now holds {current!r}; left alone", file=sys.stderr)
            continue
        worksheet.update(range_name=f"{col}{n}", values=[[trk]], value_input_option="RAW")
        written += 1
    print(f"Wrote {written} cell(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
