import csv
import logging
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

from config.settings import settings
from models.order import FIELDNAMES, TERMINAL_STATUSES

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Display names for the sheet's header row, positionally 1:1 with models.order.FIELDNAMES — rows are
# written positionally from column A, so the two lists must stay the same length and order. Adding a
# column means appending to BOTH, never inserting. tests/test_schema.py enforces this.
HEADER = [
    "Retailer",
    "Profile",
    "Order ID",
    "Order Date",
    "Status",
    "Order Link",
    "Tracking Number",
    "Tracking Link",
    "Delivery Date",
    "Delivery Address",
    "Item Name",
    "Quantity",
    "Cost Per Item",
    "Shipping",
    "Total Cost",
    "Card Last 4",
    "Last Scraped At",
    "Shipment",  # last column; see models.order FIELDNAMES for why it's appended, not inserted
]

# Numeric columns get coerced to numbers so the sheet supports sum()/formulas.
_NUMERIC_FIELDS = {"quantity", "cost_per_item", "shipping", "total_cost"}

# Furthest-along status wins when two rows of ONE shipment are collapsed in a single sync (see
# _collapse_records). Mirrors _rollup_status's spirit: cancelled overrides, then delivered, then
# shipped, then ordered.
_STATUS_RANK = {"ordered": 0, "shipped": 1, "delivered": 2, "cancelled": 3}


def _collapse_records(records: list[dict]) -> list[dict]:
    """Collapse CSV records that share an upsert key into one row before upserting.

    A single sync can carry two rows for the same (order, order_date, item, shipment): the cheap
    CDP tracking read (status 'shipped' + tracking number, blank delivery date) and the agent's
    re-read of that same shipment (delivery date, but blank tracking — the Amazon agent never reads
    the number). Upserting them independently would let the second clobber the first: each merges
    against the same pre-sync snapshot and writes the row separately, so last-write-wins wipes CDP's
    tracking number back to blank and the order never progresses. Merge them here first — non-blank
    wins per field (so the two half-rows combine), and for the one field they can legitimately
    disagree on, status, the furthest-along value wins. First-seen order is preserved.
    """
    collapsed: dict[tuple, dict] = {}
    order: list[tuple] = []
    for rec in records:
        key = (
            rec.get("order_id", ""),
            rec.get("order_date", ""),
            rec.get("item_name", ""),
            rec.get("shipment", ""),
        )
        if key not in collapsed:
            collapsed[key] = dict(rec)
            order.append(key)
            continue
        acc = collapsed[key]
        for field, val in rec.items():
            if field == "status":
                continue  # status is not first-non-blank; it's resolved by rank below
            if str(val).strip() and not str(acc.get(field, "")).strip():
                acc[field] = val
        new_status = (rec.get("status") or "").strip().lower()
        cur_status = (acc.get("status") or "").strip().lower()
        if _STATUS_RANK.get(new_status, -1) > _STATUS_RANK.get(cur_status, -1):
            acc["status"] = rec.get("status")
    return [collapsed[k] for k in order]


def _get_worksheet() -> gspread.Worksheet:
    creds = Credentials.from_service_account_file(settings.google_service_account_file, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(settings.google_sheet_id)
    try:
        return spreadsheet.worksheet(settings.google_sheet_worksheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=settings.google_sheet_worksheet_name, rows=1000, cols=len(HEADER)
        )
        worksheet.append_row(HEADER)
        return worksheet


def _coerce(field: str, value: str):
    if field in _NUMERIC_FIELDS:
        try:
            return int(value) if field == "quantity" else float(value)
        except ValueError:
            return value
    return value


def sync_csv_to_sheet(csv_path: Path) -> None:
    worksheet = _get_worksheet()
    existing = worksheet.get_all_values()
    # An empty-but-existing worksheet returns [] or a single blank row like [[]] — both mean
    # "no header yet", so (re)write our header into row 1.
    if not existing or not any(cell.strip() for cell in existing[0]):
        worksheet.update(range_name="A1", values=[HEADER])
        existing = [HEADER]

    header = existing[0]
    # Migrate older sheets that predate the Shipment column: it's appended at the end, so existing
    # data rows keep their positions and just gain a trailing (empty) cell. Rewriting row 1 to the
    # full HEADER is safe because the leading columns are identical to what's already there.
    if "Shipment" not in header:
        worksheet.update(range_name="A1", values=[HEADER])
        header = list(HEADER)

    # Row identity: Order ID + Order Date (per spec) plus Item Name and Shipment — a single order
    # can contain multiple line items, and a single item can repeat across shipments (same SKU in
    # different shipments), which would otherwise collide on the same key.
    key_cols = ["Order ID", "Order Date", "Item Name", "Shipment"]
    missing = [c for c in key_cols if c not in header]
    if missing:
        raise RuntimeError(
            f"Worksheet '{settings.google_sheet_worksheet_name}' first row is not a recognized "
            f"header (missing {missing}). Clear the sheet, or set its header row to: {HEADER}"
        )
    key_idx = [header.index(col) for col in key_cols]
    oid_idx = header.index("Order ID")
    # Name-agnostic shipment-line index for the deferral below (Order ID + Order Date + Shipment).
    skey_idx = [header.index(c) for c in ("Order ID", "Order Date", "Shipment")]
    name_hdr_idx = header.index("Item Name")
    name_field_idx = FIELDNAMES.index("item_name")

    key_to_existing: dict[tuple, tuple[int, list]] = {}
    shipment_to_existing: dict[tuple, list[tuple[int, list]]] = {}
    for row_number, row in enumerate(existing[1:], start=2):
        # Rows written before Shipment existed are shorter than key_idx; read missing cells as ""
        # (never skip them, or pre-migration rows would fail to match and duplicate on re-check).
        if oid_idx >= len(row) or not row[oid_idx].strip():
            continue
        key = tuple(row[i] if i < len(row) else "" for i in key_idx)
        key_to_existing[key] = (row_number, row)
        skey = tuple(row[i] if i < len(row) else "" for i in skey_idx)
        shipment_to_existing.setdefault(skey, []).append((row_number, row))

    with csv_path.open(newline="", encoding="utf-8") as f:
        records = list(csv.DictReader(f))

    # Collapse same-key rows (CDP read + agent re-read of one shipment) before upserting, so the
    # two half-rows merge into one instead of the later overwriting the earlier's tracking number.
    collapsed = _collapse_records(records)
    # How many incoming rows target each shipment line — the deferral only fires in the unambiguous
    # 1:1 case (see below), so a shipment carrying two distinct products doesn't mis-merge.
    incoming_skey_count: dict[tuple, int] = {}
    for rec in collapsed:
        if str(rec.get("order_id", "")).strip():
            sk = (rec.get("order_id", ""), rec.get("order_date", ""), rec.get("shipment", ""))
            incoming_skey_count[sk] = incoming_skey_count.get(sk, 0) + 1

    updates = 0
    appends: list[list] = []
    claimed_rows: set[int] = set()
    skipped_blank = 0
    for record in collapsed:
        # A record with no Order ID can't form a valid upsert key (Order ID + Order Date + Item Name
        # + Shipment), so it never matches an existing row and appends as a permanent orphan/duplicate
        # — seen once when the agent dropped the order_id on a single shipment entry, leaving a stray
        # blank-Order-ID row alongside the correct one. Skip it; the next run re-reads that order and
        # writes the row with its real key.
        if not str(record.get("order_id", "")).strip():
            skipped_blank += 1
            log.warning(
                "Skipping a record with a blank Order ID (would orphan into a duplicate): "
                "item=%r shipment=%r",
                record.get("item_name", ""), record.get("shipment", ""),
            )
            continue
        # Driven off FIELDNAMES (the same list csv_writer writes) rather than a second literal
        # copy, so a new column can't land in one place and not the other. .get() tolerates
        # re-syncing an older CSV written before a column was added — the missing value arrives
        # blank, which _merge_row then refuses to write over existing data.
        sheet_row = [_coerce(field, record.get(field, "")) for field in FIELDNAMES]
        key = (
            record["order_id"],
            record["order_date"],
            record["item_name"],
            record.get("shipment", ""),
        )
        if key in key_to_existing:
            row_number, existing_row = key_to_existing[key]
        else:
            # DEFER TO AN ALREADY-RECORDED SHIPMENT LINE. The exact key (…+ Item Name) didn't match,
            # but a row for the SAME order+date+shipment may already exist under a differently-worded
            # item name — e.g. the ss-api's `itemDesc` vs the agent fallback's page-title text. Update
            # that row IN PLACE, keeping its recorded item name, instead of appending a divergent
            # duplicate. Only in the unambiguous 1:1 case (exactly one incoming row and exactly one
            # unclaimed existing row for the shipment line) — a shipment with two distinct products is
            # left to append rather than risk mis-merging.
            skey = (record["order_id"], record["order_date"], record.get("shipment", ""))
            candidates = [c for c in shipment_to_existing.get(skey, []) if c[0] not in claimed_rows]
            if incoming_skey_count.get(skey, 0) == 1 and len(candidates) == 1:
                row_number, existing_row = candidates[0]
                if name_hdr_idx < len(existing_row) and str(existing_row[name_hdr_idx]).strip():
                    sheet_row[name_field_idx] = existing_row[name_hdr_idx]  # keep recorded identity
            else:
                appends.append(sheet_row)
                continue

        merged = _merge_row(existing_row, sheet_row)
        # Preserved cells come back as strings from get_all_values(); re-coerce so a kept numeric
        # (e.g. a quantity carried over from a prior run) is written as a number, not text —
        # otherwise Sheets stores it as text and shows a leading-apostrophe '1.
        merged = [_coerce(field, val) for field, val in zip(FIELDNAMES, merged)]
        worksheet.update(range_name=f"A{row_number}", values=[merged])
        claimed_rows.add(row_number)
        updates += 1

    if appends:
        # Write at an explicit column-A range rather than worksheet.append_rows(): append_rows lets
        # the Sheets API auto-detect the "table" to append after, which on some sheets anchors to the
        # wrong column (observed shifting rows 10 columns right into K:AB). Positioning from column A
        # of the first empty row keeps every row aligned to the header. `existing` was read before any
        # updates and updates never add rows, so len(existing)+1 is the first free row.
        start_row = len(existing) + 1
        worksheet.update(range_name=f"A{start_row}", values=appends)

    log.info(
        "Sheet sync: %d row(s) updated, %d row(s) appended%s.",
        updates,
        len(appends),
        f", {skipped_blank} skipped (blank Order ID)" if skipped_blank else "",
    )


def _merge_row(existing_row: list, new_row: list) -> list:
    """Overlay new_row onto existing_row, but never overwrite an existing non-empty cell with a
    blank. This makes partial refreshes safe: a tracking-only re-check leaves the static columns
    (item name, cost, address, ...) blank, and those blanks must not wipe already-captured data —
    while real new values (status, tracking, delivery date, last scraped at) still update."""
    merged = []
    for i, new_val in enumerate(new_row):
        old_val = existing_row[i] if i < len(existing_row) else ""
        if str(new_val).strip() == "" and str(old_val).strip() != "":
            merged.append(old_val)
        else:
            merged.append(new_val)
    return merged


def _rollup_status(statuses: list[str]) -> str:
    """Collapse several shipment statuses into one for display.

    Cancelled is order-level in practice (the whole order is cancelled), so it wins outright.
    Otherwise delivered only when everything is; shipped if anything has shipped; else ordered.
    """
    if statuses and all(s == "cancelled" for s in statuses):
        return "cancelled"
    if statuses and all(s in ("delivered", "cancelled") for s in statuses):
        return "delivered"
    if any(s in ("shipped", "delivered") for s in statuses):
        return "shipped"
    return "ordered"


def load_order_state(profile_label: str | None = None, since: str | None = None) -> dict:
    """Read the sheet and return, for this profile:

        {
          "delivered_ids": [order_id, ...],    # all shipments delivered — terminal, skip
          "cancelled_ids": [order_id, ...],    # cancelled — terminal, skip
          "open_orders": [
            {
              order_id, order_date, order_url,
              status,        # rolled up across shipments (display only)
              needs_agent,   # some shipment has no tracking number yet -> it can still SPLIT,
                             # so the agent must re-read the order-details page
              shipments: [
                {shipment, status, tracking_number, tracking_url, delivery_date, item_names: [...]},
                ...
              ],
            },
            ...
          ],
        }

    Each sheet row IS one (shipment x item), so shipments are recovered by grouping an order's
    rows on the Shipment column. Keeping them separate is what lets the caller re-check each
    shipment's own tracking page — an order with several shipments has several tracking links,
    and collapsing them to one would silently drop all but the first.

    Terminal orders (all delivered, or cancelled) drop out of re-checks entirely and only feed
    the agent's discovery skip list. `since` (YYYY-MM-DD) trims terminal orders older than the
    discovery window from those lists — the agent never scans back past the window, so without
    this the skip list grows forever and is re-sent on every single agent step.

    Fails soft (empty state) if the sheet isn't configured/readable → treat all as new.
    """
    empty: dict = {"delivered_ids": [], "cancelled_ids": [], "open_orders": []}
    try:
        worksheet = _get_worksheet()
        existing = worksheet.get_all_values()
    except Exception:
        log.warning("Could not read order state from sheet; treating all orders as new.", exc_info=True)
        return empty

    if not existing or not any(cell.strip() for cell in existing[0]):
        return empty
    header = existing[0]
    needed = (
        "Order ID", "Order Date", "Status", "Profile", "Order Link",
        "Tracking Number", "Tracking Link", "Delivery Date", "Item Name",
    )
    if any(c not in header for c in needed):
        return empty
    idx = {c: header.index(c) for c in needed}
    # Optional: sheets written before the Shipment column exist. Those rows group under "",
    # which behaves like any other single shipment.
    shipment_idx = header.index("Shipment") if "Shipment" in header else None

    orders: dict[str, dict] = {}
    for row in existing[1:]:
        if len(row) <= max(idx.values()):
            continue
        if profile_label is not None and row[idx["Profile"]] != profile_label:
            continue
        oid = row[idx["Order ID"]].strip()
        if not oid:
            continue
        o = orders.setdefault(
            oid, {"order_id": oid, "order_date": "", "order_url": "", "_groups": {}}
        )
        o["order_date"] = o["order_date"] or row[idx["Order Date"]].strip()
        o["order_url"] = o["order_url"] or row[idx["Order Link"]].strip()

        label = row[shipment_idx].strip() if shipment_idx is not None and shipment_idx < len(row) else ""
        s = o["_groups"].setdefault(
            label,
            {
                "shipment": label, "tracking_number": "", "tracking_url": "",
                "delivery_date": "", "item_names": [], "_statuses": [],
            },
        )
        # First non-blank wins within a shipment: every row of one shipment carries the same
        # tracking/delivery values, so any non-blank one is that shipment's value.
        s["tracking_number"] = s["tracking_number"] or row[idx["Tracking Number"]].strip()
        s["tracking_url"] = s["tracking_url"] or row[idx["Tracking Link"]].strip()
        s["delivery_date"] = s["delivery_date"] or row[idx["Delivery Date"]].strip()
        name = row[idx["Item Name"]].strip()
        if name and name not in s["item_names"]:
            s["item_names"].append(name)
        s["_statuses"].append((row[idx["Status"]].strip() or "ordered").lower())

    delivered_ids: list[str] = []
    cancelled_ids: list[str] = []
    open_orders: list[dict] = []
    for oid, o in orders.items():
        shipments = []
        for s in o.pop("_groups").values():
            s["status"] = _rollup_status(s.pop("_statuses"))
            shipments.append(s)

        in_window = since is None or not o["order_date"] or o["order_date"] >= since

        # Terminal orders drop out of re-checks; they only remain to tell the agent "skip these"
        # during discovery, so trim ones older than the window.
        if all(s["status"] == "cancelled" for s in shipments):
            if in_window:
                cancelled_ids.append(oid)
            continue
        if all(s["status"] in TERMINAL_STATUSES for s in shipments):
            if in_window:
                delivered_ids.append(oid)
            continue

        o["shipments"] = shipments
        o["status"] = _rollup_status([s["status"] for s in shipments])
        # A shipment that is still open (not delivered or cancelled) and has no tracking number
        # hasn't shipped yet, and Amazon splits an order into its final shipments AT ship time —
        # so the structure can still change and only a fresh read of order details can see that.
        o["needs_agent"] = any(
            s["status"] not in TERMINAL_STATUSES and not s["tracking_number"] for s in shipments
        )
        open_orders.append(o)
    return {
        "delivered_ids": delivered_ids,
        "cancelled_ids": cancelled_ids,
        "open_orders": open_orders,
    }
