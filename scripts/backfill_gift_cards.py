"""Backfill the Gift Card (+ Sales Tax) cells for Amazon orders already on the sheet. Dry run by default.

    python -m scripts.backfill_gift_cards                          # fetch + report, write nothing
    python -m scripts.backfill_gift_cards --apply                  # write it
    python -m scripts.backfill_gift_cards --retailer amazon        # one side only (amazon-business)
    python -m scripts.backfill_gift_cards --orders 111-1111111-1111111 ...

WHY. The Gift Card / Sales Tax columns landed 2026-08-30, and terminal rows are never re-scraped —
so an order part-paid with an Amazon gift card BEFORE then never gets its cell filled. This
re-reads each order's details page and fills the blanks.

A DETECTED ZERO IS A VALUE: an
order whose parsed summary shows no `Gift Card Amount` line gets real 0.00s in its blank Gift Card
cells — "checked, none" instead of "unknown" — plus the page's Sales Tax. Only an order whose
summary could not be parsed at all is left alone, so a 0 never stands in for ignorance. Sales Tax
is filled for gift-card orders too (blank cells only): the COGS formula reads both terms, and
writing the gift card without the tax it covered would understate that order's cost.

LEGACY-NETTED ROWS ARE CONVERTED, not skipped. Before the columns existed the mappings scaled Total Cost down to
the card-paid share and threw the amount away; the page's own subtotal tells that shape apart from
a gross row (gross matches the subtotal, legacy matches subtotal - gift card). Such an order gets
its Cost Per Item / Total Cost RESTORED TO GROSS in the same write batch that fills the Gift Card
cell, so COGS — a live formula reading both — lands on the identical net number with the gift card
now visible. The conversion refuses two shapes rather than guessing: a legacy row carrying nonzero
Shipping (the old scaling shrank that too, and re-deriving it needs the page's shipping line — none
exist on the live sheet today), and a sheet cost matching neither form (a partial order or a hand
edit).

REWARDS USED (2026-09-08) rides along: a spent Prime cash-back balance ("Prime for Young Adults
cash back: -$15.98", a summary line) and Amazon points (the Prime Business card's rewards, which
the order page never prices — Business reads the rewards ledger once, the related-transactions
page is the fallback) land in their own column, filled here into BLANK cells like the other two.
They are NOT gift cards: the cost stays full and only the cashback basis shrinks, because the user
nets every Amazon reward from COGS at year end outside the sheet.

COST. One cloud CDP page-load per candidate order, per owning profile — no discovery pass, no
tracking pages, no agent. Orders whose profile is not in config.json are reported and skipped.

GUARDS. Gift Card / Sales Tax land only in cells that are STILL BLANK at write time; a conversion's
cost cells are guarded on their CURRENT value instead (they must still hold the netted number the
plan saw). Each row gets its cost-weighted share of the order totals (the _reprorate_order_level
rule); cancelled rows are skipped. Everything else is read-only.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict

from bs4 import BeautifulSoup

import config.settings  # noqa: F401  -- loads config.json so the cloud-browser API key reaches the SDK
from models.order import MONEY_FREE_STATUSES
from sheets.ledger_sync import HEADER, _col_letter, _get_worksheet, _parse_display_number

#: retailer display name on the sheet -> (retailer_key, mapping module name, api module name)
_RETAILERS = {
    "Amazon": ("amazon", "scrapers.amazon_mapping", "scrapers.amazon_api"),
    "Amazon Business": ("amazon-business", "scrapers.amazon_business_mapping",
                        "scrapers.amazon_business_api"),
}


def collect_candidates(grid: list[list], only_retailer: str | None,
                       only_orders: set[str]) -> dict[tuple[str, str], dict[str, list[dict]]]:
    """{(retailer_display, profile): {order_id: [row dict, ...]}} for orders with at least one blank
    Gift Card or Rewards Used cell. Cancelled rows never count (they carry no money). Each row dict
    carries what the planner needs: row number, cost, quantity, shipping, and which target cells
    are blank."""
    idx = {h: i for i, h in enumerate(grid[0])}
    cell = lambda row, name: (str(row[idx[name]]).strip() if name in idx and idx[name] < len(row) else "")  # noqa: E731

    out: dict[tuple[str, str], dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    needs: set[tuple[str, str]] = set()  # (retailer, order) with >= 1 blank target cell
    for n, row in enumerate(grid[1:], start=2):
        retailer = cell(row, "Retailer")
        order_id = cell(row, "Order ID")
        if retailer not in _RETAILERS or not order_id:
            continue
        if only_retailer and _RETAILERS[retailer][0] != only_retailer:
            continue
        if only_orders and order_id not in only_orders:
            continue
        if cell(row, "Status").lower() in MONEY_FREE_STATUSES:
            continue  # cancelled / superseded rows carry no money and need no share
        quantity = _parse_display_number(cell(row, "Quantity"))
        gc_blank = not cell(row, "Gift Card")
        rw_blank = not cell(row, "Rewards Used")
        out[(retailer, cell(row, "Profile"))][order_id].append({
            "n": n,
            "cost": float(_parse_display_number(cell(row, "Total Cost")) or 0.0),
            "quantity": int(quantity) if quantity else None,
            "shipping": float(_parse_display_number(cell(row, "Shipping")) or 0.0),
            "gc_blank": gc_blank,
            "rw_blank": rw_blank,
            "tax_blank": not cell(row, "Sales Tax"),
        })
        if gc_blank or rw_blank:
            needs.add((retailer, order_id))
    for (retailer, profile), orders in list(out.items()):
        for oid in list(orders):
            if (retailer, oid) not in needs:
                del orders[oid]
        if not orders:
            del out[(retailer, profile)]
    return out


def _rewards_writes(rows: list[dict], rewards_used: float | None) -> list[dict]:
    """Rewards Used shares for the rows whose cell is blank — cost-weighted like the others. None
    (the amount could not be read) writes nothing, so a blank never becomes a false 0."""
    if rewards_used is None:
        return []
    cost_sum = sum(r["cost"] for r in rows)
    writes = []
    for r in rows:
        if not r.get("rw_blank"):
            continue
        weight = (r["cost"] / cost_sum) if cost_sum else (1 / len(rows))
        writes.append({"n": r["n"], "field": "rewards_used",
                       "value": round(rewards_used * weight, 2), "expect": None})
    return writes


def plan_order_writes(rows: list[dict], gift_card: float | None, sales_tax: float | None,
                      subtotal: float | None, rewards_used: float | None = None) -> tuple[list[dict], str]:
    """The cell writes for ONE order, and a one-line note describing what was decided.

    Pure. Returns ([], note) when the order is left alone. Each write is
    {"n": row, "field": ledger field, "value": ..., "expect": current-value-or-None} — "expect" is
    set for a conversion's cost cells, which must still hold the netted number at write time.
    """
    if gift_card is None:
        return [], "no parsed summary for this order -- left alone"
    cost_sum = sum(r["cost"] for r in rows)
    rewards = _rewards_writes(rows, rewards_used)
    rewards_note = f"; rewards used {rewards_used:.2f}" if rewards_used else ""

    # A detected 0 IS a value now: fill the blanks with real zeros (and the
    # page's tax) so the column reads "checked, none" instead of "unknown". No legacy/shape checks
    # apply — nothing was ever netted out of a no-gift-card order.
    if not gift_card:
        writes = []
        for r in rows:
            weight = (r["cost"] / cost_sum) if cost_sum else (1 / len(rows))
            if r["gc_blank"]:
                writes.append({"n": r["n"], "field": "gift_card", "value": 0.0, "expect": None})
            if sales_tax is not None and r["tax_blank"]:
                writes.append({"n": r["n"], "field": "sales_tax",
                               "value": round(sales_tax * weight, 2), "expect": None})
        return writes + rewards, (f"no gift card -> 0.00 filled (tax "
                                  f"{sales_tax if sales_tax is not None else 'unknown'}){rewards_note}")

    legacy = subtotal is not None and abs(cost_sum - (subtotal - gift_card)) <= 0.02
    if subtotal is not None and not legacy and abs(cost_sum - subtotal) > 0.02:
        return [], (f"?? sheet cost {cost_sum:.2f} matches neither the subtotal {subtotal:.2f} nor "
                    f"its netted form -- resolve by hand")

    writes: list[dict] = []
    note = f"gift card {gift_card:.2f}{rewards_note}"
    if legacy:
        # CONVERT: restore gross costs (the old scaling was uniform, so each row's gross share is
        # subtotal-proportional), then fill the Gift Card cell — COGS nets back to the same number.
        if any(r["shipping"] for r in rows):
            return [], ("?? legacy-netted with nonzero Shipping -- the old scaling shrank that too "
                        "and this can't re-derive it; resolve by hand")
        if not cost_sum:
            return [], "?? legacy-netted but every cost is 0 -- can't apportion; resolve by hand"
        note = f"CONVERTED from legacy netting: cost -> gross, gift card {gift_card:.2f}{rewards_note}"
        for r in rows:
            gross = round(subtotal * r["cost"] / cost_sum, 2)
            unit = round(gross / r["quantity"], 2) if r["quantity"] else gross
            total = round(unit * (r["quantity"] or 1), 2)
            writes.append({"n": r["n"], "field": "cost_per_item", "value": unit, "expect": True})
            writes.append({"n": r["n"], "field": "total_cost", "value": total, "expect": True})
        # The gift-card/tax shares below weight by the GROSS costs just derived.
        rows = [dict(r, cost=round(subtotal * r["cost"] / cost_sum, 2)) for r in rows]
        cost_sum = sum(r["cost"] for r in rows)
        rewards = _rewards_writes(rows, rewards_used)

    for r in rows:
        weight = (r["cost"] / cost_sum) if cost_sum else (1 / len(rows))
        if r["gc_blank"]:
            writes.append({"n": r["n"], "field": "gift_card",
                           "value": round(gift_card * weight, 2), "expect": None})
        if sales_tax is not None and r["tax_blank"]:
            writes.append({"n": r["n"], "field": "sales_tax",
                           "value": round(sales_tax * weight, 2), "expect": None})
    return writes + rewards, note


def fetch_summaries(retailer: str, profile, order_ids: list[str]) -> dict[str, tuple]:
    """{order_id: (gift_card, sales_tax, subtotal, rewards_used)} read live from each order's
    details page (+ the rewards ledger / transactions page for an order that used points)."""
    import importlib

    mapping = importlib.import_module(_RETAILERS[retailer][1])
    api = importlib.import_module(_RETAILERS[retailer][2])
    from scrapers.amazon_signin import looks_logged_out
    from scrapers.cdp import CdpBrowser

    client_cls = getattr(api, "AmazonApiClient", None) or api.AmazonBusinessApiClient
    client = client_cls(profile)
    results: dict[str, tuple] = {}
    parsed: dict[str, tuple] = {}  # order -> (summary element, order region), resolved below
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
            parsed[oid] = (summary, region)
        # Amazon points never show on the order page. Business: the rewards ledger, loaded ONCE,
        # prices every points order (a redemption can be partial); the transactions page is the
        # fallback for an order the ledger does not list. Consumer: the transactions page only.
        points_orders = [oid for oid, (_, region) in parsed.items() if mapping.uses_points(region)]
        ledger: dict[str, float] = {}
        if points_orders and hasattr(mapping, "points_redeemed_by_order"):
            page.goto(mapping.REWARDS_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
            for _ in range(4):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(800)
            ledger = mapping.points_redeemed_by_order(page.content())
            print(f"  rewards ledger lists {len(ledger)} redemption(s)")
        for oid, (summary, region) in parsed.items():
            points = None
            if oid in points_orders:
                points = ledger.get(oid)
                if points is None:
                    page.goto(mapping.TRANSACTIONS_URL.format(oid), wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(3000)
                    points = mapping.points_used_from_transactions(page.content(), oid)
                print(f"  {oid}: paid with Amazon points -> "
                      f"{f'{points:.2f}' if points is not None else 'amount NOT readable (left blank)'}")
            results[oid] = (mapping._gift_card_amount(summary), mapping._sales_tax_amount(summary),
                            mapping._order_subtotal(summary),
                            mapping.rewards_used_amount(summary, region, points, oid))
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
        print("No Amazon orders with a blank Gift Card / Rewards Used cell match the filters. Nothing to do.")
        return 0

    col_of = {"gift_card": _col_letter(HEADER.index("Gift Card")),
              "sales_tax": _col_letter(HEADER.index("Sales Tax")),
              "rewards_used": _col_letter(HEADER.index("Rewards Used")),
              "cost_per_item": _col_letter(HEADER.index("Cost Per Item")),
              "total_cost": _col_letter(HEADER.index("Total Cost"))}
    idx_of = {f: HEADER.index(h) for f, h in (("gift_card", "Gift Card"), ("sales_tax", "Sales Tax"),
                                              ("rewards_used", "Rewards Used"),
                                              ("cost_per_item", "Cost Per Item"),
                                              ("total_cost", "Total Cost"))}

    all_writes: list[dict] = []
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
        for oid, (gift_card, sales_tax, subtotal, rewards_used) in sorted(summaries.items()):
            writes, note = plan_order_writes(orders[oid], gift_card, sales_tax, subtotal, rewards_used)
            no_gift_card += "no gift card" in note
            if not writes:
                print(f"  -- {oid}: {note}")
                continue
            print(f"  {oid}: {note}")
            for w in writes:
                w["note"] = oid
                all_writes.append(w)
                print(f"    {col_of[w['field']]}{w['n']}  <- {w['value']:>10.2f}")

    print(f"\n{len(all_writes)} cell(s) to write; {no_gift_card} order(s) had no gift card (zeros filled).")
    if not args.apply:
        print("DRY RUN -- nothing written. Re-run with --apply.")
        return 0
    if not all_writes:
        return 0

    # One batch per the guards: blank-only for gift_card/sales_tax/rewards_used; a conversion's cost
    # cells must still hold what the plan derived its gross numbers from (the netted value read above).
    live = worksheet.get_all_values()
    data, kept = [], 0
    stale_i = {n: (live[n - 1] if n - 1 < len(live) else []) for n in {w["n"] for w in all_writes}}
    for w in all_writes:
        row = stale_i[w["n"]]
        i = idx_of[w["field"]]
        current = str(row[i] if i < len(row) else "").strip()
        if w["expect"] is None and current:
            print(f"  {col_of[w['field']]}{w['n']} now holds {current!r}; left alone", file=sys.stderr)
            continue
        data.append({"range": f"{col_of[w['field']]}{w['n']}", "values": [[w["value"]]]})
        kept += 1
    if data:
        worksheet.batch_update(data, value_input_option="RAW")
    print(f"Wrote {kept} cell(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
