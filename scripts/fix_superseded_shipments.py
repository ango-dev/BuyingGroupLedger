"""
Repair for a SUPERSEDED tracking number: mark the dead row `superseded` (default), or delete it.

Amazon re-issues a new tracking number for the SAME physical shipment when one is delayed. While that
is in flight the order-details page can render the package twice, and because shipments are numbered by
DOM position the replacement lands as a brand-new "Shipment N+1" row carrying the full quantity and cost
again — the order's cost is then booked twice. Seen live on 111-9990021-9990021: two rows of 3 iPads
against a $2,847 order, booking $5,694.

`scrapers/amazon_mapping._reconcile_against_subtotal` stops NEW ones (an order's shipment cards may not
be worth more than its subtotal). This script repairs rows written before that guard existed, or left
behind by a re-label the guard could not see (the dead card had already left the page).

HOW IT DECIDES: it re-reads the order's page and its tracking pages, which together are the only
authority on what packages actually exist, and matches sheet rows on the PACKAGE ID when the row has
one (Amazon's shipmentId / Best Buy's groupId — column 33 since 2026-09-10, beside Card Last 4), then on TRACKING NUMBER
(the Shipment number is a DOM ordinal that is recomputed every scrape). A row whose tracking number no
longer appears on the order AND whose package id (if any) is not on the page either is superseded.
A row whose number is gone but whose package id is still on the page is a RE-LABELLED package, not a
dead one: it is reported (`RELABELLED`) and never marked or deleted — the sync's own re-label rule
owns that row and will carry the new number onto it. Survivors are renumbered to the page's own
ordering so future scrapes match them. A row with a BLANK tracking number is never touched — it cannot
be matched, so it is reported and left alone.

WHAT HAPPENS TO THE DEAD ROW:

  MARK (the default). The row STAYS, as the record that this number really was posted to the buying
  group: Status -> `superseded`, every money cell blanked (Quantity included — see
  ledger_sync._SUPERSEDED_BLANK_FIELDS), and its Shipment renumbered AFTER the live packages so the
  row's key can never collide with a real box. Tracking number, dates, card, rate and the
  Tracking Submitted tick are kept. `superseded` is terminal and RETIRED: never re-scraped, never a
  merge target, never submitted/insured/paid (sync_tracking skips it). REFUSED, before anything is
  written, for a dead row that already carries money from a group (Actual Payout / Payout Date /
  Insurance non-blank) or whose status is not ordered/shipped/delivered — money exchanged on that
  number is a human decision, not something to blank.

  DELETE (`--delete`). The old behaviour: the row is removed and every remaining row's formulas are
  re-stamped (they use same-row references). Its only trace is the backup CSV.

  RESTORE (`--restore-from <backup.csv>`). Puts a row DELETED by an earlier run back as a superseded
  row, read from that run's backup by column NAME (older backups have fewer columns): needs no
  browser. The row is appended at the bottom — run `python -m scripts.sort_ledger --apply` after.

Costs a small Browser-Use browser fee (one CDP session, one page load per order plus one per shipment)
and spends no LLM tokens; `--restore-from` costs nothing.

DRY RUN BY DEFAULT — reads the live sheet and the order pages, and writes NOTHING:
    python -m scripts.fix_superseded_shipments --order 111-9990021-9990021
    python -m scripts.fix_superseded_shipments --order 111-9990021-9990021 --delete
    python -m scripts.fix_superseded_shipments --order 111-9990021-9990021 \\
        --restore-from data/ledger_backup_20260822T200318Z.csv

Apply for real (backs the sheet up to data/ledger_backup_<timestamp>.csv FIRST):
    python -m scripts.fix_superseded_shipments --order 111-9990021-9990021 --apply
"""

import argparse
import csv
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from ledger_db.worksheet import ValueInputOption, ValueRenderOption

# This drives the cloud browser, and the Browser-Use SDK reads BROWSER_USE_API_KEY out of the
# ENVIRONMENT itself. Importing config.settings is what puts the config.json value there. It
# currently arrives transitively via ledger.sync, but stated explicitly so an import
# tidy-up somewhere else cannot quietly break this script with a valid config.
import config.settings  # noqa: F401
from models.order import FIELDNAMES, shipment_label
from ledger.sync import (
    HEADER,
    _SUPERSEDED_BLANK_FIELDS,
    _clear_cells,
    _coerce,
    _col_letter,
    _ensure_grid_rows,
    _get_worksheet,
    _last_occupied_row,
    _write_profit_formulas,
)

log = logging.getLogger("fix_superseded_shipments")

# retailer -> (profiles.json key, mapping module, scraper class holding the pt-page reader).
# Best Buy has no page to parse: its ss-api payload names every package's number directly, so it
# is read through the API client instead (see read_live_shipments).
RETAILERS = {
    "Amazon": ("amazon", "scrapers.amazon_mapping", "scrapers.amazon", "AmazonScraper"),
    "Amazon Business": ("amazon-business", "scrapers.amazon_business_mapping",
                        "scrapers.amazon_business", "AmazonBusinessScraper"),
    "Best Buy": ("bestbuy", None, None, None),
}

SUPERSEDED = "superseded"
#: The only statuses a dead row may hold to be marked: a live lifecycle state, or delivered under the
#: dead label before the re-label was noticed. paid/return/cancelled mean a group (or the retailer)
#: already acted on the row, and that is a hand decision.
_MARKABLE_STATUSES = ("ordered", "shipped", "delivered")
#: Money from a BUYING GROUP on the dead number. Blanking it would erase a real settlement.
_SETTLED_COLUMNS = ("Actual Payout", "Payout Date", "Insurance")
#: The money cells a marked / restored row loses, by sheet header name.
_BLANKED_COLUMNS = tuple(HEADER[FIELDNAMES.index(f)] for f in _SUPERSEDED_BLANK_FIELDS)
_SHIPMENT_NUMBER = re.compile(r"(?:shipment\s*)?(\d+)", re.IGNORECASE)


def _shipment_int(text) -> int:
    m = _SHIPMENT_NUMBER.match(str(text or "").strip())
    return int(m.group(1)) if m else 0


def _cell_reader(header: list[str]):
    idx = {name: header.index(name) for name in header}

    def cell(row, name):
        i = idx.get(name)
        return str(row[i]).strip() if i is not None and i < len(row) else ""

    return cell


def _refusal(cell, row) -> str | None:
    """Why this dead row must NOT be marked, or None."""
    status = cell(row, "Status").lower()
    if status not in _MARKABLE_STATUSES:
        return f"status {status or '(blank)'!r} is not one of {_MARKABLE_STATUSES}"
    for column in _SETTLED_COLUMNS:
        value = cell(row, column)
        if value not in ("", "0", "0.0"):
            return f"{column} holds {value!r} -- money from a buying group, a hand decision"
    return None


def _live_entries(entries: list) -> list[dict]:
    """Normalise one order's live packages to [{"tracking", "package_id"}, ...] in page order.

    Accepts the original shape (a list of tracking-number strings) and the current one (dicts that
    also carry the page's package id). An entry with neither a number nor an id is unreadable and is
    dropped, exactly as blank numbers always were.
    """
    out = []
    for entry in entries:
        if isinstance(entry, dict):
            tracking = str(entry.get("tracking", "") or "").strip()
            package_id = str(entry.get("package_id", "") or "").strip()
        else:
            tracking, package_id = str(entry or "").strip(), ""
        if tracking or package_id:
            out.append({"tracking": tracking, "package_id": package_id})
    return out


def plan_supersede_fix(header: list[str], data_rows: list[list], live_by_order: dict,
                       mode: str = "mark") -> dict:
    """Read-only: which rows would be marked (or deleted) and which renumbered. Pure — no network.

    `live_by_order` maps an order id to the packages the page currently shows, IN PAGE ORDER — either
    plain tracking numbers or `{"tracking", "package_id"}` dicts (see _live_entries). An order missing
    from it is skipped entirely (nothing was read for it), which is what keeps a failed page load
    from ever looking like "every row is superseded".

    A row is LIVE when its tracking number is on the page, or — failing that — when its Package ID
    is. The second case is a re-labelled package (same id, new number): it is reported under
    "relabelled", renumbered to its page position like any live row, and never marked or deleted.

    `mode` is "mark" (keep the dead row as `superseded`, the default) or "delete" (the old repair).
    A dead row that is ALREADY superseded is reported and never touched in either mode; a dead row
    the mark rule refuses (see _refusal) is reported under "refused" and blocks an --apply.
    """
    cell = _cell_reader(header)
    marks, deletions, renumbers, blanks, refused, already, relabelled = [], [], [], [], [], [], []
    live_entries = {oid: _live_entries(entries) for oid, entries in live_by_order.items()}

    # A second re-label of the same order must number PAST the row the first one retired.
    taken: dict[str, int] = {}
    for row in data_rows:
        if cell(row, "Order ID") in live_by_order and cell(row, "Status").lower() == SUPERSEDED:
            taken[cell(row, "Order ID")] = max(taken.get(cell(row, "Order ID"), 0),
                                              _shipment_int(cell(row, "Shipment")))
    assigned: dict[tuple[str, str], str] = {}  # one new number per dead carton, however many SKUs

    for offset, row in enumerate(data_rows):
        order_id = cell(row, "Order ID")
        if order_id not in live_by_order:
            continue
        live = live_entries[order_id]
        live_numbers = [e["tracking"] for e in live]
        live_ids = [e["package_id"] for e in live]
        row_number = offset + 2  # +1 header, +1 for 1-based sheet rows
        tracking = cell(row, "Tracking Number")
        package_id = cell(row, "Package ID")
        shipment = cell(row, "Shipment")
        item = cell(row, "Item Name")
        if not tracking:
            # Unmatchable, so never touched: it may be a not-yet-shipped box.
            blanks.append((row_number, order_id, shipment, item))
            continue
        if cell(row, "Status").lower() == SUPERSEDED:
            already.append((row_number, order_id, shipment, tracking))
            continue
        if tracking in live_numbers:
            position = live_numbers.index(tracking)
        elif package_id and package_id in live_ids:
            position = live_ids.index(package_id)
            relabelled.append((row_number, order_id, shipment, tracking, live[position]["tracking"]))
        else:
            if mode == "delete":
                deletions.append((row_number, order_id, shipment, tracking, item))
                continue
            reason = _refusal(cell, row)
            if reason:
                refused.append((row_number, order_id, tracking, reason))
                continue
            if (order_id, tracking) not in assigned:
                taken[order_id] = max(len(live), taken.get(order_id, 0)) + 1
                assigned[(order_id, tracking)] = shipment_label(taken[order_id])
            marks.append((row_number, order_id, shipment, tracking, item, assigned[(order_id, tracking)]))
            continue
        want = shipment_label(position + 1)
        if shipment != want:
            renumbers.append((row_number, order_id, shipment, want))

    return {
        "marks": marks,
        "deletions": deletions,
        "renumbers": renumbers,
        "blank_tracking": blanks,
        "refused": refused,
        "already_superseded": already,
        "relabelled": relabelled,
        "orders": sorted({m[1] for m in marks} | {d[1] for d in deletions} | {r[1] for r in renumbers}),
    }


def plan_restore(header: list[str], data_rows: list[list], backup_header: list[str],
                 backup_rows: list[list], order_id: str) -> dict:
    """Read-only: the backup rows of `order_id` whose tracking number is no longer on the sheet,
    rebuilt as superseded rows in today's column order. Pure — no network, no sheet.

    Remapped by column NAME, so a backup taken before a column was appended still restores (the
    missing cells come back blank). Money cells are blanked and the Shipment is numbered after
    every number the order already holds, exactly as a fresh mark would be.
    """
    cell, bcell = _cell_reader(header), _cell_reader(backup_header)
    present, highest = set(), 0
    for row in data_rows:
        if cell(row, "Order ID") == order_id:
            present.add(cell(row, "Tracking Number"))
            highest = max(highest, _shipment_int(cell(row, "Shipment")))
    appends, skipped = [], []
    assigned: dict[str, str] = {}
    for brow in backup_rows:
        if bcell(brow, "Order ID") != order_id:
            continue
        tracking = bcell(brow, "Tracking Number")
        if not tracking:
            skipped.append((tracking, "no tracking number in the backup row"))
            continue
        if tracking in present:
            skipped.append((tracking, "already on the sheet"))
            continue
        if tracking not in assigned:
            highest += 1
            assigned[tracking] = shipment_label(highest)
        new = [bcell(brow, name) if name in backup_header else "" for name in header]
        new[header.index("Status")] = SUPERSEDED
        new[header.index("Shipment")] = assigned[tracking]
        for column in _BLANKED_COLUMNS:
            new[header.index(column)] = ""
        new = [_coerce(field, value) for field, value in zip(FIELDNAMES, new)]
        appends.append((tracking, new))
    return {"appends": appends, "skipped": skipped}


def read_live_shipments(retailer: str, profile_label: str, order_ids: list[str]) -> dict:
    """{order_id: [{"tracking", "package_id"}, ...]} in page order, read from the live order + tracking
    pages. `package_id` is the card's shipmentId (see parse_shipment_targets), blank when the card has
    none; `tracking` is blank when the card has no track link or its pt page could not be read.

    Imports are local so the module stays importable — and plan_supersede_fix stays testable — without
    playwright or a browser.
    """
    from importlib import import_module

    from config.profiles import load_profiles_for_retailer
    from scrapers.amazon_api import ORDER_DETAILS_URL, _looks_logged_out
    from scrapers.cdp import CdpBrowser

    retailer_key, mapping_path, scraper_path, scraper_name = RETAILERS[retailer]

    profiles = [p for p in load_profiles_for_retailer(retailer_key)
                if not profile_label or p.label == profile_label]
    if not profiles:
        raise SystemExit(f"No profile in profiles.json handles {retailer!r} (label={profile_label!r}).")
    profile = profiles[0]
    if retailer == "Best Buy":
        return _read_live_bestbuy(profile, order_ids)

    parse_shipment_targets = import_module(mapping_path).parse_shipment_targets
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
            entries = []
            for target in targets:
                number = ""
                if target.get("tracking_url"):
                    page.goto(target["tracking_url"], wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(2500)
                    info = reader(page)
                    number = (info or {}).get("tracking_number", "")
                entries.append({"tracking": number, "package_id": target.get("shipmentId", "")})
            # Only orders we could actually read get an entry — see plan_supersede_fix.
            live[oid] = [e for e in entries if e["tracking"] or e["package_id"]]
            log.info("%s %s: page shows %d shipment(s) -> %s", retailer, oid, len(targets),
                     [(e["tracking"], e["package_id"]) for e in live[oid]])
    return live


def _read_live_bestbuy(profile, order_ids: list[str]) -> dict:
    """{order_id: [{"tracking", "package_id"}, ...]} for Best Buy, straight from the ss-api payloads.

    The production client fetches "every order since `since_date` plus every still-open id", so
    asking for today plus the wanted ids returns exactly those orders (and any placed today, which
    the planner ignores as unrequested). The mapping then names every package's number in row order —
    the same rows a scrape would write — and an order the API did not return stays out of the
    result, so the planner leaves it alone.
    """
    from datetime import date

    from scrapers.bestbuy_api import BestBuyApiClient
    from scrapers.bestbuy_mapping import build_order_items

    payloads = BestBuyApiClient(profile).fetch_order_payloads(
        date.today().isoformat(), open_ids=set(order_ids), terminal_ids=set(),
    )
    rows = build_order_items(payloads, profile.label, known_open_ids=frozenset(order_ids))
    live: dict = {}
    for oid in order_ids:
        # One entry per package (a multi-SKU carton is several rows sharing number AND groupId).
        packages = dict.fromkeys(
            (r.tracking_number, r.package_id) for r in rows
            if r.order_id == oid and (r.tracking_number or r.package_id)
        )
        if any(r.order_id == oid for r in rows):
            live[oid] = [{"tracking": t, "package_id": p} for t, p in packages]
            log.info("Best Buy %s: API shows %d package(s) -> %s", oid, len(live[oid]), list(packages))
    return live


def _print_plan(plan: dict, apply: bool) -> None:
    if not (plan["marks"] or plan["deletions"] or plan["renumbers"] or plan["refused"]):
        print("\nNothing to change — every row matches a shipment the order still shows.")
    for row_number, oid, shipment, tracking, item, new in plan["marks"]:
        print(f"  MARK     row {row_number:>4}  {oid}  shipment {shipment} -> {new}  {tracking}  "
              f"{item[:30]}  -> status superseded, money blanked")
    for row_number, oid, shipment, tracking, item in plan["deletions"]:
        print(f"  DELETE   row {row_number:>4}  {oid}  shipment {shipment}  {tracking}  {item[:36]}")
    for row_number, oid, old, new in plan["renumbers"]:
        print(f"  RENUMBER row {row_number:>4}  {oid}  shipment {old} -> {new}")
    for row_number, oid, tracking, reason in plan["refused"]:
        print(f"  REFUSED  row {row_number:>4}  {oid}  {tracking}: {reason}")
    for row_number, oid, shipment, tracking in plan["already_superseded"]:
        print(f"  (already superseded: row {row_number} {oid} shipment {shipment} {tracking})")
    for row_number, oid, shipment, tracking, live_number in plan.get("relabelled", []):
        print(f"  RELABELLED row {row_number:>4}  {oid}  shipment {shipment}  {tracking} -> "
              f"{live_number or '(number not read)'}: same package id, new number -- left to the "
              f"sync's re-label rule, not marked")
    for row_number, oid, shipment, item in plan["blank_tracking"]:
        print(f"  (left alone: row {row_number} {oid} shipment {shipment} has no tracking number)")
    if not apply:
        print("\nDry run only — nothing written. Re-run with --apply to make these changes.")


def _backup(existing: list[list]) -> Path:
    backup_dir = Path("data")
    backup_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"ledger_backup_{stamp}.csv"
    with backup_path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(existing)
    print(f"\nBacked the whole sheet up -> {backup_path}")
    return backup_path


def _read_sheet(worksheet) -> tuple[list[str], list[list]]:
    existing = worksheet.get_values(value_render_option=ValueRenderOption.unformatted)
    if not existing or not any(str(c).strip() for c in existing[0]):
        raise SystemExit("Sheet is empty — nothing to fix.")
    header = [str(c) for c in existing[0]]
    if header != list(HEADER):
        raise SystemExit(
            "The ledger's header doesn't match the current schema, so this would target the wrong "
            f"columns (the file migrates its own columns on open, so this should not happen).\n"
            f"  ledger:   {header}\n  expected: {list(HEADER)}"
        )
    return header, existing


def _apply_marks(worksheet, header: list[str], plan: dict) -> None:
    """Status first, then Shipment, then the money — so a partial failure leaves a row the audit's
    superseded_rows_carry_no_money check flags, never a quiet `shipped` row with no cost."""
    status_col = _col_letter(header.index("Status"))
    shipment_col = _col_letter(header.index("Shipment"))
    rows = [m[0] for m in plan["marks"]]
    for row_number, _oid, _old, _tracking, _item, new in plan["marks"]:
        worksheet.update(range_name=f"{status_col}{row_number}", values=[[SUPERSEDED]],
                         value_input_option=ValueInputOption.raw)
        worksheet.update(range_name=f"{shipment_col}{row_number}", values=[[int(new)]],
                         value_input_option=ValueInputOption.raw)
    _clear_cells(worksheet, rows, _SUPERSEDED_BLANK_FIELDS)  # raises: a half-marked row must be loud
    # The dropdown tripwire: a Status data-validation rule that rejects unknown values would leave
    # the old status in place while the money is already gone. Read back and refuse to stay quiet.
    wrong = [n for n in rows if str(worksheet.acell(f"{status_col}{n}").value or "").strip().lower()
             != SUPERSEDED]
    if wrong:
        raise SystemExit(
            f"Status did not stick on row(s) {wrong} -- add `superseded` to the Status column's "
            "data-validation dropdown and re-run; their money cells are ALREADY blank, which "
            "`python -m scripts.audit_ledger` will report until the status lands."
        )
    print(f"Marked {len(rows)} row(s) superseded (money blanked, renumbered after the live boxes).")


def _restore(args, worksheet, header: list[str], existing: list[list]) -> None:
    backup_path = Path(args.restore_from)
    with backup_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows:
        raise SystemExit(f"{backup_path} is empty.")
    backup_header, backup_rows = [str(c) for c in rows[0]], rows[1:]
    appends: list[list] = []
    for order_id in args.order:
        plan = plan_restore(header, existing[1:], backup_header, backup_rows, order_id)
        for tracking, reason in plan["skipped"]:
            print(f"  (skipped {order_id} {tracking or '(blank)'}: {reason})")
        for tracking, new in plan["appends"]:
            print(f"  RESTORE  {order_id}  {tracking}  as shipment {new[header.index('Shipment')]}  "
                  f"-> status superseded, money blank")
            appends.append(new)
    if not appends:
        print("\nNothing to restore.")
        return
    if not args.apply:
        print("\nDry run only — nothing written. Re-run with --apply to append these rows.")
        return
    _backup(existing)
    start_row = _last_occupied_row(existing) + 1
    _ensure_grid_rows(worksheet, start_row + len(appends) - 1)
    worksheet.update(range_name=f"A{start_row}", values=appends,
                     value_input_option=ValueInputOption.raw)
    new_rows = list(range(start_row, start_row + len(appends)))
    _write_profit_formulas(worksheet, new_rows)
    status_col = _col_letter(header.index("Status"))
    wrong = [n for n in new_rows if str(worksheet.acell(f"{status_col}{n}").value or "").strip().lower()
             != SUPERSEDED]
    if wrong:  # the same dropdown tripwire as _apply_marks
        raise SystemExit(
            f"Status did not stick on restored row(s) {wrong} -- add `superseded` to the Status "
            "column's data-validation dropdown, fix those cells by hand, and re-run the audit."
        )
    print(f"\nRestored {len(appends)} row(s) at row(s) {new_rows} and stamped their formulas. "
          "They sit at the bottom: run `python -m scripts.sort_ledger --apply`, then "
          "`python -m scripts.audit_ledger`.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--order", action="append", required=True,
                        help="Order id to reconcile (repeatable)")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write on the live sheet (default: dry run, read-only)")
    parser.add_argument("--delete", action="store_true",
                        help="Delete the dead row instead of marking it superseded (the old repair)")
    parser.add_argument("--restore-from", metavar="BACKUP_CSV",
                        help="Put a row an earlier --delete removed back as a superseded row, from "
                             "that run's data/ledger_backup_<ts>.csv (no browser needed)")
    args = parser.parse_args()

    worksheet = _get_worksheet()
    header, existing = _read_sheet(worksheet)

    if args.restore_from:
        _restore(args, worksheet, header, existing)
        return

    idx = {name: header.index(name) for name in header}
    wanted = set(args.order)
    groups: dict = {}
    for row in existing[1:]:
        get = lambda name: str(row[idx[name]]).strip() if idx[name] < len(row) else ""  # noqa: E731
        if get("Order ID") in wanted and get("Retailer") in RETAILERS:
            groups.setdefault((get("Retailer"), get("Profile")), set()).add(get("Order ID"))
    if not groups:
        raise SystemExit(f"None of {sorted(wanted)} are Amazon / Amazon Business / Best Buy rows on the sheet.")

    live_by_order: dict = {}
    for (retailer, profile_label), ids in sorted(groups.items()):
        print(f"Re-reading {len(ids)} {retailer} order(s) on profile {profile_label or '(any)'}...")
        live_by_order.update(read_live_shipments(retailer, profile_label, sorted(ids)))

    mode = "delete" if args.delete else "mark"
    plan = plan_supersede_fix(header, existing[1:], live_by_order, mode=mode)
    _print_plan(plan, args.apply)
    if not args.apply or not (plan["marks"] or plan["deletions"] or plan["renumbers"]):
        return
    if plan["refused"]:
        raise SystemExit(
            f"{len(plan['refused'])} dead row(s) carry money from a buying group or a settled status "
            "(REFUSED above) -- nothing was written. Resolve them by hand first."
        )

    _backup(existing)

    # RENUMBERS FIRST: a cell write never moves a row, so the original row numbers stay valid no matter
    # what is deleted afterwards. Deleting first would invalidate any renumber below a deleted row.
    if plan["renumbers"]:
        col = _col_letter(header.index("Shipment"))
        for row_number, _oid, _old, new in plan["renumbers"]:
            worksheet.update(range_name=f"{col}{row_number}", values=[[int(new)]],
                             value_input_option=ValueInputOption.raw)
        print(f"Renumbered {len(plan['renumbers'])} shipment cell(s).")

    if plan["marks"]:
        # No formula re-stamp: nothing moves, and the formula columns are not written.
        _apply_marks(worksheet, header, plan)

    if plan["deletions"]:
        # Bottom-to-top by original row number: deleting the highest row only shifts rows below it,
        # none of which remain to process.
        for row_number, *_ in sorted(plan["deletions"], key=lambda t: t[0], reverse=True):
            worksheet.delete_rows(row_number)
        print(f"Deleted {len(plan['deletions'])} superseded row(s).")

        # MANDATORY after a delete, not tidy: Total Profit uses same-row relative references, so every
        # row that shifted up now carries a formula pointing at its OLD position.
        # audit_ledger.profit_formula_literal is the tripwire for getting this wrong.
        remaining = len(existing) - 1 - len(plan["deletions"])
        _write_profit_formulas(worksheet, list(range(2, remaining + 2)))
        print(f"Re-stamped the Total Profit formula on {remaining} row(s).")

    print("\nDone. Verify with `python -m scripts.audit_ledger`.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    main()
