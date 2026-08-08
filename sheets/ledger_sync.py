import csv
import logging
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

from config.settings import settings

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

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
            sheet_row = [
                _coerce(field, record[field])
                for field in [
                    "retailer",
                    "profile_label",
                    "order_id",
                    "order_date",
                    "status",
                    "order_url",
                    "tracking_number",
                    "tracking_url",
                    "delivery_date",
                    "delivery_address",
                    "item_name",
                    "quantity",
                    "cost_per_item",
                    "shipping",
                    "total_cost",
                    "card_last4",
                    "last_scraped_at",
                    "shipment",
                ]
            ]
            key = (
                record["order_id"],
                record["order_date"],
                record["item_name"],
                record.get("shipment", ""),
            )
            if key in key_to_existing:
                row_number, existing_row = key_to_existing[key]
                merged = _merge_row(existing_row, sheet_row)
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


def load_order_state(profile_label: str | None = None) -> dict:
    """Read the sheet and return, for this profile:

        {
          "delivered_ids": [order_id, ...],   # terminal — skip entirely
          "open_orders": [                     # undelivered (any age) — re-check directly
            {order_id, order_date, order_url, tracking_url, item_names: [...], status},
            ...
          ],
        }

    Lets a scrape skip delivered orders, do a tracking-only re-check of every undelivered
    order (jumping straight to its saved link regardless of age), and full-scrape only new
    orders. A multi-item order rolls up to 'delivered' only if every recorded row is delivered.
    Fails soft (empty state) if the sheet isn't configured/readable → treat all as new.
    """
    empty = {"delivered_ids": [], "open_orders": []}
    try:
        worksheet = _get_worksheet()
        existing = worksheet.get_all_values()
    except Exception:
        log.warning("Could not read order state from sheet; treating all orders as new.", exc_info=True)
        return empty

    if not existing or not any(cell.strip() for cell in existing[0]):
        return empty
    header = existing[0]
    needed = ("Order ID", "Order Date", "Status", "Profile", "Order Link", "Tracking Link", "Item Name")
    if any(c not in header for c in needed):
        return empty
    idx = {c: header.index(c) for c in needed}
    # Shipment is optional (older sheets predate it); read it when present so we can flag
    # multi-shipment orders — those must re-check via the agent, not the single-page CDP reader.
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
            oid,
            {
                "order_id": oid, "order_date": "", "order_url": "", "tracking_url": "",
                "item_names": [], "statuses": [], "shipments": [],
            },
        )
        o["order_date"] = o["order_date"] or row[idx["Order Date"]].strip()
        o["order_url"] = o["order_url"] or row[idx["Order Link"]].strip()
        o["tracking_url"] = o["tracking_url"] or row[idx["Tracking Link"]].strip()
        name = row[idx["Item Name"]].strip()
        if name and name not in o["item_names"]:
            o["item_names"].append(name)
        if shipment_idx is not None and shipment_idx < len(row):
            ship = row[shipment_idx].strip()
            if ship and ship not in o["shipments"]:
                o["shipments"].append(ship)
        o["statuses"].append((row[idx["Status"]].strip() or "ordered").lower())

    delivered_ids: list[str] = []
    open_orders: list[dict] = []
    for oid, o in orders.items():
        statuses = o.pop("statuses")
        if all(s == "delivered" for s in statuses):
            delivered_ids.append(oid)
        else:
            o["status"] = "shipped" if any(s == "shipped" for s in statuses) else "ordered"
            open_orders.append(o)
    return {"delivered_ids": delivered_ids, "open_orders": open_orders}
