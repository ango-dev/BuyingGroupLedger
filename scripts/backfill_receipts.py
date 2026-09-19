"""Capture receipts for orders ALREADY on the sheet. One-off, dry-run by default.

    python -m scripts.backfill_receipts                              # dry run, everything
    python -m scripts.backfill_receipts --retailer bestbuy           # dry run, one retailer
    python -m scripts.backfill_receipts --retailer bestbuy --apply --limit 3
    python -m scripts.backfill_receipts --apply                      # the whole backlog

WHY A SEPARATE SCRIPT IS NEEDED AT ALL. Receipt capture rides the scrape, and a scrape only ever
touches orders it fetches — new ones, plus open ones being re-checked. A TERMINAL order (delivered /
paid / cancelled / return) is deliberately never re-read, because that is what stops a growing
ledger making every run slower and more expensive. So a row that was already terminal when receipt
capture shipped is unreachable by any normal run, FOREVER: the retailer's page still exists, but
nothing will ever go and fetch it. 33 of the first 40 rows were in exactly that state.

This is that one-off. It reads the sheet, finds rows whose Receipt Link is blank, captures one
receipt per ORDER (not per row — one order is one document, however many line items it became), and
writes the link back to every row of that order.

COSTS MONEY, in a small way: one cloud browser per profile x retailer that has anything to capture,
and one page load per order. It is bounded by --limit and by the fact that an order already in the
bucket is skipped without a browser ever opening, so re-running after a partial failure is cheap.

SAFE TO RE-RUN. It only ever fills BLANK cells, so it cannot overwrite a link, and receipts.store
skips any order already in object storage.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass, field

from config.profiles import load_profiles_for_retailer
from receipts import store
from receipts.capture import attach_receipts
from receipts.sources import is_capturable
from ledger.sync import HEADER, _col_letter, _get_worksheet

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backfill_receipts")

# The sheet stores the DISPLAY name; every other part of the system keys off the retailer_key.
RETAILER_KEYS = {
    "Amazon": "amazon",
    "Amazon Business": "amazon-business",
    "Best Buy": "bestbuy",
    "Costco": "costco",
}


@dataclass
class Order:
    """One order to capture.

    Shaped for attach_receipts, which reads order_id/order_date and sets receipt_url — so this
    stands in for an OrderItem without dragging in its validators.
    """

    order_id: str
    order_date: str
    receipt_url: str = ""
    rows: list = field(default_factory=list)  # sheet row numbers sharing this order
    statuses: list = field(default_factory=list)  # one per row; attach_receipts gates on these

    @property
    def status(self) -> str:
        """attach_receipts reads .status per item. This object stands in for ALL of an order's rows,
        so report the LEAST finished one — that is what its is_capturable check needs to see."""
        for s in self.statuses:
            if (s or "").strip().lower() in ("ordered", "shipped"):
                return s
        return self.statuses[0] if self.statuses else ""


def collect(grid):
    """{(profile_label, retailer_key): {order_id: Order}} for every row with a blank Receipt Link.

    Grouped by PROFILE as well as retailer because a capture needs the logged-in browser profile
    that actually owns the account, and one retailer can span several.
    """
    header = grid[0]
    idx = {name: header.index(name) for name in
           ("Order ID", "Order Date", "Retailer", "Profile", "Receipt Link", "Status")}
    groups = defaultdict(dict)
    skipped = defaultdict(int)

    for offset, row in enumerate(grid[1:]):
        row_number = offset + 2  # row 1 is the header

        def cell(name, _row=row):
            i = idx[name]
            return _row[i].strip() if len(_row) > i else ""

        if cell("Receipt Link"):
            skipped["already has a receipt"] += 1
            continue
        order_id = cell("Order ID")
        if not order_id:
            # ledger_sync refuses to write these anyway; there is nothing to attach a receipt to.
            skipped["blank Order ID"] += 1
            continue
        key = RETAILER_KEYS.get(cell("Retailer"))
        if not key:
            skipped["unrecognized Retailer"] += 1
            continue

        bucket = groups[(cell("Profile"), key)]
        order = bucket.setdefault(order_id, Order(order_id, cell("Order Date")))
        order.rows.append(row_number)
        # attach_receipts gates on status per ROW, so carry every row's status through.
        order.statuses.append(cell("Status"))

    # Drop orders attach_receipts would refuse anyway — a cancelled order (never captured, by
    # design) or one still in flight. Done HERE so the dry run reports what would ACTUALLY be
    # captured; listing an order it would then silently skip makes the preview a lie.
    unfinished = 0
    for key, bucket in list(groups.items()):
        for order_id, order in list(bucket.items()):
            if not is_capturable(order.statuses, include_settled=True):
                del bucket[order_id]
                unfinished += 1
        if not bucket:
            del groups[key]
    if unfinished:
        log.info("Skipped %d order(s): cancelled, or not finished yet.", unfinished)

    for reason, n in sorted(skipped.items()):
        log.info("Skipped %d row(s): %s", n, reason)
    return groups


def _resolve_profile(profile_label, retailer_key):
    """The browser profile to capture on. Prefers the row's own Profile cell."""
    profiles = load_profiles_for_retailer(retailer_key)
    if not profiles:
        return None
    for p in profiles:
        if p.label == profile_label:
            return p
    # A blank or renamed Profile cell should not block a backfill when the retailer has exactly one.
    return profiles[0] if len(profiles) == 1 else None


def _write_links(worksheet, updates, apply):
    """Write the Receipt Link cell for each row.

    Writes ONLY that one column. sync_tracking's _write_payout_cells has to re-stamp the Total
    Profit formula after writing, because it touches cells that formula reads; this touches a column
    the formula has nothing to do with, so the formula is left alone.
    `scripts/audit_ledger.py:check_profit_formula_literal` is the tripwire either way — run it after.
    """
    col = _col_letter(HEADER.index("Receipt Link"))
    data = [{"range": f"{col}{row}", "values": [[link]]}
            for row, link in sorted(updates.items()) if link]
    if not data:
        return 0
    if not apply:
        print(f"    would write {len(data)} Receipt Link cell(s)")
        return 0
    worksheet.batch_update(data, value_input_option="RAW")
    log.info("Wrote %d Receipt Link cell(s).", len(data))
    return len(data)


def run(apply=False, only_retailer=None, limit=None):
    if not store.is_configured():
        print("Receipt storage is not configured; nothing to do. "
              "Turn it on with receipts.capture_enabled in config.json.")
        return 1

    worksheet = _get_worksheet()
    grid = worksheet.get_all_values()
    if grid[0] != list(HEADER):
        print("The sheet header does not match the schema; refusing to write. Run a normal sync "
              "first, which migrates an appended column.")
        return 1

    groups = collect(grid)
    if only_retailer:
        groups = {k: v for k, v in groups.items() if k[1] == only_retailer}
    if not groups:
        print("\nNothing to backfill.")
        return 0

    print(f"\n{'APPLYING' if apply else 'DRY RUN'} — receipts to capture:\n")
    total_written = 0
    for (profile_label, retailer_key), orders in sorted(groups.items()):
        selected = list(orders.values())[:limit] if limit else list(orders.values())
        rows = sum(len(o.rows) for o in selected)
        extra = f"  (limited from {len(orders)})" if limit and len(orders) > len(selected) else ""
        print(f"  {retailer_key} [{profile_label}]: {len(selected)} order(s), {rows} row(s){extra}")

        profile = _resolve_profile(profile_label, retailer_key)
        if profile is None:
            print(f"    ! no profile in profiles.json covers {retailer_key} as {profile_label!r};"
                  f" skipped")
            continue
        if not apply:
            for o in selected[:5]:
                print(f"      {o.order_id}  ({o.order_date})  rows {o.rows}")
            if len(selected) > 5:
                print(f"      ... and {len(selected) - 5} more")
            continue

        # attach_receipts does the rest: it skips anything already in the bucket WITHOUT opening a
        # browser, opens ONE browser for the remainder, and never raises for a single bad order.
        try:
            # include_settled: the sheet's `paid`/`return` rows are genuinely finished
            # purchases that a live scrape can never see (no scraper emits those), and
            # they are exactly the ones you may later have to prove.
            attach_receipts(selected, profile, retailer_key, include_settled=True)
        except Exception:  # noqa: BLE001 — one retailer must not end the backfill
            log.exception("Backfill failed for %s [%s]; continuing.", retailer_key, profile_label)
            continue

        updates = {row: o.receipt_url for o in selected if o.receipt_url for row in o.rows}
        captured = sum(1 for o in selected if o.receipt_url)
        print(f"    captured {captured}/{len(selected)} order(s)")
        total_written += _write_links(worksheet, updates, apply)

    if apply:
        print(f"\nWrote {total_written} Receipt Link cell(s). "
              f"Now run `python -m scripts.audit_ledger` to confirm the sheet is still sound.")
    else:
        print("\nDry run — nothing was captured and nothing was written. Re-run with --apply.")
    return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — not every stream supports it (pytest capture, pipes)
            pass
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true",
                        help="actually capture and write (default is a dry run)")
    parser.add_argument("--retailer", help="amazon | amazon-business | bestbuy | costco")
    parser.add_argument("--limit", type=int, help="cap how many ORDERS per profile x retailer")
    args = parser.parse_args(argv)
    return run(apply=args.apply, only_retailer=args.retailer, limit=args.limit)


if __name__ == "__main__":
    sys.exit(main())
