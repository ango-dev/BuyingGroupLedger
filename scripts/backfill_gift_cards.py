"""Backfill the Gift Card (+ Sales Tax) cells for Amazon orders already on the sheet. Dry run by default.

    python -m scripts.backfill_gift_cards                          # fetch + report, write nothing
    python -m scripts.backfill_gift_cards --apply                  # write it
    python -m scripts.backfill_gift_cards --retailer amazon        # one side only (amazon-business)
    python -m scripts.backfill_gift_cards --orders 111-1111111-1111111 ...

WHY. The Gift Card / Sales Tax columns landed 2026-08-30, and terminal rows are never re-scraped —
so an order part-paid with an Amazon gift card BEFORE then sits with a blank Gift Card cell and a
COGS overstated by the gift-card amount (the legacy scaled-cost rows are fine; this is for imported
and swept rows that carry the GROSS cost). This re-reads each order's details page and fills the
blanks.

ONLY ORDERS THAT USED A GIFT CARD GET WRITES: an order whose summary shows no
`Gift Card Amount` line is reported and left completely alone — not even its tax is written, so the
sweep cannot disturb rows the user has reconciled by hand. For a gift-card order, Sales Tax is
filled too (into a blank cell only): the COGS formula reads both terms, and writing the gift card
without the tax it also covered would understate that order's cost.

COST. One cloud CDP page-load per candidate order, per owning profile — no discovery pass, no
tracking pages, no agent. Orders whose profile is not in config.json are reported and skipped.

GUARDS. Writes land only in cells that are STILL BLANK at write time; each row gets its
cost-weighted share of the order total (the _reprorate_order_level rule); cancelled rows are
skipped. Everything else is read-only.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict

from bs4 import BeautifulSoup

import config.settings  # noqa: F401  -- loads config.json so the cloud-browser API key reaches the SDK
from sheets.ledger_sync import HEADER, _col_letter, _get_worksheet, _parse_display_number

#: retailer display name on the sheet -> (retailer_key, mapping module name, api module name)
_RETAILERS = {
    "Amazon": ("amazon", "scrapers.amazon_mapping", "scrapers.amazon_api"),
    "Amazon Business": ("amazon-business", "scrapers.amazon_business_mapping",
                        "scrapers.amazon_business_api"),
}


def collect_candidates(grid: list[list], only_retailer: str | None,
                       only_orders: set[str]) -> dict[tuple[str, str], dict[str, list[tuple[int, float]]]]:
    """{(retailer_display, profile): {order_id: [(row_number, total_cost), ...]}} for orders with at
    least one blank Gift Card cell. Cancelled rows never count (they carry no money)."""
    idx = {h: i for i, h in enumerate(grid[0])}
    cell = lambda row, name: (str(row[idx[name]]).strip() if name in idx and idx[name] < len(row) else "")  # noqa: E731

    out: dict[tuple[str, str], dict[str, list[tuple[int, float]]]] = defaultdict(lambda: defaultdict(list))
    needs: set[tuple[str, str]] = set()  # (retailer, order) with >= 1 blank Gift Card cell
    for n, row in enumerate(grid[1:], start=2):
        retailer = cell(row, "Retailer")
        order_id = cell(row, "Order ID")
        if retailer not in _RETAILERS or not order_id:
            continue
        if only_retailer and _RETAILERS[retailer][0] != only_retailer:
            continue
        if only_orders and order_id not in only_orders:
            continue
        if cell(row, "Status").lower() == "cancelled":
            continue
        cost = _parse_display_number(cell(row, "Total Cost"))
        out[(retailer, cell(row, "Profile"))][order_id].append((n, float(cost or 0.0)))
        if not cell(row, "Gift Card"):
            needs.add((retailer, order_id))
    for (retailer, profile), orders in list(out.items()):
        for oid in list(orders):
            if (retailer, oid) not in needs:
                del orders[oid]
        if not orders:
            del out[(retailer, profile)]
    return out


def fetch_summaries(retailer: str, profile, order_ids: list[str]) -> dict[str, tuple]:
    """{order_id: (gift_card, sales_tax)} read live from each order's details page."""
    import importlib

    mapping = importlib.import_module(_RETAILERS[retailer][1])
    api = importlib.import_module(_RETAILERS[retailer][2])
    from scrapers.amazon_signin import looks_logged_out
    from scrapers.cdp import CdpBrowser

    client_cls = getattr(api, "AmazonApiClient", None) or api.AmazonBusinessApiClient
    client = client_cls(profile)
    results: dict[str, tuple] = {}
    with CdpBrowser(profile) as page:
        signed_in = False
        for oid in order_ids:
            page.goto(api.ORDER_DETAILS_URL.format(oid), wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            if not signed_in and looks_logged_out(page):
                client._sign_in_here(page)
                page.goto(api.ORDER_DETAILS_URL.format(oid), wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
            signed_in = True
            region = mapping._order_region(BeautifulSoup(page.content(), "html.parser"))
            summary = region.select_one("[data-component='orderSummary']")
            if summary is None:
                print(f"  ?? {oid}: no order summary on the page (kept? too old? wrong account) -- skipped",
                      file=sys.stderr)
                continue
            results[oid] = (mapping._gift_card_amount(summary), mapping._sales_tax_amount(summary),
                            mapping._order_subtotal(summary))
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Backfill Gift Card (+ Sales Tax) for Amazon orders already on the sheet. Dry run by default.")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--retailer", choices=["amazon", "amazon-business"],
                    help="limit to one Amazon side")
    ap.add_argument("--orders", nargs="*", default=[], help="limit to these order numbers")
    args = ap.parse_args(argv)

    from config.profiles import load_profiles_for_retailer

    worksheet = _get_worksheet()
    grid = worksheet.get_all_values()
    candidates = collect_candidates(grid, args.retailer, set(args.orders))
    if not candidates:
        print("No Amazon orders with a blank Gift Card cell match the filters. Nothing to do.")
        return 0

    gc_col = _col_letter(HEADER.index("Gift Card"))
    tax_col = _col_letter(HEADER.index("Sales Tax"))
    gc_i, tax_i = HEADER.index("Gift Card"), HEADER.index("Sales Tax")

    writes: list[tuple[int, str, float, str]] = []   # (row, column letter, value, note)
    no_gift_card = 0
    for (retailer, profile_label), orders in sorted(candidates.items()):
        key = _RETAILERS[retailer][0]
        profile = next((p for p in load_profiles_for_retailer(key) if p.label == profile_label), None)
        if profile is None:
            print(f"  ?? {retailer} [{profile_label}]: profile not configured for {key} -- "
                  f"{len(orders)} order(s) skipped", file=sys.stderr)
            continue
        print(f"{retailer} [{profile_label}]: fetching {len(orders)} order-details page(s)...")
        summaries = fetch_summaries(retailer, profile, sorted(orders))
        for oid, (gift_card, sales_tax, subtotal) in sorted(summaries.items()):
            if not gift_card:
                no_gift_card += 1
                print(f"  -- {oid}: no gift card on this order -- left alone")
                continue
            rows = orders[oid]
            cost_sum = sum(c for _n, c in rows)
            # LEGACY-NETTED GUARD. Before 2026-08-30 the mappings scaled Total Cost down to the
            # card-paid share instead of filling this column, so a row from that era ALREADY nets
            # the gift card inside its cost -- writing the amount again would subtract it twice.
            # The page's own subtotal tells the two shapes apart: a gross row matches the subtotal,
            # a legacy row matches (subtotal - gift card). Anything else is ambiguous (a partial
            # order, a hand edit) and is reported, not guessed.
            if subtotal is not None:
                if abs(cost_sum - (subtotal - gift_card)) <= 0.02:
                    print(f"  -- {oid}: sheet cost {cost_sum:.2f} == subtotal {subtotal:.2f} - gift "
                          f"card {gift_card:.2f} -- ALREADY netted the old way (COGS is right; a "
                          f"blank Gift Card cell counts as 0) -- left alone")
                    continue
                if abs(cost_sum - subtotal) > 0.02:
                    print(f"  ?? {oid}: sheet cost {cost_sum:.2f} matches neither the subtotal "
                          f"{subtotal:.2f} nor its netted form -- resolve by hand", file=sys.stderr)
                    continue
            for n, cost in rows:
                weight = (cost / cost_sum) if cost_sum else (1 / len(rows))
                live = grid[n - 1] if n - 1 < len(grid) else []
                if not str(live[gc_i] if gc_i < len(live) else "").strip():
                    writes.append((n, gc_col, round(gift_card * weight, 2), f"{oid} gift card {gift_card}"))
                if sales_tax is not None and not str(live[tax_i] if tax_i < len(live) else "").strip():
                    writes.append((n, tax_col, round(sales_tax * weight, 2), f"{oid} tax {sales_tax}"))

    for n, col, value, note in writes:
        print(f"  {col}{n}  <- {value:>10.2f}   {note}")
    print(f"\n{len(writes)} cell(s) to fill; {no_gift_card} order(s) had no gift card and were left alone.")
    if not args.apply:
        print("DRY RUN -- nothing written. Re-run with --apply.")
        return 0

    live = worksheet.get_all_values()
    data, kept = [], 0
    for n, col, value, _note in writes:
        i = HEADER.index("Gift Card") if col == gc_col else HEADER.index("Sales Tax")
        current = live[n - 1][i] if n - 1 < len(live) and i < len(live[n - 1]) else ""
        if str(current).strip():
            print(f"  {col}{n} now holds {current!r}; left alone", file=sys.stderr)
            continue
        data.append({"range": f"{col}{n}", "values": [[value]]})
        kept += 1
    if data:
        worksheet.batch_update(data, value_input_option="RAW")
    print(f"Wrote {kept} cell(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
