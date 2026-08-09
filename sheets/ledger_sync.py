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

    key_to_existing: dict[tuple, tuple[int, list]] = {}
    for row_number, row in enumerate(existing[1:], start=2):
        # Rows written before Shipment existed are shorter than key_idx; read missing cells as ""
        # (never skip them, or pre-migration rows would fail to match and duplicate on re-check).
        if oid_idx >= len(row) or not row[oid_idx].strip():
            continue
        key = tuple(row[i] if i < len(row) else "" for i in key_idx)
        key_to_existing[key] = (row_number, row)

    updates = 0
    appends: list[list] = []
    with csv_path.open(newline="", encoding="utf-8") as f:
        for record in csv.DictReader(f):
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
                merged = _merge_row(existing_row, sheet_row)
                # Preserved cells come back as strings from get_all_values(); re-coerce so a kept
                # numeric (e.g. a quantity carried over from a prior run) is written as a number,
                # not text — otherwise Sheets stores it as text and shows a leading-apostrophe '1.
                merged = [_coerce(field, val) for field, val in zip(FIELDNAMES, merged)]
                worksheet.update(range_name=f"A{row_number}", values=[merged])
                updates += 1
            else:
                appends.append(sheet_row)

    if appends:
        worksheet.append_rows(appends)

    log.info("Sheet sync: %d row(s) updated, %d row(s) appended.", updates, len(appends))


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
