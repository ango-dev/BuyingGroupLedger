"""
One-off repair: remove ledger rows left behind by a SUPERSEDED tracking number.

Amazon re-issues a new tracking number for the SAME physical shipment when one is delayed. While that
is in flight the order-details page can render the package twice, and because shipments are numbered by
DOM position the replacement lands as a brand-new "Shipment N+1" row carrying the full quantity and cost
again — the order's cost is then booked twice. Seen live on 111-9990021-9990021: two rows of 3 iPads
against a $2,847 order, booking $5,694.

`scrapers/amazon_mapping._reconcile_against_subtotal` stops NEW ones (an order's shipment cards may not
be worth more than its subtotal). This script cleans up rows written before that guard existed.

HOW IT DECIDES: it re-reads the order's page and its tracking pages, which together are the only
authority on what packages actually exist, and matches sheet rows on TRACKING NUMBER — the one durable
identity a row has (the Shipment number is a DOM ordinal that is recomputed every scrape). A row whose
tracking number no longer appears on the order is superseded and gets deleted; survivors are renumbered
to the page's own ordering so future scrapes match them. A row with a BLANK tracking number is never
deleted — it cannot be matched, so it is reported and left alone.

Costs a small Browser-Use browser fee (one CDP session, one page load per order plus one per shipment)
and spends no LLM tokens.

DRY RUN BY DEFAULT — reads the live sheet and the order pages, and writes NOTHING:
    python -m scripts.fix_superseded_shipments --order 111-9990021-9990021

Apply for real (backs the sheet up to data/sheet_backup_<timestamp>.csv FIRST):
    python -m scripts.fix_superseded_shipments --order 111-9990021-9990021 --apply
"""

import argparse
import csv
import logging
from datetime import datetime, timezone
from pathlib import Path

from gspread.utils import ValueInputOption, ValueRenderOption

from models.order import shipment_label
from sheets.ledger_sync import HEADER, _col_letter, _get_worksheet, _write_profit_formulas

log = logging.getLogger("fix_superseded_shipments")

# retailer -> (profiles.json key, mapping module, scraper class holding the pt-page reader)
RETAILERS = {
    "Amazon": ("amazon", "scrapers.amazon_mapping", "scrapers.amazon", "AmazonScraper"),
    "Amazon Business": ("amazon-business", "scrapers.amazon_business_mapping",
                        "scrapers.amazon_business", "AmazonBusinessScraper"),
}


def plan_supersede_fix(header: list[str], data_rows: list[list], live_by_order: dict) -> dict:
    """Read-only: which rows would be deleted and which renumbered. Pure — no network, no sheet.

    `live_by_order` maps an order id to the tracking numbers the page currently shows, IN PAGE ORDER.
    An order missing from it is skipped entirely (nothing was read for it), which is what keeps a
    failed page load from ever looking like "every row is superseded".
    """
    idx = {name: header.index(name) for name in header}

    def cell(row, name):
        i = idx.get(name)
        return str(row[i]).strip() if i is not None and i < len(row) else ""

    deletions, renumbers, blanks = [], [], []
    for offset, row in enumerate(data_rows):
        order_id = cell(row, "Order ID")
        if order_id not in live_by_order:
            continue
        live = live_by_order[order_id]
        row_number = offset + 2  # +1 header, +1 for 1-based sheet rows
        tracking = cell(row, "Tracking Number")
        item = cell(row, "Item Name")
        if not tracking:
            # Unmatchable, so never deleted: it may be a not-yet-shipped box.
            blanks.append((row_number, order_id, cell(row, "Shipment"), item))
            continue
        if tracking not in live:
            deletions.append((row_number, order_id, cell(row, "Shipment"), tracking, item))
            continue
        want = shipment_label(live.index(tracking) + 1)
        if cell(row, "Shipment") != want:
            renumbers.append((row_number, order_id, cell(row, "Shipment"), want))

    return {
        "deletions": deletions,
        "renumbers": renumbers,
        "blank_tracking": blanks,
        "orders": sorted({d[1] for d in deletions} | {r[1] for r in renumbers}),
    }


def read_live_shipments(retailer: str, profile_label: str, order_ids: list[str]) -> dict:
    """{order_id: [tracking number, ...]} in page order, read from the live order + tracking pages.

    Imports are local so the module stays importable — and plan_supersede_fix stays testable — without
    playwright or a browser.
    """
    from importlib import import_module

    from config.profiles import load_profiles_for_retailer
    from scrapers.amazon_api import ORDER_DETAILS_URL, _looks_logged_out
    from scrapers.cdp import CdpBrowser

    retailer_key, mapping_path, scraper_path, scraper_name = RETAILERS[retailer]
    parse_shipment_targets = import_module(mapping_path).parse_shipment_targets

    profiles = [p for p in load_profiles_for_retailer(retailer_key)
                if not profile_label or p.label == profile_label]
    if not profiles:
        raise SystemExit(f"No profile in profiles.json handles {retailer!r} (label={profile_label!r}).")
    profile = profiles[0]
    reader = getattr(import_module(scraper_path), scraper_name)(profile).read_tracking_page

    live: dict = {}
    with CdpBrowser(profile) as page:
        for oid in order_ids:
            page.goto(ORDER_DETAILS_URL.format(oid), wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
            if _looks_logged_out(page):
                raise SystemExit(
                    f"{retailer} session is logged out (profile {profile.label}). Log in on that "
                    "profile and re-run; this script never attempts a login."
                )
            targets = parse_shipment_targets(page.content())
            numbers = []
            for target in targets:
                if not target.get("tracking_url"):
                    numbers.append("")
                    continue
                page.goto(target["tracking_url"], wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2500)
                info = reader(page)
                numbers.append((info or {}).get("tracking_number", ""))
            # Only orders we could actually read get an entry — see plan_supersede_fix.
            live[oid] = [n for n in numbers if n]
            log.info("%s %s: page shows %d shipment(s) -> %s", retailer, oid, len(targets), live[oid])
    return live


def _print_plan(plan: dict, apply: bool) -> None:
    if not plan["deletions"] and not plan["renumbers"]:
        print("\nNothing to change — every row matches a shipment the order still shows.")
    for row_number, oid, shipment, tracking, item in plan["deletions"]:
        print(f"  DELETE   row {row_number:>4}  {oid}  shipment {shipment}  {tracking}  {item[:36]}")
    for row_number, oid, old, new in plan["renumbers"]:
        print(f"  RENUMBER row {row_number:>4}  {oid}  shipment {old} -> {new}")
    for row_number, oid, shipment, item in plan["blank_tracking"]:
        print(f"  (left alone: row {row_number} {oid} shipment {shipment} has no tracking number)")
    if not apply:
        print("\nDry run only — nothing written. Re-run with --apply to make these changes.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--order", action="append", required=True,
                        help="Order id to reconcile (repeatable)")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write/delete on the live sheet (default: dry run, read-only)")
    args = parser.parse_args()

    worksheet = _get_worksheet()
    existing = worksheet.get_values(value_render_option=ValueRenderOption.unformatted)
    if not existing or not any(str(c).strip() for c in existing[0]):
        raise SystemExit("Sheet is empty — nothing to fix.")
    header = [str(c) for c in existing[0]]
    if header != list(HEADER):
        raise SystemExit(
            "Sheet header doesn't match the current schema, so this would target the wrong columns. "
            f"Run `python -m scripts.reorder_sheet` first.\n  sheet:    {header}\n"
            f"  expected: {list(HEADER)}"
        )

    idx = {name: header.index(name) for name in header}
    wanted = set(args.order)
    groups: dict = {}
    for row in existing[1:]:
        get = lambda name: str(row[idx[name]]).strip() if idx[name] < len(row) else ""  # noqa: E731
        if get("Order ID") in wanted and get("Retailer") in RETAILERS:
            groups.setdefault((get("Retailer"), get("Profile")), set()).add(get("Order ID"))
    if not groups:
        raise SystemExit(f"None of {sorted(wanted)} are Amazon / Amazon Business rows on the sheet.")

    live_by_order: dict = {}
    for (retailer, profile_label), ids in sorted(groups.items()):
        print(f"Re-reading {len(ids)} {retailer} order(s) on profile {profile_label or '(any)'}...")
        live_by_order.update(read_live_shipments(retailer, profile_label, sorted(ids)))

    plan = plan_supersede_fix(header, existing[1:], live_by_order)
    _print_plan(plan, args.apply)
    if not args.apply or not (plan["deletions"] or plan["renumbers"]):
        return

    backup_dir = Path("data")
    backup_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"sheet_backup_{stamp}.csv"
    with backup_path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(existing)
    print(f"\nBacked the whole sheet up -> {backup_path}")

    # RENUMBERS FIRST: a cell write never moves a row, so the original row numbers stay valid no matter
    # what is deleted afterwards. Deleting first would invalidate any renumber below a deleted row.
    if plan["renumbers"]:
        col = _col_letter(header.index("Shipment"))
        for row_number, _oid, _old, new in plan["renumbers"]:
            worksheet.update(range_name=f"{col}{row_number}", values=[[int(new)]],
                             value_input_option=ValueInputOption.raw)
        print(f"Renumbered {len(plan['renumbers'])} shipment cell(s).")

    if plan["deletions"]:
        # Bottom-to-top by original row number: deleting the highest row only shifts rows below it,
        # none of which remain to process.
        for row_number, *_ in sorted(plan["deletions"], key=lambda t: t[0], reverse=True):
            worksheet.delete_rows(row_number)
        print(f"Deleted {len(plan['deletions'])} superseded row(s).")

        # MANDATORY after a delete, not tidy: Total Profit uses same-row relative references, so every
        # row that shifted up now carries a formula pointing at its OLD position.
        # audit_sheet.profit_formula_literal is the tripwire for getting this wrong.
        remaining = len(existing) - 1 - len(plan["deletions"])
        _write_profit_formulas(worksheet, list(range(2, remaining + 2)))
        print(f"Re-stamped the Total Profit formula on {remaining} row(s).")

    print("\nDone. Verify with `python -m scripts.audit_sheet`.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    main()
