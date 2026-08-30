"""Import a foreign spreadsheet of FINISHED orders onto the ledger. Dry run by default.

    python -m scripts.import_history old.csv                          # dry run: mapping, conversions, preview
    python -m scripts.import_history old.csv --rate-add "SUB Rate:SUB" --profile profile-alpha
    python -m scripts.import_history old.csv --map "Net=Total Profit" --map "Store=Retailer"
    python -m scripts.import_history old.csv --apply

WHY THIS EXISTS. Pasting rows straight into the sheet is fine for a handful, but a real export has
the errors the audit is BLIND to: a rate column that means 1% where you meant 13.5%, an Insurance
column with the sign flipped, a date column whose 3/11 might be March or November, three tracking
numbers in one cell. `audit_sheet` checks shapes; this checks the MONEY. The feature that justifies
it over pasting is the reconciliation: every source row's own profit figure is recomputed from the
mapped inputs, and any row that disagrees by more than a cent FAILS the import before a cell is
written. If the numbers agree everywhere, the mapping is right everywhere.

WHAT IT DOES, IN ORDER (every step prints what it did):
  1. Map the source headers onto the ledger's columns (normalised match, aliases, `--map` overrides).
  2. Convert dates to ISO. The file's day/month order is detected from a day > 12 anywhere in it;
     a file that never disambiguates is REFUSED unless `--date-format` says which it is.
  3. Derive: Cost Per Item = Total Cost / Quantity; Cashback Rate = the rate column plus any
     `--rate-add` columns (a sign-up-bonus rate that applies only when a flag column is TRUE);
     Insurance as a positive cost whatever sign the source uses; Card name / last 4 from a
     "Card Used" cell like "Triple Cash 4351"; Shipment numbers by grouping an order's rows by
     tracking number, with a cell holding several numbers split into one row per box.
  4. Reconcile against the source's profit column (auto-detected, or `--source-profit`).
  5. Refuse rows that are not terminal (ordered / shipped) -- the scrapers own open orders, and an
     imported open row becomes a duplicate on the next re-check. `--allow-open` overrides, loudly.
  6. Preview against the LIVE sheet, read-only: which rows would UPDATE an existing row, which
     would APPEND, which belong to an order the scrapers already recorded under different item
     names (skipped -- the scraped rows are authoritative -- unless `--allow-existing-orders`), and
     which reuse a tracking number already on the sheet under another order.
  7. Write a normalised CSV to data/import_<ts>.csv. With `--apply`, sync it through the SAME
     upsert every scrape uses (sheets.ledger_sync.sync_csv_to_sheet) and re-sort the sheet.

Rows with NO tracking number are accepted and WARNED about (a delivered order from before the
ledger often has none; the buying-group sync can never match such a row to a payout). Rows with no
order number get a synthetic one (`<group>-IMPORT-<date>-<n>`) and a warning -- a referral bonus is
real income the tax report must see. Blank rows are skipped. Placeholders ("Please fill", "#VALUE!")
read as blank.
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from models.order import FIELDNAMES, STATUSES, TERMINAL_STATUSES, OrderItem, shipment_label
from sheets.ledger_sync import HEADER, _parse_display_number

log = logging.getLogger(__name__)

PLACEHOLDERS = {"please fill", "#value!", "#n/a", "#ref!", "n/a", "-", "--", "tbd", "?"}

#: Source-header spellings that map onto a ledger column (normalised: lower-case, alphanumerics only).
ALIASES: dict[str, tuple[str, ...]] = {
    "order_date": ("orderdate", "date", "purchased", "purchasedate", "ordered"),
    "status": ("status", "state"),
    "item_name": ("item", "itemname", "product", "description", "title"),
    "quantity": ("quantity", "qty", "units", "count"),
    "retailer": ("retailer", "store", "merchant", "vendor"),
    "order_id": ("orderid", "ordernumber", "order", "orderno", "ordernum"),
    "tracking_number": ("trackingnumber", "tracking", "trackingno", "trackingnumbers"),
    "delivery_date": ("deliverydate", "delivered", "delivereddate", "arrived"),
    "total_cost": ("totalcost", "cost", "total", "amount", "price"),
    "cost_per_item": ("costperitem", "unitcost", "unitprice", "each"),
    "shipping": ("shipping", "shippingcost"),
    "card_name": ("cardused", "card", "cardname", "paymentmethod"),
    "card_last4": ("cardlast4", "last4", "lastfour"),
    "cashback_rate": ("cashback", "cashbackrate", "rate", "rebate", "cashbackpct"),
    "buying_group": ("buyinggroup", "group", "bg"),
    "insurance": ("insurance", "premium", "insurancefee", "fee"),
    "gift_card": ("giftcard", "giftcardamount", "gc", "giftcards"),
    "sales_tax": ("salestax", "tax", "taxes", "estimatedtax", "salestaxes"),
    "payout_date": ("payoutdate", "paiddate", "datepaid", "paid"),
    "payout_amount": ("payoutamount", "payout", "paidamount", "reimbursement", "received"),
    "delivery_address": ("deliveryaddress", "address", "shipto"),
    "tracking_submitted": ("trackingsubmitted", "submitted"),
    "order_url": ("orderurl", "orderlink", "link"),
    "tracking_url": ("trackingurl", "trackinglink"),
    "receipt_url": ("receipturl", "receiptlink", "receipt"),
    "profile_label": ("profile", "profilelabel", "account"),
    # reconcile-only: not a ledger column the import writes (it is a live formula on the sheet)
    "source_profit": ("totalprofit", "profit", "net", "netprofit", "margin"),
}
_DISPLAY_TO_FIELD = {h.lower().replace(" ", ""): f for f, h in zip(FIELDNAMES, HEADER)}

REQUIRED = ("order_date", "item_name", "total_cost")
_DATE_SLASH = re.compile(r"^\s*(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\s*$")
_DATE_ISO = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})")
_LAST4 = re.compile(r"(\d{4})\s*$")
_TRACKING_SPLIT = re.compile(r"[\s,;/|]+")

STATUS_WORDS = {
    "paid": "paid", "payout": "paid", "received": "paid",
    "return": "return", "returned": "return", "refunded": "return",
    "delivered": "delivered", "complete": "delivered", "completed": "delivered",
    "ordered": "ordered", "placed": "ordered", "pending": "ordered",
    "shipped": "shipped", "intransit": "shipped", "shipping": "shipped",
    "cancelled": "cancelled", "canceled": "cancelled",
}


# ---------------------------------------------------------------- pure helpers ---------------------
def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def is_placeholder(value) -> bool:
    return str(value or "").strip().lower() in PLACEHOLDERS


def clean(value) -> str:
    """A source cell as text, with placeholders read as blank."""
    text = str(value if value is not None else "").strip()
    return "" if is_placeholder(text) else text


def money(value) -> float | None:
    text = clean(value)
    if not text:
        return None
    parsed = _parse_display_number(text)
    return float(parsed) if parsed is not None else None


def truthy(value) -> bool:
    return clean(value).lower() in {"true", "yes", "y", "1", "x", "✓"}


def map_headers(source_headers: list[str], overrides: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """source header -> ledger field. Returns (mapping, unmapped source headers)."""
    by_norm: dict[str, str] = {}
    for field, names in ALIASES.items():
        for n in names:
            by_norm.setdefault(n, field)
    for disp, field in _DISPLAY_TO_FIELD.items():
        by_norm.setdefault(norm(disp), field)
    mapping, unmapped = {}, []
    for h in source_headers:
        if not str(h).strip():
            continue
        if h in overrides:
            target = overrides[h]
            field = target if target in FIELDNAMES or target == "source_profit" else _DISPLAY_TO_FIELD.get(norm(target))
            if field is None:
                raise SystemExit(f"--map {h!r}={target!r}: {target!r} is not a ledger column")
            mapping[h] = field
            continue
        field = by_norm.get(norm(h))
        if field and field not in mapping.values():
            mapping[h] = field
        else:
            unmapped.append(h)
    return mapping, unmapped


def detect_date_order(values: list[str]) -> str | None:
    """'mdy' / 'dmy' from a slash date with a component > 12 anywhere; None if never disambiguated."""
    verdict = None
    for v in values:
        m = _DATE_SLASH.match(v or "")
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12 and b > 12:
            raise SystemExit(f"date {v!r} is impossible in either day/month order")
        found = "dmy" if a > 12 else ("mdy" if b > 12 else None)
        if found and verdict and found != verdict:
            raise SystemExit(f"the file mixes day/month orders ({v!r} contradicts an earlier date)")
        verdict = verdict or found
    return verdict


def to_iso(value: str, order: str) -> str:
    text = clean(value)
    if not text:
        return ""
    if _DATE_ISO.match(text):
        y, m, d = (int(x) for x in _DATE_ISO.match(text).groups())
        return date(y, m, d).isoformat()
    m = _DATE_SLASH.match(text)
    if not m:
        raise ValueError(f"unrecognised date {text!r}")
    a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    mm, dd = (a, b) if order == "mdy" else (b, a)
    return date(y, mm, dd).isoformat()


def split_card(value: str) -> tuple[str, str]:
    """'Triple Cash 4351' -> ('Triple Cash', '4351'); 'Prime Visa' -> ('Prime Visa', '')."""
    text = clean(value)
    m = _LAST4.search(text)
    if m and len(text) > 4:
        return text[: m.start()].strip(" -:,"), m.group(1)
    return text, ""


def normalise_status(value: str) -> str:
    key = norm(value)
    if key in STATUS_WORDS:
        return STATUS_WORDS[key]
    if key in STATUSES:
        return key
    raise ValueError(f"unknown status {value!r}")


def tracking_numbers(value: str) -> list[str]:
    text = clean(value)
    return [t for t in _TRACKING_SPLIT.split(text) if t] if text else []


def distribute(total: int, buckets: int) -> list[int]:
    base, rem = divmod(total, buckets)
    return [base + (1 if i < rem else 0) for i in range(buckets)]


# ---------------------------------------------------------------- the import --------------------------
class Row:
    """One SOURCE row, normalised, before it becomes one or more ledger rows."""

    def __init__(self, n: int, raw: dict):
        self.n = n            # 1-based data row in the source file (header = 0)
        self.raw = raw
        self.fields: dict = {}
        self.source_profit: float | None = None
        self.warnings: list[str] = []
        self.rate_parts: list[str] = []
        self.trackings: list[str] = []

    def __getitem__(self, k):
        return self.fields.get(k)


def read_rows(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def normalise(raw_rows: list[dict], mapping: dict[str, str], *, date_order: str, rate_adds: list[tuple[str, str | None]],
              profile: str, allow_open: bool) -> tuple[list[Row], list[str], Counter]:
    """Source rows -> Row objects with ledger-field values. Returns (rows, refusals, counters)."""
    rows, refusals, counts = [], [], Counter()
    inverse = {f: h for h, f in mapping.items()}
    synthetic_seq: Counter = Counter()
    for n, raw in enumerate(raw_rows, start=1):
        if not any(clean(v) for k, v in raw.items() if k and norm(k) not in {"sub", "submitted"}):
            counts["blank rows skipped"] += 1
            continue
        r = Row(n, raw)
        get = lambda field: clean(raw.get(inverse.get(field, ""), ""))  # noqa: E731

        # --- identity
        try:
            r.fields["order_date"] = to_iso(get("order_date"), date_order)
            r.fields["delivery_date"] = to_iso(get("delivery_date"), date_order)
            r.fields["payout_date"] = to_iso(get("payout_date"), date_order)
        except ValueError as exc:
            refusals.append(f"row {n}: {exc}")
            continue
        try:
            status = normalise_status(get("status") or "delivered")
        except ValueError as exc:
            refusals.append(f"row {n}: {exc}")
            continue
        if status not in TERMINAL_STATUSES and not allow_open:
            refusals.append(f"row {n}: status {status!r} is not terminal -- the scrapers own open orders "
                            f"(--allow-open to force)")
            counts["open rows refused"] += 1
            continue
        r.fields["status"] = status
        r.fields["item_name"] = get("item_name")
        r.fields["retailer"] = get("retailer")
        r.fields["buying_group"] = get("buying_group")
        r.fields["profile_label"] = get("profile_label") or profile
        order_id = get("order_id")
        if not order_id:
            group = r.fields["buying_group"] or "IMPORT"
            synthetic_seq[(group, r.fields["order_date"])] += 1
            seq = synthetic_seq[(group, r.fields["order_date"])]
            order_id = f"{group}-IMPORT-{r.fields['order_date']}" + (f"-{seq}" if seq > 1 else "")
            r.warnings.append(f"no order number -- synthesised {order_id!r}")
            counts["order ids synthesised"] += 1
        r.fields["order_id"] = order_id
        if not r.fields["retailer"]:
            r.fields["retailer"] = r.fields["buying_group"] or "Unknown"
            r.warnings.append(f"no retailer -- using {r.fields['retailer']!r}")

        # --- quantities and money
        qty = money(get("quantity"))
        r.fields["quantity"] = int(qty) if qty else 1
        total = money(get("total_cost"))
        unit = money(get("cost_per_item"))
        if total is None and unit is not None:
            total = round(unit * r.fields["quantity"], 2)
        r.fields["total_cost"] = total
        r.fields["cost_per_item"] = round(total / r.fields["quantity"], 2) if total is not None else unit
        r.fields["shipping"] = money(get("shipping")) or 0.0
        # ORDER-LEVEL like shipping (sync prorates); blank stays None so nothing is overwritten.
        gc = money(get("gift_card"))
        r.fields["gift_card"] = abs(gc) if gc is not None else None
        r.fields["sales_tax"] = money(get("sales_tax"))
        ins = money(get("insurance"))
        r.fields["insurance"] = abs(ins) if ins is not None else None
        r.fields["payout_amount"] = money(get("payout_amount"))
        card_name, last4 = split_card(get("card_name"))
        r.fields["card_name"] = card_name
        r.fields["card_last4"] = get("card_last4") or last4
        rate = money(get("cashback_rate"))
        parts = [f"{inverse.get('cashback_rate', 'rate')}={rate}"] if rate is not None else []
        for col, flag in rate_adds:
            if flag is None or truthy(raw.get(flag, "")):
                extra = money(raw.get(col, ""))
                if extra:
                    rate = (rate or 0.0) + extra
                    parts.append(f"{col}={extra}")
        r.fields["cashback_rate"] = rate
        r.rate_parts = parts
        r.source_profit = money(get("source_profit"))
        r.fields["tracking_submitted"] = ""
        for f in ("delivery_address", "order_url", "tracking_url", "receipt_url"):
            r.fields[f] = get(f)

        # --- tracking
        r.trackings = tracking_numbers(get("tracking_number"))
        if not r.trackings:
            r.warnings.append("no tracking number -- the buying-group sync can never match a payout to this row")
            counts["rows without tracking"] += 1
        elif len(r.trackings) > 1:
            r.warnings.append(f"{len(r.trackings)} tracking numbers in one cell -- split into one row per box, "
                              f"quantity and money prorated")
            counts["multi-tracking rows split"] += 1
        rows.append(r)
    return rows, refusals, counts


def reconcile(rows: list[Row]) -> list[str]:
    """Recompute each source row's profit from the mapped inputs; return the rows that disagree."""
    bad = []
    for r in rows:
        if r.source_profit is None or r["payout_amount"] is None or r["total_cost"] is None:
            continue
        rate = r["cashback_rate"] or 0.0
        basis = (r["total_cost"] - (r["gift_card"] or 0.0) + (r["shipping"] or 0.0)
                 + (r["sales_tax"] or 0.0))
        expected = r["payout_amount"] - basis * (1 - rate) - (r["insurance"] or 0.0)
        if abs(expected - r.source_profit) > 0.011:
            bad.append(f"row {r.n} {r['order_id']}: source profit {r.source_profit:.2f}, recomputed "
                       f"{expected:.2f} (payout {r['payout_amount']:.2f} - cost {r['total_cost']:.2f} x (1 - "
                       f"{rate:.4f}) - insurance {(r['insurance'] or 0):.2f}); rate parts: {', '.join(r.rate_parts) or 'none'}")
    return bad


def explode(rows: list[Row]) -> list[OrderItem]:
    """Rows -> ledger rows: shipments numbered per order by tracking number, multi-number cells split."""
    items: list[OrderItem] = []
    ship_no: dict[tuple[str, str], int] = {}
    next_no: Counter = Counter()

    def number_for(order_id: str, tracking: str) -> str:
        key = (order_id, tracking)
        if key not in ship_no:
            next_no[order_id] += 1
            ship_no[key] = next_no[order_id]
        return shipment_label(ship_no[key])

    for r in rows:
        pieces = r.trackings or [""]
        qtys = distribute(r["quantity"], len(pieces)) if len(pieces) > 1 else [r["quantity"]]
        for tracking, qty in zip(pieces, qtys):
            share = qty / r["quantity"] if r["quantity"] else 1.0
            items.append(OrderItem(
                retailer=r["retailer"], profile_label=r["profile_label"], order_id=r["order_id"],
                order_date=r["order_date"], status=r["status"], item_name=r["item_name"],
                quantity=qty, cost_per_item=r["cost_per_item"], shipping=0.0,
                # ORDER-LEVEL: the full figure on every split row; the sync prorates by Total Cost.
                gift_card=r["gift_card"], sales_tax=r["sales_tax"],
                tracking_number=tracking, delivery_date=r["delivery_date"],
                card_last4=r["card_last4"], card_name=r["card_name"], cashback_rate=r["cashback_rate"],
                insurance=None if r["insurance"] is None else round(r["insurance"] * share, 2),
                payout_date=r["payout_date"],
                payout_amount=None if r["payout_amount"] is None else round(r["payout_amount"] * share, 2),
                buying_group=r["buying_group"], delivery_address=r["delivery_address"],
                order_url=r["order_url"], tracking_url=r["tracking_url"], receipt_url=r["receipt_url"],
                shipment=number_for(r["order_id"], tracking),
            ))
    return items


def preview_against_sheet(items: list[OrderItem], grid: list[list]) -> dict:
    """Classify each ledger row against the live sheet's FORMATTED grid (what the upsert keys on)."""
    header = grid[0]
    idx = {h: i for i, h in enumerate(header)}
    key_cols = ("Order ID", "Order Date", "Item Name", "Shipment")
    keys, orders, tracking_owner = set(), set(), {}
    for row in grid[1:]:
        cell = lambda c: (row[idx[c]] if idx.get(c) is not None and idx[c] < len(row) else "")  # noqa: E731
        if not str(cell("Order ID")).strip():
            continue
        keys.add(tuple(str(cell(c)).strip() for c in key_cols))
        orders.add(str(cell("Order ID")).strip())
        trk = str(cell("Tracking Number")).strip()
        if trk:
            tracking_owner.setdefault(trk, str(cell("Order ID")).strip())
    out = {"update": [], "append": [], "existing_order": [], "tracking_collision": []}
    for it in items:
        key = (it.order_id, it.order_date, it.item_name, it.shipment)
        owner = tracking_owner.get(it.tracking_number) if it.tracking_number else None
        if key in keys:
            out["update"].append(it)
        elif it.order_id in orders:
            out["existing_order"].append(it)
        elif owner and owner != it.order_id:
            out["tracking_collision"].append((it, owner))
        else:
            out["append"].append(it)
    return out


def write_normalised(items: list[OrderItem], out_path: Path) -> Path:
    from output.csv_writer import write_csv
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = write_csv(items, out_path.parent)
    tmp.replace(out_path)
    return out_path


# ---------------------------------------------------------------- CLI -------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Import finished orders from a foreign CSV. Dry run by default.")
    ap.add_argument("csv", type=Path)
    ap.add_argument("--apply", action="store_true", help="write to the sheet (default: preview only)")
    ap.add_argument("--profile", default="", help="Profile label to stamp when the source has none")
    ap.add_argument("--map", action="append", default=[], metavar="SRC=TARGET",
                    help="map a source column onto a ledger column (repeatable)")
    ap.add_argument("--rate-add", action="append", default=[], metavar="COL[:FLAGCOL]",
                    help="add this rate column to Cashback Rate, only when FLAGCOL is TRUE if given (repeatable)")
    ap.add_argument("--date-format", choices=("mdy", "dmy"), help="force the day/month order")
    ap.add_argument("--source-profit", metavar="COL", help="the source's profit column to reconcile against")
    ap.add_argument("--allow-open", action="store_true", help="import ordered/shipped rows too")
    ap.add_argument("--keep-no-cost", action="store_true",
                    help="import rows that have no Total Cost (default: skip them so a later scrape can fill the order)")
    ap.add_argument("--allow-existing-orders", action="store_true",
                    help="import rows for orders the sheet already holds under other item names")
    ap.add_argument("--no-sheet", action="store_true", help="skip the live-sheet preview (offline)")
    ap.add_argument("--out", type=Path, help="where to write the normalised CSV (default data/import_<ts>.csv)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)

    headers, raw_rows = read_rows(args.csv)
    overrides = dict(m.split("=", 1) for m in args.map)
    if args.source_profit:
        overrides[args.source_profit] = "source_profit"
    mapping, unmapped = map_headers(headers, overrides)
    print(f"Source: {args.csv} -- {len(raw_rows)} row(s), {len(headers)} column(s)\n")
    print("Column mapping:")
    for h in headers:
        if h in mapping:
            print(f"  {h:<20} -> {mapping[h]}")
    if unmapped:
        print(f"  ignored (no ledger column): {', '.join(repr(h) for h in unmapped)}")
    missing = [f for f in REQUIRED if f not in mapping.values()]
    if missing:
        print(f"\nREFUSED: required column(s) not mapped: {', '.join(missing)}. Use --map.", file=sys.stderr)
        return 2

    # dates
    inverse = {f: h for h, f in mapping.items()}
    date_values = [clean(r.get(inverse[f], "")) for r in raw_rows for f in ("order_date", "delivery_date", "payout_date") if f in inverse]
    order = args.date_format or detect_date_order(date_values)
    if order is None and any(_DATE_SLASH.match(v) for v in date_values):
        print("\nREFUSED: every slash date in the file could be month/day OR day/month. Say which with "
              "--date-format mdy|dmy.", file=sys.stderr)
        return 2
    order = order or "mdy"
    print(f"\nDates: read as {'month/day/year' if order == 'mdy' else 'day/month/year'}")
    samples = {}
    for v in date_values:
        if v and v not in samples and len(samples) < 4:
            try:
                samples[v] = to_iso(v, order)
            except ValueError:
                samples[v] = "?"
    for v, iso in samples.items():
        print(f"  {v:<14} -> {iso}")

    rate_adds = [(spec.split(":", 1)[0], spec.split(":", 1)[1] if ":" in spec else None) for spec in args.rate_add]
    rows, refusals, counts = normalise(raw_rows, mapping, date_order=order, rate_adds=rate_adds,
                                       profile=args.profile, allow_open=args.allow_open)
    if not args.keep_no_cost:
        # A placeholder row for a REAL order is worse than no row: once its order id is on the sheet
        # as terminal, no scrape will ever fetch that order again, so the placeholder blocks the very
        # sweep that could fill it in. Skip such rows; a bonus/credit with a genuine $0 cost is kept.
        no_cost = [r for r in rows if r["total_cost"] is None]
        for r in no_cost:
            refusals.append(f"row {r.n} {r['order_id']}: no Total Cost -- skipped so a scrape can still fill "
                            f"this order (--keep-no-cost to import it as-is)")
        counts["rows without a cost skipped"] += len(no_cost)
        rows = [r for r in rows if r["total_cost"] is not None]

    print(f"\nRows: {len(rows)} usable" + "".join(f"; {v} {k}" for k, v in counts.items()))
    if refusals:
        print(f"\nRefused ({len(refusals)}):")
        for line in refusals[:40]:
            print(f"  {line}")
    warned = [r for r in rows if r.warnings]
    if warned:
        print(f"\nWarnings ({len(warned)} row(s)):")
        for r in warned[:60]:
            for w in r.warnings:
                print(f"  row {r.n} {r['order_id']}: {w}")

    # reconciliation
    if "source_profit" in mapping.values():
        checked = [r for r in rows if r.source_profit is not None]
        bad = reconcile(rows)
        print(f"\nProfit reconciliation against {inverse['source_profit']!r}: {len(checked)} row(s) checked, "
              f"{len(bad)} disagree")
        for line in bad[:40]:
            print(f"  {line}")
        if bad:
            print("\nREFUSED: the recomputed profit disagrees with the source on the rows above -- a rate or an "
                  "insurance sign is mapped wrong. Fix the mapping (--rate-add, --map) before importing.",
                  file=sys.stderr)
            return 1
    else:
        print("\nNo profit column found to reconcile against -- pass --source-profit COL if the source has one.")

    items = explode(rows)
    print(f"\nLedger rows: {len(items)} (from {len(rows)} source rows)")

    # preview against the live sheet
    to_write = items
    if not args.no_sheet:
        try:
            from scripts.audit_sheet import open_worksheet_readonly, read_grids
            ws, title = open_worksheet_readonly()
            grid = read_grids(ws, title).formatted
        except Exception as exc:  # noqa: BLE001
            print(f"\nLive-sheet preview skipped ({type(exc).__name__}: {exc}); pass --no-sheet to silence.")
            grid = None
        if grid:
            pv = preview_against_sheet(items, grid)
            print(f"\nAgainst the live sheet ({len(grid) - 1} data rows):")
            print(f"  {len(pv['update'])} would UPDATE an existing row (same upsert key)")
            print(f"  {len(pv['append'])} would APPEND")
            if pv["existing_order"]:
                verb = "imported anyway (--allow-existing-orders)" if args.allow_existing_orders else "SKIPPED -- the scraped rows are authoritative"
                print(f"  {len(pv['existing_order'])} belong to orders ALREADY on the sheet under other item names: {verb}")
                for it in pv["existing_order"][:20]:
                    print(f"      {it.retailer} {it.order_id} ship {it.shipment} {it.item_name[:45]!r}")
            if pv["tracking_collision"]:
                print(f"  {len(pv['tracking_collision'])} reuse a tracking number the sheet holds under ANOTHER order (imported; check them):")
                for it, owner in pv["tracking_collision"][:20]:
                    print(f"      {it.order_id} {it.tracking_number} -- sheet has it under {owner}")
            if not args.allow_existing_orders:
                skip = {id(it) for it in pv["existing_order"]}
                to_write = [it for it in items if id(it) not in skip]

    out_path = args.out or Path("data") / f"import_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.csv"
    write_normalised(to_write, out_path)
    print(f"\nNormalised CSV: {out_path} ({len(to_write)} row(s))")

    if not args.apply:
        print("\nDRY RUN -- nothing written to the sheet. Re-run with --apply to import.")
        return 0
    from sheets.ledger_sync import sort_ledger_by_date_desc, sync_csv_to_sheet
    sync_csv_to_sheet(out_path)
    sort_ledger_by_date_desc()
    print("\nImported and re-sorted. Run `python -m scripts.audit_sheet --compare <before.json> --strict` to judge it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
