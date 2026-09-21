"""
One-off: backfill the Amazon order-page PROMO cashback and GIFT-CARD netting onto rows already on
the ledger.

Both values are read off the order-details page at scrape time (see scrapers/amazon_mapping.py): the
payment block's "... plus an extra 1% back ..." is added to the card's cards.json rate, and a "Gift
Card Amount" line scales the cost basis down to what the CARD actually paid. Neither is stored
anywhere else, and a delivered/paid/cancelled row is TERMINAL — never re-scraped — so every order
that closed before those rules existed still carries the pre-promo rate and the full sticker cost.
This script is that one-time correction (and the manual fix if a row is ever doubted).

It therefore has to RE-READ each order's page, which needs a browser — unlike
scripts/backfill_profit_columns.py, which resolves everything offline from cards.json. It uses the
deterministic CDP path (no AI agent): one cloud-browser session for the whole run, one page load per
order. That costs a small Browser-Use browser fee and touches the live Amazon account, but spends no
LLM tokens.

The rebuilt values come from the SAME production functions the scrapers use (`build_order_items` ->
`config.cards.tag_cards`), so this can never drift from what a fresh scrape would have written.

DRY RUN BY DEFAULT — reads the ledger, loads the order pages, and writes NOTHING:
    python -m scripts.backfill_amazon_promo
    python -m scripts.backfill_amazon_promo --limit 3          # only the 3 most recent orders
    python -m scripts.backfill_amazon_promo --order 111-9990017-9990017

Apply for real (backs the ledger up to data/ledger_backup_<timestamp>.csv FIRST):
    python -m scripts.backfill_amazon_promo --apply
"""

from scrapers.amazon_mapping import OrderPageShapeError
import argparse
import csv
import logging
from datetime import datetime, timezone
from pathlib import Path

from ledger_db.worksheet import ValueInputOption, ValueRenderOption

# This drives the cloud browser, and the Browser-Use SDK reads BROWSER_USE_API_KEY out of the
# ENVIRONMENT itself. Importing config.settings is what puts the config.json value there. It
# currently arrives transitively via ledger.sync, but stated explicitly so an import
# tidy-up somewhere else cannot quietly break this script with a valid config.
import config.settings  # noqa: F401
from config.cards import load_cards, tag_cards
from config.profiles import load_profiles_for_retailer
from models.card import parse_rate
from models.order import FIELDNAMES
from ledger.sync import HEADER, _col_letter, _get_worksheet

log = logging.getLogger("backfill_amazon_promo")

# Only these cells are ever touched. Everything else on the row is left exactly as it is — this
# script corrects money columns, it is not a re-scrape.
TARGET_FIELDS = ("cashback_rate", "promo_rate", "cost_per_item", "total_cost", "shipping")  # Promo Rate since 2026-09-20
_HEADER_FOR_FIELD = dict(zip(FIELDNAMES, HEADER))

# retailer -> (retailer key in profiles.json, the mapping module that parses its pages)
RETAILERS = {
    "Amazon": ("amazon", "scrapers.amazon_mapping"),
    "Amazon Business": ("amazon-business", "scrapers.amazon_business_mapping"),
}


def _money(value):
    """Cell -> float, or None when blank/unparseable. Unformatted reads give real numbers, but
    a hand-formatted cell can still arrive as "$449.00"."""
    if value is None:
        return None
    text = str(value).strip().replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _cell_number(field: str, value):
    """The cell read back as the number the model would hold.

    The rate column needs models.card.parse_rate rather than a plain float: that column is usually
    percent-FORMATTED, so the cell can read back as the text "6%" — which is 0.06, NOT 6.0. Getting
    this wrong reports every already-correct row as a change (the same trap
    backfill_profit_columns._same_rate guards against).
    """
    if field in ("cashback_rate", "promo_rate"):
        try:
            return parse_rate(value if isinstance(value, str) else str(value))
        except ValueError:
            return None
    return _money(value)


def _key(order_id, order_date, item_name, shipment) -> tuple:
    """The ledger's upsert key (ledger/sync.py: Order ID + Order Date + Item Name + Shipment).

    Shipment is normalized through int() where possible because the ledger stores it as a NUMBER, so
    an unformatted read can hand back 1 or 1.0 while the model holds "1" — three spellings of one
    shipment, and a mismatch here would silently match nothing and report "no changes".
    """
    ship = str(shipment).strip()
    try:
        ship = str(int(float(ship)))
    except (TypeError, ValueError):
        pass
    return (str(order_id).strip(), str(order_date).strip(), str(item_name).strip(), ship)


def plan_amazon_backfill(header: list[str], data_rows: list[list], rebuilt: dict) -> dict:
    """Read-only: which cells would change. Pure — no network, no ledger access, so it is unit-testable.

    `rebuilt` maps the upsert key to the OrderItem a fresh scrape would produce today (already
    promo-tagged and gift-card netted). Returns
    {"changes": [(row_number, column, old, new)], "unmatched": [key, ...], "orders": [order_id, ...]}.

    A rebuilt value of None means "the page didn't say" — those are SKIPPED rather than written,
    mirroring ledger_sync._merge_row's rule that a blank never overwrites recorded data.
    """
    idx = {name: header.index(name) for name in header}

    def cell(row, name):
        i = idx.get(name)
        return row[i] if i is not None and i < len(row) else ""

    changes: list[tuple] = []
    touched_orders: set = set()
    seen_keys: set = set()

    for offset, row in enumerate(data_rows):
        order_id = str(cell(row, "Order ID")).strip()
        if not order_id:
            continue
        key = _key(order_id, cell(row, "Order Date"), cell(row, "Item Name"), cell(row, "Shipment"))
        seen_keys.add(key)
        item = rebuilt.get(key)
        if item is None:
            continue
        row_number = offset + 2  # +1 for the header row, +1 because rows are 1-based
        for field in TARGET_FIELDS:
            new = getattr(item, field, None)
            if new is None:
                continue
            column = _HEADER_FOR_FIELD[field]
            old = _cell_number(field, cell(row, column))
            if old is not None and abs(old - float(new)) < 1e-9:
                continue
            changes.append((row_number, column, old, round(float(new), 4)))
            touched_orders.add(order_id)

    return {
        "changes": changes,
        "unmatched": sorted(set(rebuilt) - seen_keys),
        "orders": sorted(touched_orders),
    }


def rebuild_orders(retailer: str, profile_label: str, order_ids: list[str], cards) -> dict:
    """Load each order's details page over CDP and rebuild its rows the way a scrape would.

    Returns {upsert_key: OrderItem}. Imports are local so this module stays importable — and
    plan_amazon_backfill stays testable — without playwright or a browser installed.
    """
    from importlib import import_module

    from scrapers.amazon_api import ORDER_DETAILS_URL, _looks_logged_out
    from scrapers.cdp import CdpBrowser

    retailer_key, mapping_path = RETAILERS[retailer]
    build_order_items = import_module(mapping_path).build_order_items

    profiles = [p for p in load_profiles_for_retailer(retailer_key)
                if not profile_label or p.label == profile_label]
    if not profiles:
        raise SystemExit(
            f"No profile in profiles.json handles {retailer!r} (label={profile_label!r})."
        )
    profile = profiles[0]

    rebuilt: dict = {}
    with CdpBrowser(profile) as page:
        for oid in order_ids:
            page.goto(ORDER_DETAILS_URL.format(oid), wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
            if _looks_logged_out(page):
                raise SystemExit(
                    f"{retailer} session is logged out (profile {profile.label}). Log in on that "
                    "profile and re-run; this script never attempts a login."
                )
            # known_open_ids keeps a cancelled order's rows rather than dropping them at discovery.
            try:
                rows = build_order_items(page.content(), profile.label, known_open_ids={oid})
            except OrderPageShapeError as exc:
                # 2026-09-19: the mapping raises on a page it cannot identify (no order id, no
                # order date, no shipment cards) instead of returning []; here that is one skipped
                # order, said out loud, not a dead script.
                log.warning("%s %s: page shape changed, skipped (%s)", retailer, oid, exc)
                continue
            if not rows:
                log.warning("%s %s: no rows parsed (page shape changed?)", retailer, oid)
                continue
            tag_cards(rows, cards)
            for r in rows:
                rebuilt[_key(r.order_id, r.order_date, r.item_name, r.shipment)] = r
            log.info("%s %s: rebuilt %d row(s), promo=%s, rate=%s", retailer, oid, len(rows),
                     rows[0].promo_rate, rows[0].cashback_rate)
    return rebuilt


def _print_plan(plan: dict, apply: bool) -> None:
    changes = plan["changes"]
    if not changes:
        print("\nNothing to change — every matched row already holds what a fresh scrape would write.")
    else:
        print(f"\n{len(changes)} cell(s) across {len(plan['orders'])} order(s) would change:")
        for row_number, column, old, new in changes:
            shown_old = "(blank)" if old is None else f"{old:g}"
            print(f"  row {row_number:>4}  {column:<16} {shown_old:>10}  ->  {new:g}")
    for key in plan["unmatched"]:
        print(f"  NOTE: rebuilt row not found on the ledger: {key}")
    if not apply:
        print("\nDry run only — nothing written. Re-run with --apply to make these changes.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--apply", action="store_true",
                        help="Actually write the cells (default: dry run, read-only)")
    parser.add_argument("--order", action="append", default=[],
                        help="Only this order id (repeatable)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Only the N most recent orders per profile (0 = all)")
    parser.add_argument("--retailer", default="", choices=["", *RETAILERS],
                        help="Restrict to one retailer")
    args = parser.parse_args()

    worksheet = _get_worksheet()
    existing = worksheet.get_values(value_render_option=ValueRenderOption.unformatted)
    if not existing or not any(str(c).strip() for c in existing[0]):
        raise SystemExit("Ledger is empty — nothing to backfill.")
    header = [str(c) for c in existing[0]]
    if header != list(HEADER):
        raise SystemExit(
            "The ledger's header doesn't match the current schema, so this would target the wrong "
            f"columns (the file migrates its own columns on open, so this should not happen).\n"
            f"  ledger:   {header}\n  expected: {list(HEADER)}"
        )

    idx = {name: header.index(name) for name in header}

    def cell(row, name):
        i = idx[name]
        return row[i] if i < len(row) else ""

    # (retailer, profile) -> {order_id: order date}, so each browser session covers one profile.
    groups: dict = {}
    for row in existing[1:]:
        retailer = str(cell(row, "Retailer")).strip()
        order_id = str(cell(row, "Order ID")).strip()
        if retailer not in RETAILERS or not order_id:
            continue
        if args.retailer and retailer != args.retailer:
            continue
        if args.order and order_id not in args.order:
            continue
        groups.setdefault((retailer, str(cell(row, "Profile")).strip()), {})[order_id] = str(
            cell(row, "Order Date")
        )

    if not groups:
        raise SystemExit("No Amazon / Amazon Business rows matched — nothing to do.")

    cards = load_cards()
    rebuilt: dict = {}
    for (retailer, profile_label), orders in sorted(groups.items()):
        ids = [oid for oid, _ in sorted(orders.items(), key=lambda kv: kv[1], reverse=True)]
        if args.limit:
            ids = ids[: args.limit]
        print(f"Re-reading {len(ids)} {retailer} order(s) on profile {profile_label or '(any)'}...")
        rebuilt.update(rebuild_orders(retailer, profile_label, ids, cards))

    plan = plan_amazon_backfill(header, existing[1:], rebuilt)
    _print_plan(plan, args.apply)
    if not args.apply or not plan["changes"]:
        return

    backup_dir = Path("data")
    backup_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"ledger_backup_{stamp}.csv"
    with backup_path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(existing)
    print(f"\nBacked the whole ledger up -> {backup_path}")

    data = [{"range": f"{_col_letter(header.index(column))}{row_number}", "values": [[new]]}
            for row_number, column, _old, new in plan["changes"]]
    worksheet.batch_update(data, value_input_option=ValueInputOption.raw)
    print(f"Wrote {len(data)} cell(s).")
    print("\nTotal Profit recalculates itself — it is a live formula over these columns.")
    print("Verify with `python -m scripts.audit_ledger`.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    main()
