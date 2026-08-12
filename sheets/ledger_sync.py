import csv
import logging
import re
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

from config.settings import settings
from config.warehouses import classify_address, is_personal
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
    "Shipment",  # see models.order FIELDNAMES for why columns are appended, not inserted
    "Buying Group",  # derived from Delivery Address (config.warehouses.classify_address)
    "Card",  # derived from Card Last 4 (config.cards.resolve_card)
    "Cashback Rate",  # decimal fraction, e.g. 0.02 — format the column as a percentage to taste
    "Insurance",  # user-entered (BFMR/MaxOutDeals later)
    "Payout Date",  # user-entered (BFMR/MaxOutDeals later)
    "Payout Amount",  # user-entered (BFMR/MaxOutDeals later)
    "Total Profit",  # a live sheet formula, written by _profit_formula
]

# Numeric columns get coerced to numbers so the sheet supports sum()/formulas. total_profit is
# deliberately absent: it's written as a formula string, never as a number.
_NUMERIC_FIELDS = {
    "quantity", "cost_per_item", "shipping", "total_cost",
    "cashback_rate", "insurance", "payout_amount",
}


def _col_letter(index: int) -> str:
    """0-based column index -> its A1 letter ("A", ..., "Z", "AA", ...)."""
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


# A1 letters for the columns the profit formula reads, derived from FIELDNAMES rather than hardcoded
# so appending another column can never silently point the formula at the wrong cells.
_COL = {field: _col_letter(i) for i, field in enumerate(FIELDNAMES)}


def _profit_formula(row_number: int) -> str:
    """The live Total Profit formula for one sheet row.

        Total Profit = Payout Amount + Cashback - Total Cost - Shipping - Insurance
        Cashback     = (Total Cost + Shipping) * Cashback Rate

    It's a formula, not a Python-computed number, because Insurance and Payout Amount are typed into
    the sheet by hand (and later filled by the BFMR/MaxOutDeals step). A value computed at scrape time
    would be stale the moment either is entered, and a delivered row is terminal — never re-scraped —
    so it would stay stale forever.

    SHIPPING IS ALLOCATED PRO-RATA, which is the one non-obvious part. Every retailer repeats the
    ORDER-level shipping total on each of the order's rows (Best Buy `price.shippingTotal`, Costco
    `shippingAndHandling`, both Amazon parsers, and all four agent prompts). Subtracting column N
    as-is would therefore charge a 3-row order its shipping three times over, and credit cashback on
    it three times, making the column's SUM wrong — the number this ledger exists to get right. So
    each row takes the share of shipping matching its share of the order's Total Cost:

        s = Shipping * Total Cost / SUMIF(all rows of this Order ID, Total Cost)

    which sums back to exactly one shipping charge per order. IFERROR covers the degenerate case
    where an order's costs are all blank (division by zero) -> 0.

    Returns "" (blank cell, not 0) until Payout Amount is filled, so an un-paid-out row doesn't
    display a large fake loss that would poison a column sum.
    """
    n = row_number
    oid, ship, cost = _COL["order_id"], _COL["shipping"], _COL["total_cost"]
    rate, ins, payout = _COL["cashback_rate"], _COL["insurance"], _COL["payout_amount"]
    prorated_shipping = (
        f"IFERROR({ship}{n}*{cost}{n}/SUMIF(${oid}$2:${oid},${oid}{n},${cost}$2:${cost}),0)"
    )
    profit = f"{payout}{n}+({cost}{n}+s)*{rate}{n}-{cost}{n}-s-{ins}{n}"
    return f'=IF({payout}{n}="","",IFERROR(LET(s,{prorated_shipping},{profit}),""))'

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


def _next_shipment_number(order_id, existing, oid_hdr_idx, shipment_hdr_idx,
                          appends, oid_field_idx, shipment_field_idx) -> int:
    """Highest 'Shipment N' seen for this order (across existing sheet rows AND rows already queued to
    append this sync) + 1 — a unique, stable label for a newly-detected split box."""
    nums = [1]  # so the first extra box becomes at least "Shipment 2" even if labels don't parse
    for row in existing[1:]:
        if oid_hdr_idx < len(row) and row[oid_hdr_idx] == order_id and shipment_hdr_idx < len(row):
            m = re.match(r"Shipment\s+(\d+)", str(row[shipment_hdr_idx]).strip())
            if m:
                nums.append(int(m.group(1)))
    for row in appends:
        if oid_field_idx < len(row) and row[oid_field_idx] == order_id:
            m = re.match(r"Shipment\s+(\d+)", str(row[shipment_field_idx]).strip())
            if m:
                nums.append(int(m.group(1)))
    return max(nums) + 1


def sync_csv_to_sheet(csv_path: Path) -> None:
    worksheet = _get_worksheet()
    existing = worksheet.get_all_values()
    # An empty-but-existing worksheet returns [] or a single blank row like [[]] — both mean
    # "no header yet", so (re)write our header into row 1.
    if not existing or not any(cell.strip() for cell in existing[0]):
        worksheet.update(range_name="A1", values=[HEADER])
        existing = [HEADER]

    header = existing[0]
    # Migrate an older sheet whose header is a PREFIX of the current HEADER. Columns are only ever
    # APPENDED (Shipment, then Buying Group), so a pre-migration sheet's header is HEADER truncated at
    # the right — its leading columns are byte-identical to ours. Rewriting row 1 to the full HEADER is
    # then safe: existing data rows keep their positions and simply gain trailing (empty) cells. A
    # header that is NOT a prefix (garbage, reordered, or renamed) is deliberately left alone so the
    # key-column check below can reject it instead of silently overwriting real column names.
    if header != list(HEADER) and header == list(HEADER[: len(header)]):
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
    shipment_hdr_idx = header.index("Shipment")
    shipment_field_idx = FIELDNAMES.index("shipment")
    oid_field_idx = FIELDNAMES.index("order_id")
    qty_field_idx = FIELDNAMES.index("quantity")
    total_field_idx = FIELDNAMES.index("total_cost")
    # Tracking-number index for the tracking-based deferral (Order ID + Tracking Number). The carrier
    # tracking number is an identity BOTH the API and the agent read identically, so it reconciles rows
    # even when their synthetic Shipment numbers diverge (e.g. Costco: the API numbers shipments by
    # tracking sort, the agent numbers top-to-bottom).
    tracking_hdr_idx = header.index("Tracking Number") if "Tracking Number" in header else None

    key_to_existing: dict[tuple, tuple[int, list]] = {}
    shipment_to_existing: dict[tuple, list[tuple[int, list]]] = {}
    tracking_to_existing: dict[tuple, list[tuple[int, list]]] = {}
    for row_number, row in enumerate(existing[1:], start=2):
        # Rows written before Shipment existed are shorter than key_idx; read missing cells as ""
        # (never skip them, or pre-migration rows would fail to match and duplicate on re-check).
        if oid_idx >= len(row) or not row[oid_idx].strip():
            continue
        key = tuple(row[i] if i < len(row) else "" for i in key_idx)
        key_to_existing[key] = (row_number, row)
        skey = tuple(row[i] if i < len(row) else "" for i in skey_idx)
        shipment_to_existing.setdefault(skey, []).append((row_number, row))
        if tracking_hdr_idx is not None:
            trk = row[tracking_hdr_idx].strip() if tracking_hdr_idx < len(row) else ""
            if trk:
                tracking_to_existing.setdefault((row[oid_idx], trk), []).append((row_number, row))

    with csv_path.open(newline="", encoding="utf-8") as f:
        records = list(csv.DictReader(f))

    # Collapse same-key rows (CDP read + agent re-read of one shipment) before upserting, so the
    # two half-rows merge into one instead of the later overwriting the earlier's tracking number.
    collapsed = _collapse_records(records)
    # How many incoming rows target each shipment line — the deferral only fires in the unambiguous
    # 1:1 case (see below), so a shipment carrying two distinct products doesn't mis-merge.
    incoming_skey_count: dict[tuple, int] = {}
    incoming_tkey_count: dict[tuple, int] = {}
    for rec in collapsed:
        oid = str(rec.get("order_id", "")).strip()
        if oid:
            sk = (rec.get("order_id", ""), rec.get("order_date", ""), rec.get("shipment", ""))
            incoming_skey_count[sk] = incoming_skey_count.get(sk, 0) + 1
            trk = str(rec.get("tracking_number", "")).strip()
            if trk:
                tk = (rec.get("order_id", ""), trk)
                incoming_tkey_count[tk] = incoming_tkey_count.get(tk, 0) + 1

    updates = 0
    appends: list[list] = []
    claimed_rows: set[int] = set()
    written_rows: list[int] = []  # every row touched this sync -> gets its Total Profit formula
    split_events: list[dict] = []
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
            match: tuple[int, list] | None = None
            # DEFER (1) BY TRACKING NUMBER — the strongest cross-path identity, tried FIRST. The
            # carrier tracking number is read identically by the API and the agent even when their
            # synthetic Shipment numbers diverge (Costco: the API numbers shipments by tracking sort,
            # the agent top-to-bottom, so they can be swapped). An incoming row uniquely sharing
            # (Order ID, Tracking Number) with one existing row is the same physical line: update it in
            # place, keeping BOTH its recorded item name and Shipment number so the paths converge on
            # one row. Tried before the shipment-line rule below so a swapped Shipment number can't
            # mis-merge onto the wrong box. Only the unambiguous 1:1 case (one incoming, one existing
            # for that tracking number) — a box holding two distinct SKUs is left to append.
            if tracking_hdr_idx is not None:
                trk = str(record.get("tracking_number", "")).strip()
                tkey = (record["order_id"], trk)
                tcandidates = [c for c in tracking_to_existing.get(tkey, []) if c[0] not in claimed_rows]
                if trk and incoming_tkey_count.get(tkey, 0) == 1 and len(tcandidates) == 1:
                    match = tcandidates[0]
                    er = match[1]
                    if name_hdr_idx < len(er) and str(er[name_hdr_idx]).strip():
                        sheet_row[name_field_idx] = er[name_hdr_idx]  # keep recorded name
                    if shipment_hdr_idx < len(er) and str(er[shipment_hdr_idx]).strip():
                        sheet_row[shipment_field_idx] = er[shipment_hdr_idx]  # keep recorded shipment
            # DEFER (2) TO AN ALREADY-RECORDED SHIPMENT LINE (name-agnostic), for rows the tracking
            # rule didn't resolve — e.g. not-yet-shipped lines with no tracking number. The exact key
            # (…+ Item Name) missed, but a row for the SAME order+date+shipment may already exist under
            # a differently-worded item name (the Best Buy ss-api `itemDesc` vs the agent's page-title
            # text, where the two paths DO agree on the Shipment number). Keep the recorded item name.
            # Unambiguous 1:1 only — a shipment with two distinct products is left to append.
            if match is None:
                skey = (record["order_id"], record["order_date"], record.get("shipment", ""))
                candidates = [c for c in shipment_to_existing.get(skey, []) if c[0] not in claimed_rows]
                if incoming_skey_count.get(skey, 0) == 1 and len(candidates) == 1:
                    match = candidates[0]
                    if name_hdr_idx < len(match[1]) and str(match[1][name_hdr_idx]).strip():
                        sheet_row[name_field_idx] = match[1][name_hdr_idx]  # keep recorded name
            if match is None:
                appends.append(sheet_row)
                continue
            row_number, existing_row = match

        # UNDISCLOSED-SPLIT DETECTION. We're about to update an existing shipment row, but its recorded
        # tracking number is being CHANGED to a different non-blank number. That usually means the order
        # shipped in more than one box while the retailer surfaces only ONE "primary" tracking number at
        # a time (seen live on a Best Buy qty-15 order that split 9+6 — its number rotated 086084 ->
        # 128095). Rather than overwrite (losing the first box), keep the existing box's row and APPEND a
        # new row for the new box with Quantity "*" (the split is unknown), then alert so the user sets
        # the per-box quantities. IDEMPOTENT: if the incoming number already owns its own row (a prior
        # split already recorded it), update THAT row instead — so a stable rotated number doesn't
        # re-duplicate every run. Exact-key/shipment-line matches reach here; tracking-number matches
        # can't (they matched on an equal number).
        if tracking_hdr_idx is not None:
            existing_trk = existing_row[tracking_hdr_idx].strip() if tracking_hdr_idx < len(existing_row) else ""
            incoming_trk = str(record.get("tracking_number", "")).strip()
            if existing_trk and incoming_trk and existing_trk != incoming_trk:
                owners = [c for c in tracking_to_existing.get((record["order_id"], incoming_trk), [])
                          if c[0] not in claimed_rows]
                if len(owners) == 1:
                    # The new number already has a home row → update that one (keeping its identity).
                    row_number, existing_row = owners[0]
                    if name_hdr_idx < len(existing_row) and str(existing_row[name_hdr_idx]).strip():
                        sheet_row[name_field_idx] = existing_row[name_hdr_idx]
                    if shipment_hdr_idx < len(existing_row) and str(existing_row[shipment_hdr_idx]).strip():
                        sheet_row[shipment_field_idx] = existing_row[shipment_hdr_idx]
                else:
                    label = f"Shipment {_next_shipment_number(record['order_id'], existing, oid_idx, shipment_hdr_idx, appends, oid_field_idx, shipment_field_idx)}"
                    split_row = list(sheet_row)
                    split_row[shipment_field_idx] = label
                    split_row[qty_field_idx] = "*"      # unknown per-box split — user fills it in
                    split_row[total_field_idx] = ""     # can't compute Total Cost without a quantity
                    appends.append(split_row)
                    split_events.append({
                        "order_id": record["order_id"],
                        "item_name": (existing_row[name_hdr_idx] if name_hdr_idx < len(existing_row)
                                      else record.get("item_name", "")),
                        "existing_tracking": existing_trk,
                        "new_tracking": incoming_trk,
                        "new_shipment": label,
                    })
                    continue  # leave the existing box's row untouched

        merged = _merge_row(existing_row, sheet_row)
        # Preserved cells come back as strings from get_all_values(); re-coerce so a kept numeric
        # (e.g. a quantity carried over from a prior run) is written as a number, not text —
        # otherwise Sheets stores it as text and shows a leading-apostrophe '1.
        merged = [_coerce(field, val) for field, val in zip(FIELDNAMES, merged)]
        worksheet.update(range_name=f"A{row_number}", values=[merged])
        claimed_rows.add(row_number)
        written_rows.append(row_number)
        updates += 1

    if appends:
        # Write at an explicit column-A range rather than worksheet.append_rows(): append_rows lets
        # the Sheets API auto-detect the "table" to append after, which on some sheets anchors to the
        # wrong column (observed shifting rows 10 columns right into K:AB). Positioning from column A
        # of the first empty row keeps every row aligned to the header. `existing` was read before any
        # updates and updates never add rows, so len(existing)+1 is the first free row.
        start_row = len(existing) + 1
        worksheet.update(range_name=f"A{start_row}", values=appends)
        written_rows.extend(range(start_row, start_row + len(appends)))

    _write_profit_formulas(worksheet, written_rows)

    if split_events:
        lines = [
            f"- Order {e['order_id']} / {e['item_name']}: existing box {e['existing_tracking']}, "
            f"NEW box {e['new_tracking']} added as {e['new_shipment']} (Quantity set to '*')"
            for e in split_events
        ]
        # Lazy import: keep ledger_sync free of an alerts dependency at module load.
        from alerts.notifier import alert

        alert(
            f"Split shipment detected on {len(split_events)} row(s) — set the quantities",
            "A shipment's tracking number changed to a new value, which usually means the order shipped "
            "in more than one box while the retailer reports only one tracking number at a time. A new "
            "row was added per new box with Quantity '*'. Set the per-box quantities (and adjust the "
            "ORIGINAL row's quantity to match), then verify each tracking number:\n\n" + "\n".join(lines),
        )

    log.info(
        "Sheet sync: %d row(s) updated, %d row(s) appended%s%s.",
        updates,
        len(appends),
        f", {len(split_events)} split-box row(s) added" if split_events else "",
        f", {skipped_blank} skipped (blank Order ID)" if skipped_blank else "",
    )


def _write_profit_formulas(worksheet, row_numbers: list[int]) -> None:
    """(Re)write the Total Profit formula into every row this sync touched — one batched API call.

    Why a SEPARATE write instead of putting the formula in the main row block: the row block is sent
    RAW so the sheet stores values exactly as scraped, which a formula string would land as literal
    text. This call is the only one using USER_ENTERED, and it's scoped to a single column — keeping
    USER_ENTERED away from the data columns, where it would reinterpret long numeric tracking numbers
    as numbers and render them in scientific notation.

    Rewriting on every touch is deliberate: get_all_values() returns a formula cell's EVALUATED text,
    so _merge_row carries that number forward and the RAW row write would replace the formula with a
    frozen value. Re-stamping the formula last restores it.

    A failure here is logged, not raised: the scraped data is already safely written, and the next
    sync re-stamps the formula.
    """
    if not row_numbers:
        return
    col = _COL["total_profit"]
    data = [
        {"range": f"{col}{n}", "values": [[_profit_formula(n)]]}
        for n in sorted(set(row_numbers))
    ]
    try:
        worksheet.batch_update(data, value_input_option="USER_ENTERED")
    except Exception:
        log.exception(
            "Could not write the Total Profit formula into %d row(s); the row data itself was "
            "written and the next sync will restore the formula.", len(data),
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


def plan_buying_group_retag(header: list[str], data_rows: list[list[str]], warehouses) -> dict:
    """Read-only: work out what a retroactive Buying Group classification pass would do to rows
    ALREADY on the sheet, without writing anything. `apply_buying_group_retag` (or a caller script)
    turns this plan into real writes/deletes.

    This exists because the classifier only tags NEW/re-checked rows at scrape time (main.run_scrape) —
    rows recorded before the Buying Group column existed, or before warehouses.json had an entry that
    now matches them, are never revisited automatically. This is the one-off backfill.

    `header` is the sheet's CURRENT header row (may predate the Buying Group column — that's reported
    via `needs_header_migration`, not assumed). `data_rows` is `existing[1:]` (no header). Rows with a
    blank Order ID are skipped, same rule as sync_csv_to_sheet.

    Returns:
        {
          "needs_header_migration": bool,
          "updates": [(row_number, order_id, item_name, old_tag, new_tag), ...],
          "deletions": [(row_number, order_id, item_name, delivery_address, old_tag), ...],  # Personal
          "unchanged": int,
          "group_counts": {tag: count},  # post-classification, excluding deletions
        }
    """
    needs_header_migration = "Buying Group" not in header
    oid_idx = header.index("Order ID")
    name_idx = header.index("Item Name")
    addr_idx = header.index("Delivery Address")
    bg_idx = None if needs_header_migration else header.index("Buying Group")

    updates: list[tuple] = []
    deletions: list[tuple] = []
    group_counts: dict[str, int] = {}
    unchanged = 0

    for offset, row in enumerate(data_rows):
        row_number = offset + 2  # row 1 is the header
        oid = row[oid_idx].strip() if oid_idx < len(row) else ""
        if not oid:
            continue
        name = row[name_idx].strip() if name_idx < len(row) else ""
        addr = row[addr_idx].strip() if addr_idx < len(row) else ""
        old_tag = row[bg_idx].strip() if bg_idx is not None and bg_idx < len(row) else ""
        new_tag = classify_address(addr, warehouses)

        if is_personal(new_tag):
            deletions.append((row_number, oid, name, addr, old_tag))
            continue

        display_tag = new_tag or old_tag or "(blank)"
        group_counts[display_tag] = group_counts.get(display_tag, 0) + 1
        if new_tag and new_tag != old_tag:
            updates.append((row_number, oid, name, old_tag, new_tag))
        else:
            unchanged += 1

    return {
        "needs_header_migration": needs_header_migration,
        "updates": updates,
        "deletions": deletions,
        "unchanged": unchanged,
        "group_counts": group_counts,
    }


def load_order_state(profile_label: str | None = None, since: str | None = None,
                     retailer: str | None = None) -> dict:
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

    `retailer` (a retailer_name like "Amazon Business") scopes the state to that retailer's rows. This
    MATTERS when one profile hosts several retailers (e.g. profile-alpha = Best Buy + Costco + Amazon
    Business): without it, a scraper's re-check would pull in the OTHER retailers' open orders and
    re-read them through its own path — the agent fallback would open their order_url and re-emit those
    orders under the wrong retailer, corrupting the ledger (and, because the upsert key has no retailer
    field, overwriting the real rows). Omitted (single-retailer profiles) = no filtering, as before.

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
    # Retailer scoping (multi-retailer profiles): filter to this retailer's rows only.
    retailer_idx = header.index("Retailer") if "Retailer" in header else None

    orders: dict[str, dict] = {}
    for row in existing[1:]:
        if len(row) <= max(idx.values()):
            continue
        if profile_label is not None and row[idx["Profile"]] != profile_label:
            continue
        if (
            retailer is not None
            and retailer_idx is not None
            and retailer_idx < len(row)
            and row[retailer_idx].strip() != retailer
        ):
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
