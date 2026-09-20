"""The ledger upsert: how a scrape's rows land in the SQLite ledger.

Every writer here addresses the ledger as a positional grid through the worksheet face
ledger_db/worksheet.py puts on data/ledger.sqlite3 (`_get_worksheet` opens it; the buying-group
sync, the BFMR auto-reply and the repair scripts open it through the same function). The
adapter keeps the worksheet contract every writer here was written against, down to the empty
string (a formatted read hands back text, a blank is "").
"""

import csv
import logging
import re
from pathlib import Path

from alerts.notifier import alert
from config.settings import settings
from config.warehouses import classify_address, is_deliberately_unrouted, is_personal
from models.order import (
    FIELDNAMES, MONEY_FREE_STATUSES, RETIRED_STATUSES, TERMINAL_STATUSES, shipment_label,
)

log = logging.getLogger(__name__)

# A Shipment cell in either spelling: the current bare "2" or the pre-2026-08-12 "Shipment 2".
_SHIPMENT_NUMBER = re.compile(r"(?:shipment\s*)?(\d+)", re.IGNORECASE)

# Display names for the ledger's header row, positionally 1:1 with models.order.FIELDNAMES — rows are
# written positionally from column A, so the two lists must stay the same length and order.
# tests/test_schema.py pins both lists in full, so any reorder fails loudly and forces the author to
# think about the ledger file's column migration (ledger_db/store.py copies by NAME on open) rather
# than silently scrambling existing rows. ADDING a column means appending to BOTH lists.
HEADER = [
    # --- what it is ---
    "Order Date",
    "Status",  # PINNED AT COLUMN B (nothing needs it there any more; kept because nothing is
               # gained by moving it and the formula letters are pinned)
    "Retailer",
    "Item Name",
    "Shipment",  # bare number ("1", "2"), not "Shipment 1" — the column heading already says it
    "Quantity",
    # --- what happened to it, in the order it happens ---
    "Order ID",
    "Tracking Number",
    "Tracking Submitted",  # a checkbox; ticked by sync_tracking.py when a group accepts the number.
                           # Kept directly after the number it refers to.
    "Delivery Date",
    "Buying Group",  # where the package went, and the key sync_tracking.py routes on. DERIVED from
                     # Delivery Address (config.warehouses.classify_address).
    # --- what it cost, left to right in the order you reason about it ---
    "Cost Per Item",
    "Total Cost",  # = Quantity x Cost Per Item, so it sits directly after both
    "Shipping",  # this row's cost-weighted SHARE of the order-level total
    # Each is this row's cost-weighted SHARE of the order-level total, prorated exactly like
    # Shipping above; the COGS formula reads both.
    "Sales Tax",   # real acquisition cost: ADDED in COGS (usually 0 under the resale certificate)
    "Gift Card",   # tender the card never spent: SUBTRACTED in COGS (earns no cashback either)
    "Rewards Used",  # Amazon rewards SPENT on the order (cash back / points): kept IN cost,
                     # out of the cashback basis. Sits by Gift Card — both are order-level
                     # tender the card never spent. See models/order.py.
    "Card",  # derived from Card Last 4 (config.cards.resolve_card)
    "Cashback Rate",  # decimal fraction (0.02) — format the column as a percentage to taste
    "COGS",  # DERIVED: the adapter computes it per row (_cogs_formula is the definition): cost + shipping, net of cashback
    # --- what came back ---
    "Insurance",  # a buying-group premium — an EXPENSE, deliberately not part of COGS
    "Expected Payout",  # the group's COMMITTED payout (sync_tracking), kept apart from the paid one
    "Actual Payout",  # what the group PAID (was "Payout Amount" until 2026-09-18)
    "Payout Date",
    # A partial return is ONE hand edit, kept beside the payout it corrects: Quantity / Total Cost
    # stay GROSS and the COGS formula nets Return Qty x Cost Per Item out (see models/order.py).
    "Return Qty",   # units netted OUT of this row
    "Return Date",
    "Total Profit",  # DERIVED: the adapter computes it per row (_profit_formula is the definition): Payout - COGS - Insurance
    # --- reference / audit ---
    "Profile",  # which browser profile scraped it — never read while reconciling
    "Order Link",
    "Tracking Link",
    "Receipt Link",  # the order's captured receipt in object storage (receipts/capture.py)
    "Delivery Address",  # the raw address Buying Group was classified from
    "Card Last 4",
    "Package ID",  # the retailer's own per-package identity (Amazon shipmentId / Costco packageNumber /
                   # Best Buy groupId): matched on before the tracking number. TEXT, never typed.
                   # Added 2026-09-09, moved before Last Scraped At 2026-09-10 — see models/order.py.
    "Last Scraped At",
]

# Numeric columns get coerced to numbers so the ledger holds real numbers, not text. total_profit is
# deliberately absent: it is a derived column, never written as a number.
#
# "shipment" is here for a different reason than the rest: it's not summed, it's a plain 1-based index
# ("1", "2", ...) that should just look like the number it is, with no leading apostrophe. Unlike
# card_last4 (also digit-only, but where a leading zero is real data — "0315" must stay "0315"),
# shipment numbers never have meaningful leading zeros, so coercing to int is always safe. The rare
# non-numeric fallback label (see models.order.normalize_shipment) isn't a number, so _parse_display_
# number returns None for it and _coerce leaves it as text untouched — no data is lost.
_NUMERIC_FIELDS = {
    "quantity", "cost_per_item", "shipping", "total_cost",
    "cashback_rate", "insurance", "payout_amount", "expected_payout",
    "shipment", "return_quantity", "gift_card", "sales_tax", "rewards_used",
}

# Fields that must be a plain int rather than a float when coerced (quantity: "3", not "3.0"; shipment:
# "2", not "2.0"). Every other numeric field is a currency/rate amount, where a float is correct.
_INT_FIELDS = {"quantity", "shipment", "return_quantity"}

# Fields stored as a real BOOLEAN, so the checkbox column actually ticks.
#
# This needs its own coercion because the round trip would otherwise destroy it. Rows are read
# FORMATTED, where a boolean cell comes back as the STRING "TRUE"; _merge_row carries that string
# forward for any column the scrapers leave blank (which is all of them here); and the row is written
# back RAW, which stores a string as a string. The checkbox would silently turn into the text "TRUE"
# on the first sync after it was ticked. Coercing on write is what keeps the cell a boolean.
#
# Blank stays BLANK rather than becoming False, so _merge_row's blank-never-overwrites rule still
# protects a ticked box against a scraper that knows nothing about this column.
_BOOL_FIELDS = {"tracking_submitted"}
_TRUE_TEXT = {"true", "yes", "y", "1", "checked"}
_FALSE_TEXT = {"false", "no", "n", "0", "unchecked"}


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


def _cogs_formula(row_number: int) -> str:
    """The live COGS (Cost of Goods Sold) formula for one row.

        COGS = (Total Cost - Return Qty x Cost Per Item - Gift Card + Shipping + Sales Tax
                - Rewards Used) * (1 - Cashback Rate) + Rewards Used

    REWARDS USED STAY IN THE COST. Amazon rewards spent on an order — a
    Prime cash-back balance or Amazon points — are taken OUT of the parenthesis and ADDED BACK
    outside it: the order still costs its full sticker (the reward is netted from COGS at year end,
    outside this ledger, so netting it here too would count it twice — an all-points order would
    show a $0 cost AND a year-end rewards deduction), while the card earns no cashback on dollars
    it never paid. A blank cell is 0, so every pre-existing row computes the identical number.

    RETURNS ARE NETTED HERE, IN THE FORMULA. Quantity and Total Cost stay
    the GROSS bought numbers the scraper wrote -- putting a formula in those cells would freeze to a
    number on the next positional row write (the §8 bug class), and the money-path weights read
    them as values. Recording a partial return is therefore ONE hand edit: type Return Qty (and
    Return Date); the returned units leave the cost basis right here, and Total Profit follows
    through the COGS cell. A blank Return Qty multiplies as zero, so untouched rows are unchanged.
    The payout side nets itself for BFMR (its amount_paid is already net of clawbacks, allocated
    per order); a MOD return's payout stays hand-entered, as MOD has no return signal.

    GIFT CARD AND SALES TAX ARE NETTED HERE TOO, for the same reason and
    with the same blank-is-zero property, so every pre-existing row computes the identical number.
    A gift card is a tender the card never spent: it leaves the cost basis (the gift-card PURCHASE
    has its own ledger row -- the record-cost-once rule) and, being inside the parenthesis, earns no
    cashback. This replaces the old scheme where the Amazon mappings silently scaled Cost Per Item /
    Total Cost down to the card-paid share and threw the amount away -- algebraically the same COGS,
    but now Total Cost matches the retailer's page and the amount is visible. Sales tax is a real
    acquisition cost (usually 0 under the resale certificate, but hand-kept orders pay it) and sits
    inside the parenthesis because the card is charged tax and earns cashback on it. Both cells
    hold this row's cost-weighted share (see _reprorate_order_level). No floor/cap is needed: a
    gift card can cover at most cost+shipping+tax, so COGS bottoms out at 0, never negative -- the
    old "cap at the pre-tax basis" rule existed only because tax wasn't recorded.

    THE CASHBACK IS NETTED INTO COST, not counted as income. Card rewards earned on a purchase are a
    purchase-price adjustment rather than receipts, so this is the characterisation a Schedule C
    wants — and it is why this column exists at all: it is the year-end cost figure, ready to SUMIF.

    INSURANCE IS DELIBERATELY ABSENT. A buying-group premium is an ordinary business expense, not part
    of the cost of the goods; folding it in here would overstate COGS and understate expenses, which
    are separate lines on the form. _profit_formula subtracts it separately.

    Shipping is read directly and is already this row's cost-weighted SHARE (see
    _reprorate_order_level), so there is nothing to divide out — the same reasoning as
    _profit_formula.

    A CANCELLED ROW REPORTS NOTHING. The order was refunded, so no cost was ever incurred and it
    must not reach the year-end cost side — but the row itself stays for bookkeeping (see
    _blank_money_for_cancelled, which empties the scraped money cells behind it). The guard is a
    formula rather than a one-off cleanup so it also covers every FUTURE cancellation, and it reads
    the Status cell through _COL rather than a literal column letter, so a reorder can't leave it
    pointing at the wrong column.

    UNLIKE _profit_formula this does NOT blank out on a row with no payout. The cost was incurred
    whether or not the buying group has paid yet, and the year-end cost side has to count it; gating
    it on payout would silently drop every not-yet-paid order from the total. It blanks only when
    Total Cost itself is blank, i.e. there is no cost to report.
    """
    n = row_number
    cost, ship, rate = _COL["total_cost"], _COL["shipping"], _COL["cashback_rate"]
    ret_qty, unit = _COL["return_quantity"], _COL["cost_per_item"]
    gift, tax, rew = _COL["gift_card"], _COL["sales_tax"], _COL["rewards_used"]
    status = _COL["status"]
    return (
        f'=IF({status}{n}="cancelled","",'
        f'IF({cost}{n}="","",'
        f'IFERROR(({cost}{n}-{ret_qty}{n}*{unit}{n}-{gift}{n}+{ship}{n}+{tax}{n}-{rew}{n})'
        f'*(1-{rate}{n})+{rew}{n},"")))'
    )


def _profit_formula(row_number: int) -> str:
    """The live Total Profit formula for one row.

        Total Profit = Actual Payout - COGS - Insurance

    This READS THE COGS CELL rather than re-deriving cost from its parts. It is algebraically the
    same number as the older self-contained version — expand COGS and you get
    `payout + (cost+ship)*rate - cost - ship - insurance` exactly — but with one expression of the
    cost side instead of two that could drift apart. It also makes the row read the way the money
    actually works: what came in, minus what the goods cost, minus the fee.

    It's a formula, not a Python-computed number, because Insurance and Actual Payout arrive after the
    scrape (from the buying-group sync, or typed in). A value computed at scrape time would be stale
    the moment either lands, and a delivered row is terminal — never re-scraped — so it would stay
    stale forever.

    Returns "" (blank cell, not 0) until Actual Payout is filled, so an un-paid-out row doesn't
    display a large fake loss that would poison a column sum. COGS deliberately does NOT do this.

    A CANCELLED ROW also reports nothing, for the same reason COGS does — see _cogs_formula.
    """
    n = row_number
    cogs, ins, payout = _COL["cogs"], _COL["insurance"], _COL["payout_amount"]
    status = _COL["status"]
    return (
        f'=IF({status}{n}="cancelled","",'
        f'IF({payout}{n}="","",IFERROR({payout}{n}-{cogs}{n}-{ins}{n},"")))'
    )

# Furthest-along status wins when two rows of ONE shipment are collapsed in a single sync (see
# _collapse_records). Mirrors _rollup_status's spirit: cancelled overrides, then the buying-group
# outcomes, then delivered, then shipped, then ordered.
#
# EVERY member of STATUSES must appear here. The lookup falls back to -1, so a status missing from
# this map can never win a collapse AND loses to "ordered" (rank 0) — i.e. a row that a buying group
# had just marked "paid" would be quietly demoted back to "ordered" and re-enter the re-check list
# forever. That is exactly what happened to "paid"/"return" between their introduction and this line.
#
# "return" outranks "paid" because a return REVERSES a payment: when a single sync somehow carries
# both, the reversal is the one the user needs to see. "cancelled" stays on top as the order-level
# override it has always been.
#
# "superseded" sits ABOVE delivered so no re-scrape can ever walk a retired row back to a live
# status, and BELOW the buying-group outcomes so "a group's money outcome is never walked back"
# stays literally true. Only the relative order matters -- nothing pins the numbers.
_STATUS_RANK = {
    "ordered": 0, "shipped": 1, "delivered": 2, "superseded": 3, "paid": 4, "return": 5,
    "cancelled": 6,
}


# The retailer's own item number, as the Costco mapping writes it into the item name
# ("... (Item #2042809)"). Costco serves DIFFERENT descriptions for one SKU depending on the order's
# state — the marketing name while open, the terse warehouse name once cancelled — so the name is not a stable identity there, but the item number is.
_ITEM_NUMBER_RE = re.compile(r"\(Item #(\d+)\)\s*$")


def _item_number_of(name) -> str:
    m = _ITEM_NUMBER_RE.search(str(name or ""))
    return m.group(1) if m else ""


def _record_key(rec: dict) -> tuple:
    return (rec.get("order_id", ""), rec.get("order_date", ""), rec.get("item_name", ""),
            rec.get("shipment", ""))


# The two fields same-key records may legitimately disagree on: status is resolved by rank in
# _collapse_records, and the scrape timestamp is per read.
_COLLAPSE_FREE_FIELDS = {"status", "last_scraped_at"}


def _same_value(a: str, b: str) -> bool:
    na, nb = _parse_display_number(a), _parse_display_number(b)
    if na is not None and nb is not None:
        return abs(na - nb) < 1e-9
    return a == b


def _find_key_conflicts(records: list[dict]) -> list[dict]:
    """Same-key records that CONTRADICT each other -- two real rows, not two half-rows.

    _collapse_records exists for half-rows: two reads of ONE shipment that each know part of it
    (blank versus value), which merge cleanly. Two records that both carry a value for the same
    field and DISAGREE are something else entirely -- two distinct lines the mapping failed to tell
    apart -- and merging them keeps one and silently loses the other's money. That is exactly what
    happened three times (a split quantity, two badged qty-3 blocks, the same item from two sellers
    at two prices; the design notes-09-04/08): every producer is now a single deterministic
    mapping that parses each order once, so a contradiction on one key can only be an unknown page
    shape. Returns one entry per conflicting key: {"key", "fields": [(field, first, other), ...]}.
    """
    first_by_key: dict[tuple, dict] = {}
    conflicts: dict[tuple, list] = {}
    for rec in records:
        key = _record_key(rec)
        first = first_by_key.setdefault(key, rec)
        if first is rec:
            continue
        for field, val in rec.items():
            if field in _COLLAPSE_FREE_FIELDS:
                continue
            a, b = str(first.get(field, "") or "").strip(), str(val or "").strip()
            if a and b and not _same_value(a, b):
                conflicts.setdefault(key, []).append((field, a, b))
    return [{"key": key, "fields": fields} for key, fields in conflicts.items()]


def _alert_key_conflicts(conflicts: list[dict]) -> None:
    lines = [
        f"- Order {c['key'][0]} / {str(c['key'][2])[:40]!r} shipment {c['key'][3]}: "
        + "; ".join(f"{field} {a!r} vs {b!r}" for field, a, b in c["fields"])
        for c in conflicts
    ]
    log.error("Same-key conflict on %d order line(s) -- NOT written: %s", len(conflicts), lines)
    from alerts.notifier import alert  # lazy: keeps ledger_sync free of an alerts dependency at load

    alert(
        f"Same-key conflict on {len(conflicts)} order line(s) — NOT recorded",
        "Two rows from one scrape landed on the same ledger key (Order ID + Order Date + Item Name + "
        "Shipment) with DIFFERENT values, which means the page holds two lines the parser could not "
        "tell apart -- an unknown page shape. Merging them would silently drop one line's money "
        "(this is how a $649 iPad once vanished), so NEITHER row was written; every other row in the "
        "run was. Fix the mapping from the failure dossier, then the next run records the order:\n\n"
        + "\n".join(lines),
    )


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


def _get_worksheet():
    """The ledger file (`database.path`) behind a worksheet face (ledger_db/worksheet.py). Every
    writer in this module, the buying-group sync, the BFMR auto-reply and the scripts open the
    ledger HERE."""
    from ledger_db.store import LedgerDb
    from ledger_db.worksheet import DbWorksheet

    return DbWorksheet(LedgerDb(settings.ledger_db_path))


def _parse_display_number(value: str):
    """Parse a number that may be wearing the ledger's DISPLAY formatting. None if it isn't a number.

    This matters because of a round trip that is easy to miss: `get_all_values()` returns FORMATTED
    text, `_merge_row` PRESERVES an existing cell whenever the incoming value is blank (which is the
    whole point of partial re-checks), and the preserved value is then written straight back. So a
    percent-formatted Cashback Rate reads as "4%", and a currency-formatted Total Cost as "$3,402.00" —
    and without this, plain float() fails and the cell is rewritten as literal TEXT. A text rate breaks
    the Total Profit formula's arithmetic; a text cost stops the column summing. Formatting a column is
    something a user does for readability and should never silently corrupt the value underneath.

    Handles: currency symbols and spaces, thousands separators, a trailing % (as /100, so "4%" -> 0.04),
    and accounting-style negatives "(1.23)" -> -1.23.
    """
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
    percent = text.endswith("%")
    if percent:
        text = text[:-1].strip()
    # Strip everything that is decoration rather than magnitude (currency symbols, spaces, commas).
    cleaned = "".join(c for c in text if c.isdigit() or c in ".-+")
    if not cleaned or cleaned in ("-", "+", "."):
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    if percent:
        number /= 100
    return -number if negative else number


def _parse_checkbox(value):
    """Read a checkbox cell in any spelling it can arrive in -> True / False / "" (untouched).

    A boolean column reaches us three ways and they all have to land on the same value: a real
    boolean from an UNFORMATTED read, the string "TRUE" from a FORMATTED one, and whatever a human
    typed into the cell before ticking it. Blank returns blank, NOT False — that distinction is what
    lets _merge_row leave a ticked box alone when a scraper sends nothing for the column.
    """
    if isinstance(value, bool):
        return value
    text = str(value if value is not None else "").strip().lower()
    if not text:
        return ""
    if text in _TRUE_TEXT:
        return True
    if text in _FALSE_TEXT:
        return False
    return value  # unrecognised: hand it back rather than guessing a tick


def _coerce(field: str, value: str):
    if field in _BOOL_FIELDS:
        return _parse_checkbox(value)
    if field in _NUMERIC_FIELDS:
        number = _parse_display_number(value)
        if number is None:
            return value
        return int(number) if field in _INT_FIELDS else number
    return value


def _next_shipment_number(order_id, existing, oid_hdr_idx, shipment_hdr_idx,
                          appends, oid_field_idx, shipment_field_idx) -> int:
    """Highest shipment number seen for this order (across existing rows AND rows already queued
    to append this sync) + 1 — a unique, stable label for a newly-detected split box.

    Accepts BOTH the current bare "2" and the pre-2026-08-12 "Shipment 2" wording, so a ledger that
    still holds old-style labels (or a row an agent wrote in the labelled form) can't make this
    restart at 2 and collide with an existing box."""
    nums = [1]  # so the first extra box becomes at least "2" even if labels don't parse
    for row in existing[1:]:
        if oid_hdr_idx < len(row) and row[oid_hdr_idx] == order_id and shipment_hdr_idx < len(row):
            m = _SHIPMENT_NUMBER.match(str(row[shipment_hdr_idx]).strip())
            if m:
                nums.append(int(m.group(1)))
    for row in appends:
        if oid_field_idx < len(row) and row[oid_field_idx] == order_id:
            m = _SHIPMENT_NUMBER.match(str(row[shipment_field_idx]).strip())
            if m:
                nums.append(int(m.group(1)))
    return max(nums) + 1


def _package_id_of(row: list, package_hdr_idx: int | None) -> str:
    """The row's Package ID cell as stripped text, "" when the column or the cell is absent."""
    if package_hdr_idx is None or package_hdr_idx >= len(row):
        return ""
    return str(row[package_hdr_idx] if row[package_hdr_idx] is not None else "").strip()


def _ensure_grid_cols(worksheet) -> None:
    """Grow the grid to len(HEADER) columns before anything writes a full-width row.

    The worksheet contract has a FIXED grid, and a write past it fails — the column twin of the
    append failure _ensure_grid_rows exists for, and exactly what the first sync after a column
    append (grid 30 -> 32, 2026-08-30) would hit on an older grid. Growing is safe and
    idempotent: new columns arrive empty, existing cells don't move.
    """
    current = getattr(worksheet, "col_count", None)
    if current is not None and current < len(HEADER):
        worksheet.add_cols(len(HEADER) - current)


#: The fields that describe the CARD an order was paid with -- one card pays an order, so a value
#: the user corrected on one of its rows holds for every row of it, whichever retailer.
_ORDER_CARD_FIELDS = ("cashback_rate", "card_name", "card_last4")


def _inherit_hand_edited_card_fields(worksheet, appends: list[list], existing: list[list],
                                     protected: dict) -> list[list]:
    """A row appended for an order whose rows carry a HAND-EDITED card field takes that value too,
    recorded as a hand edit of the new row so later runs keep it (and clearing it restores what
    the run would have written). order 111-9990023-9990023's Cashback Rate was
    set to 6% by hand on Shipment 1, and the split's new rows came in at the card's configured 5%.
    Only when every hand-edited value of that field on the order agrees -- a disagreement is the
    user's to settle -- and never for the key fields, which `record` refuses anyway."""
    if not appends or not protected:
        return appends
    oid_idx = FIELDNAMES.index("order_id")
    by_key = {_row_key(r): r for r in existing[1:] if r}
    agreed: dict[tuple[str, str], set[str]] = {}
    for key, fields in protected.items():
        source = by_key.get(key)
        if source is None:
            continue
        for field in _ORDER_CARD_FIELDS:
            if field in fields:
                i = FIELDNAMES.index(field)
                value = str(source[i] if i < len(source) and source[i] is not None else "").strip()
                if value:
                    agreed.setdefault((key[0], field), set()).add(value)
    if not agreed:
        return appends
    from ledger_db.hand_edits import ledger_db_of, record

    db = ledger_db_of(worksheet)
    for row in appends:
        order_id = str(row[oid_idx] if row[oid_idx] is not None else "").strip()
        for field in _ORDER_CARD_FIELDS:
            values = agreed.get((order_id, field))
            if not values or len(values) != 1:
                continue
            i = FIELDNAMES.index(field)
            inherited = next(iter(values))
            previous = row[i]
            row[i] = _coerce(field, inherited)
            log.info("Order %s: appended row inherits the hand-edited %s %r.", order_id, field, inherited)
            if db is not None:
                record(db, _row_key(row), field, inherited,
                       previous="" if previous is None else previous)
    return appends


def _protected_cells(worksheet) -> dict:
    """{row key: fields} the user typed by hand (ledger_db/hand_edits); {} for a fake worksheet."""
    from ledger_db.hand_edits import protected_fields

    return protected_fields(worksheet)


def _row_key(values: list) -> tuple:
    from ledger_db.hand_edits import key_of_row

    return key_of_row(values)


def sync_csv_to_ledger(csv_path: Path) -> None:
    worksheet = _get_worksheet()
    _ensure_grid_cols(worksheet)
    # Cells the user typed on the dashboard: the merge below keeps them whatever a scrape says.
    #
    protected = _protected_cells(worksheet)
    existing = worksheet.get_all_values()
    # An empty-but-existing worksheet returns [] or a single blank row like [[]] — both mean
    # "no header yet", so (re)write our header into row 1.
    if not existing or not any(cell.strip() for cell in existing[0]):
        worksheet.update(range_name="A1", values=[HEADER])
        existing = [HEADER]

    header = existing[0]
    # Migrate an older ledger whose header is a PREFIX of the current HEADER. Columns are only ever
    # APPENDED (Shipment, then Buying Group), so a pre-migration ledger's header is HEADER truncated at
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
            f"Worksheet '{getattr(worksheet, "title", "ledger")}' first row is not a recognized "
            f"header (missing {missing}). Clear the ledger, or set its header row to: {HEADER}"
        )
    # THE ORDER MUST MATCH EXACTLY, not merely contain the right names. Rows are written positionally
    # from column A, so a ledger holding the right columns in a DIFFERENT order (e.g. one written before
    # the 2026-08-12 reorder) would read fine by name here and then be overwritten with values in the
    # new order — silently scrambling every field of every row it touched. That is the single worst
    # failure this module can have, and no runtime error would announce it. Refuse instead, and point
    # at the migration that rewrites the existing rows.
    if header != list(HEADER):
        raise RuntimeError(
            f"Worksheet '{getattr(worksheet, "title", "ledger")}' has the ledger's columns in a "
            "different ORDER than the current schema, so writing to it positionally would scramble "
            "existing rows. Nothing was written. The ledger file migrates its own columns on open "
            f"(ledger_db/store.py), so this should not happen.\n  ledger:   {header}\n"
            f"  expected: {list(HEADER)}"
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
    tracking_field_idx = FIELDNAMES.index("tracking_number")
    submitted_field_idx = FIELDNAMES.index("tracking_submitted")
    # Tracking-number index for the tracking-based deferral (Order ID + Tracking Number). The carrier
    # tracking number is an identity BOTH the API and the agent read identically, so it reconciles rows
    # even when their synthetic Shipment numbers diverge (e.g. Costco: the API numbers shipments by
    # tracking sort, the agent numbers top-to-bottom).
    tracking_hdr_idx = header.index("Tracking Number") if "Tracking Number" in header else None
    # Package-id index for the package deferral (Order ID + Package ID) — the retailer's OWN identity
    # for the physical package (Amazon shipmentId, Costco packageNumber, Best Buy groupId), which is
    # what the Shipment ordinal only approximates. Tried before the tracking number: a re-issued
    # label changes the number but, on Costco and Best Buy at least, not the package id.
    package_hdr_idx = header.index("Package ID") if "Package ID" in header else None

    # THE PRESERVED CELLS MUST COME FROM THE UNFORMATTED READ. `existing` is the FORMATTED grid --
    # what a human sees -- and it is the right thing to build the upsert key from (that key must be
    # display-stable). But _merge_row also carries a matched row's UNTOUCHED cells forward from it,
    # and a display format can lose information on the way: a 0-decimal currency shows 1300.45 as
    # "1300", a 0-decimal percent shows 0.0375 as "4%", and a custom format that renders a number as
    # nothing turns a hand-typed Actual Payout into "" -- which the next re-check then writes back,
    # erasing it. So numeric and checkbox cells are preserved from the stored VALUES instead; text
    # and date columns keep coming from the formatted grid (Order Date is in the key). Best-effort:
    # if the second read fails, preserved cells fall back to the formatted text, as before.
    raw_grid = _read_unformatted(worksheet)

    key_to_existing: dict[tuple, tuple[int, list]] = {}
    shipment_to_existing: dict[tuple, list[tuple[int, list]]] = {}
    tracking_to_existing: dict[tuple, list[tuple[int, list]]] = {}
    package_to_existing: dict[tuple, list[tuple[int, list]]] = {}
    placeholder_to_existing: dict[tuple, list[tuple[int, list]]] = {}  # (order, date, item name)
    itemno_to_existing: dict[tuple, list[tuple[int, list]]] = {}  # (order, date, shipment, item number)
    status_hdr_idx = header.index("Status") if "Status" in header else None
    for row_number, row in enumerate(existing[1:], start=2):
        # Rows written before Shipment existed are shorter than key_idx; read missing cells as ""
        # (never skip them, or pre-migration rows would fail to match and duplicate on re-check).
        if oid_idx >= len(row) or not row[oid_idx].strip():
            continue
        # A RETIRED row (superseded, the design notes) is never a merge target: it is a closed record of a
        # dead tracking number. Left in these indices, a future genuine box carrying its Shipment
        # number -- or, for the tracking index, its number -- would merge onto it, and the
        # money-blanking below would then erase that box's cost. _next_shipment_number still scans
        # `existing`, so a split-box append can never reuse the retired row's number either.
        if status_hdr_idx is not None and status_hdr_idx < len(row) \
                and str(row[status_hdr_idx]).strip().lower() in RETIRED_STATUSES:
            continue
        key = tuple(row[i] if i < len(row) else "" for i in key_idx)
        key_to_existing[key] = (row_number, row)
        skey = tuple(row[i] if i < len(row) else "" for i in skey_idx)
        shipment_to_existing.setdefault(skey, []).append((row_number, row))
        if tracking_hdr_idx is not None:
            trk = row[tracking_hdr_idx].strip() if tracking_hdr_idx < len(row) else ""
            if trk:
                tracking_to_existing.setdefault((row[oid_idx], trk), []).append((row_number, row))
        pid = _package_id_of(row, package_hdr_idx)
        if pid:
            package_to_existing.setdefault((row[oid_idx], pid), []).append((row_number, row))
        # A PRE-SHIP PLACEHOLDER: still `ordered`, with neither a tracking number nor a package id
        # (Amazon's pre-ship links carry no shipmentId). Its Shipment number is the one thing the
        # retailer is still free to change — see DEFER (3) below.
        placeholder_trk = (row[tracking_hdr_idx].strip()
                           if tracking_hdr_idx is not None and tracking_hdr_idx < len(row) else "")
        if (status_hdr_idx is not None and status_hdr_idx < len(row)
                and str(row[status_hdr_idx]).strip().lower() == "ordered"
                and not placeholder_trk and not pid):
            placeholder_to_existing.setdefault(key[:3], []).append((row_number, row))
        item_number = _item_number_of(row[name_hdr_idx] if name_hdr_idx < len(row) else "")
        if item_number:
            itemno_to_existing.setdefault((key[0], key[1], key[3], item_number), []).append((row_number, row))

    with csv_path.open(newline="", encoding="utf-8") as f:
        records = list(csv.DictReader(f))

    # Two records on ONE key that contradict each other are two real rows the mapping could not
    # tell apart (see _find_key_conflicts). Neither is written -- a merge would silently lose one
    # line's money -- the rest of the batch is, and the alert names them.
    conflicts = _find_key_conflicts(records)
    if conflicts:
        bad_keys = {c["key"] for c in conflicts}
        records = [r for r in records if _record_key(r) not in bad_keys]
        _alert_key_conflicts(conflicts)

    # Collapse same-key rows (CDP read + agent re-read of one shipment) before upserting, so the
    # two half-rows merge into one instead of the later overwriting the earlier's tracking number.
    collapsed = _collapse_records(records)
    # How many incoming rows target each shipment line — the deferral only fires in the unambiguous
    # 1:1 case (see below), so a shipment carrying two distinct products doesn't mis-merge.
    incoming_skey_count: dict[tuple, int] = {}
    incoming_tkey_count: dict[tuple, int] = {}
    incoming_pkey_count: dict[tuple, int] = {}   # (order, package id)
    incoming_pname_count: dict[tuple, int] = {}  # (order, package id, item name) — a multi-SKU carton
    incoming_pids_by_order: dict[str, set] = {}  # which packages this batch says are ON THE PAGE
    incoming_keys = {_record_key(rec) for rec in collapsed}  # every exact key this page still shows
    incoming_itemno_count: dict[tuple, int] = {}  # (order, date, shipment, item number)
    # Which SHIPMENTS each incoming tracking number claims, per order — the mis-read detector below.
    incoming_tracking_shipments: dict[tuple, set] = {}
    for rec in collapsed:
        oid = str(rec.get("order_id", "")).strip()
        if oid:
            sk = (rec.get("order_id", ""), rec.get("order_date", ""), rec.get("shipment", ""))
            incoming_skey_count[sk] = incoming_skey_count.get(sk, 0) + 1
            trk = str(rec.get("tracking_number", "")).strip()
            if trk:
                tk = (rec.get("order_id", ""), trk)
                incoming_tkey_count[tk] = incoming_tkey_count.get(tk, 0) + 1
                incoming_tracking_shipments.setdefault(tk, set()).add(rec.get("shipment", ""))
            item_number = _item_number_of(rec.get("item_name", ""))
            if item_number:
                ik = (rec.get("order_id", ""), rec.get("order_date", ""), rec.get("shipment", ""), item_number)
                incoming_itemno_count[ik] = incoming_itemno_count.get(ik, 0) + 1
            pid = str(rec.get("package_id", "")).strip()
            if pid:
                pk = (rec.get("order_id", ""), pid)
                incoming_pkey_count[pk] = incoming_pkey_count.get(pk, 0) + 1
                pn = (rec.get("order_id", ""), pid, rec.get("item_name", ""))
                incoming_pname_count[pn] = incoming_pname_count.get(pn, 0) + 1
                incoming_pids_by_order.setdefault(rec.get("order_id", ""), set()).add(pid)

    # ONE TRACKING NUMBER ON TWO DIFFERENT BOXES OF ONE ORDER IS A SCRAPER MIS-READ, NOT A SPLIT.
    # Two SKUs sharing a carton legitimately share both a tracking number AND a Shipment number
    # (Shipment is numbered per physical package), so differing Shipment values are the tell.
    #
    # OBSERVED LIVE: the Costco AGENT fallback returned …348 for both boxes of order
    # 1399000007, losing …357. Shipment 2's number therefore "changed", which is exactly the
    # undisclosed-split signature — so the safety net below appended a phantom Quantity "*" row for a
    # box that does not exist, left the real Shipment 2 untouched, and gave …348 two owning rows
    # (which would go on to break the tracking-number defer rule on every later run). The API path
    # read both numbers correctly minutes earlier, so this is the agent misreading, and the ledger
    # must not turn a bad read into a permanent fictitious row.
    #
    # Scoped to the whole ORDER, not the one number: a path that repeated one number has shown it
    # cannot be trusted about that order's tracking at all.
    suspect_orders = {
        order_id for (order_id, _trk), shipments in incoming_tracking_shipments.items()
        if len(shipments) > 1
    }
    suspect_tracking: list[dict] = []
    if suspect_orders:
        for (order_id, trk), shipments in sorted(incoming_tracking_shipments.items()):
            if len(shipments) > 1:
                suspect_tracking.append({
                    "order_id": order_id,
                    "tracking_number": trk,
                    "shipments": sorted(str(s) for s in shipments),
                })

    updates = 0
    appends: list[list] = []
    money_free_rows: dict[str, list[int]] = {}  # status -> updated rows whose money must be cleared
    claimed_rows: set[int] = set()
    written_rows: list[int] = []  # every row touched this sync -> gets its Total Profit formula
    split_events: list[dict] = []
    relabel_events: list[dict] = []
    skipped_blank = 0
    # Every order this sync touched, and the raw order-level figures (shipping / gift card / sales
    # tax) it sent for that order (every row of one order carries the same number) — fed to
    # _reprorate_order_level after the write loop, once every row's final position is settled.
    touched_order_ids: set[str] = set()
    raw_totals_by_order: dict[str, dict[str, float]] = {f: {} for f in _ORDER_LEVEL_FIELDS}
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
        touched_order_ids.add(record["order_id"])
        for order_level_field in _ORDER_LEVEL_FIELDS:
            if record["order_id"] not in raw_totals_by_order[order_level_field]:
                raw_value = _parse_display_number(record.get(order_level_field, ""))
                if raw_value is not None:
                    raw_totals_by_order[order_level_field][record["order_id"]] = raw_value
        # Driven off FIELDNAMES (the same list csv_writer writes) rather than a second literal
        # copy, so a new column can't land in one place and not the other. .get() tolerates
        # re-syncing an older CSV written before a column was added — the missing value arrives
        # blank, which _merge_row then refuses to write over existing data.
        sheet_row = [_coerce(field, record.get(field, "")) for field in FIELDNAMES]
        # A mis-read order keeps whatever tracking the ledger already holds: blanking the incoming
        # value hands the decision to _merge_row's blank-never-overwrites rule, which is exactly the
        # right default here. Every other field still updates — only the tracking is in doubt, and a
        # run that also refused the corrected costs would throw away good data with the bad.
        suspect_tracking_row = record["order_id"] in suspect_orders
        if suspect_tracking_row:
            sheet_row[tracking_field_idx] = ""
        key = (
            record["order_id"],
            record["order_date"],
            record["item_name"],
            record.get("shipment", ""),
        )
        # The incoming package id, and the one test every match below must pass: a row whose OWN
        # non-blank package id differs from the incoming one is a different physical package, however
        # well its key or Shipment number lines up. Blank on either side is compatible with anything,
        # so nothing changes for rows written before the column existed.
        incoming_pid = str(record.get("package_id", "")).strip()

        def same_package(candidate: tuple[int, list]) -> bool:
            existing_pid = _package_id_of(candidate[1], package_hdr_idx)
            return not (existing_pid and incoming_pid and existing_pid != incoming_pid)

        # The exact key hit a row belonging to ANOTHER package. That means one of two things, and
        # only the batch can tell them apart:
        #  - the page RE-ORDERED its cards (the row's own package is still on the page — some record
        #    of this batch carries its id): this ordinal now names a different box, so resolve the
        #    record by identity below and, if that box has no row yet, append it under a FREE
        #    Shipment number rather than the occupied one;
        #  - the row's package has VANISHED from the page (no record carries its id): Amazon re-issued
        #    the label AND minted a new shipmentId (the one witnessed re-label, 2026-08-22, did
        #    exactly that). Treat it as the same row — the changed-tracking branch below then does what
        #    it always did (qty-1: re-label; otherwise a '*' split row with NO cost) — because
        #    appending the "new" package here would book the order's full cost a second time, which
        #    is history 1f all over again.
        hit = key_to_existing.get(key)
        hit_pid = _package_id_of(hit[1], package_hdr_idx) if hit is not None else ""
        displaced = (
            hit is not None and not same_package(hit)
            and hit_pid in incoming_pids_by_order.get(record["order_id"], set())
            # A package id that TWO of the order's rows carry is provisional, not an identity.
            # Pre-ship, Amazon renders a not-yet-minted second package under the first one's
            # shipmentId. When the real
            # id appeared, the exact key still named the right row, but "its id is still on the
            # page" read as a re-ordered page and appended a THIRD full-cost row.
            and len(package_to_existing.get((record["order_id"], hit_pid), [])) == 1
        )
        if hit is not None and not displaced:
            row_number, existing_row = hit
        else:
            match: tuple[int, list] | None = None
            # DEFER (0) BY PACKAGE ID — the retailer's own per-package identity (2026-09-09, history
            # §1f). An incoming row uniquely sharing (Order ID, Package ID) with one existing row is
            # the same physical package wherever its card now sits: update it in place, keeping its
            # recorded item name and Shipment number. A multi-SKU carton puts several rows behind one
            # id, so when either side is not 1:1 the rows are told apart by ITEM NAME instead (still
            # keeping the recorded Shipment number); anything still ambiguous falls through.
            if package_hdr_idx is not None and incoming_pid:
                pkey = (record["order_id"], incoming_pid)
                pcandidates = [c for c in package_to_existing.get(pkey, []) if c[0] not in claimed_rows]
                if pcandidates:
                    if incoming_pkey_count.get(pkey, 0) == 1 and len(pcandidates) == 1:
                        match = pcandidates[0]
                        er = match[1]
                        if name_hdr_idx < len(er) and str(er[name_hdr_idx]).strip():
                            sheet_row[name_field_idx] = er[name_hdr_idx]  # keep recorded name
                        if shipment_hdr_idx < len(er) and str(er[shipment_hdr_idx]).strip():
                            sheet_row[shipment_field_idx] = er[shipment_hdr_idx]  # keep recorded shipment
                    else:
                        pname = (record["order_id"], incoming_pid, record["item_name"])
                        named = [c for c in pcandidates
                                 if name_hdr_idx < len(c[1]) and c[1][name_hdr_idx] == record["item_name"]]
                        if incoming_pname_count.get(pname, 0) == 1 and len(named) == 1:
                            match = named[0]
                            er = match[1]
                            if shipment_hdr_idx < len(er) and str(er[shipment_hdr_idx]).strip():
                                sheet_row[shipment_field_idx] = er[shipment_hdr_idx]
            # DEFER (1) BY TRACKING NUMBER — the strongest cross-path identity after the package id.
            # The carrier tracking number is read identically by the API and the agent even when
            # their synthetic Shipment numbers diverge (Costco: the API numbers shipments by tracking
            # sort, the agent top-to-bottom, so they can be swapped). An incoming row uniquely sharing
            # (Order ID, Tracking Number) with one existing row is the same physical line: update it in
            # place, keeping BOTH its recorded item name and Shipment number so the paths converge on
            # one row. Tried before the shipment-line rule below so a swapped Shipment number can't
            # mis-merge onto the wrong box. Only the unambiguous 1:1 case (one incoming, one existing
            # for that tracking number) — a box holding two distinct SKUs is left to append.
            if match is None and tracking_hdr_idx is not None:
                trk = str(record.get("tracking_number", "")).strip()
                tkey = (record["order_id"], trk)
                tcandidates = [c for c in tracking_to_existing.get(tkey, [])
                               if c[0] not in claimed_rows and same_package(c)]
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
                candidates = [c for c in shipment_to_existing.get(skey, [])
                              if c[0] not in claimed_rows and same_package(c)]
                if incoming_skey_count.get(skey, 0) == 1 and len(candidates) == 1:
                    match = candidates[0]
                    if name_hdr_idx < len(match[1]) and str(match[1][name_hdr_idx]).strip():
                        sheet_row[name_field_idx] = match[1][name_hdr_idx]  # keep recorded name
            # DEFER (3) TO A PRE-SHIP PLACEHOLDER. two
            # `ordered` cards with neither a tracking number nor a shipmentId MERGED into one package
            # at ship time, so the second item's record arrived under a new Shipment number and
            # nothing above could match it — it appended, and the old row stayed behind as a
            # permanent $19.95 orphan (history 1f's double-count by another road). A row that is
            # still `ordered` with no tracking and no package id is a placeholder whose ordinal the
            # retailer may still change; the same item's record re-homes onto it and TAKES the
            # incoming Shipment number. Only when the placeholder's own key is not in this batch (a
            # page still showing that card keeps it), and only for a lone candidate.
            if match is None and placeholder_to_existing:
                candidates = [
                    c for c in placeholder_to_existing.get(key[:3], [])
                    if c[0] not in claimed_rows
                    and tuple(c[1][i] if i < len(c[1]) else "" for i in key_idx) not in incoming_keys
                ]
                if len(candidates) == 1:
                    match = candidates[0]
                    log.info(
                        "Order %s / %r: pre-ship placeholder at shipment %s re-homed as shipment %s "
                        "(the cards merged or re-ordered before shipping).",
                        record["order_id"], record.get("item_name", ""),
                        match[1][shipment_hdr_idx] if shipment_hdr_idx < len(match[1]) else "",
                        record.get("shipment", ""),
                    )
            # DEFER (4) BY THE RETAILER'S ITEM NUMBER. the
            # order was cancelled, and Costco's API renamed every line from its marketing name to the
            # terse warehouse name — a different Item Name is a different key, so the cancelled
            # records matched nothing, appended as new rows, and the `ordered` rows stayed open
            # (which is also what kept the order being re-read and re-appended after the user
            # deleted the duplicates). The "(Item #N)" suffix the Costco mapping writes is the
            # stable identity: same order, date, shipment and item number is the same line. The
            # recorded name is kept, exactly as the tracking rule keeps it. Unambiguous 1:1 only.
            if match is None:
                item_number = _item_number_of(record["item_name"])
                if item_number:
                    ikey = (record["order_id"], record["order_date"], record.get("shipment", ""), item_number)
                    candidates = [c for c in itemno_to_existing.get(ikey, [])
                                  if c[0] not in claimed_rows and same_package(c)]
                    if incoming_itemno_count.get(ikey, 0) == 1 and len(candidates) == 1:
                        match = candidates[0]
                        if name_hdr_idx < len(match[1]) and str(match[1][name_hdr_idx]).strip():
                            sheet_row[name_field_idx] = match[1][name_hdr_idx]  # keep recorded name
            if match is None:
                if displaced:
                    label = shipment_label(_next_shipment_number(
                        record["order_id"], existing, oid_idx, shipment_hdr_idx,
                        appends, oid_field_idx, shipment_field_idx,
                    ))
                    log.warning(
                        "Order %s / %r: shipment %s is already the row of package %s (still on the "
                        "page), and package %s has no row of its own — appending it as shipment %s.",
                        record["order_id"], record.get("item_name", ""), record.get("shipment", ""),
                        _package_id_of(hit[1], package_hdr_idx), incoming_pid, label,
                    )
                    sheet_row[shipment_field_idx] = _coerce("shipment", label)
                appends.append(_blank_money_for_status(sheet_row))
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
        if tracking_hdr_idx is not None and not suspect_tracking_row:
            existing_trk = existing_row[tracking_hdr_idx].strip() if tracking_hdr_idx < len(existing_row) else ""
            incoming_trk = str(record.get("tracking_number", "")).strip()
            if existing_trk and incoming_trk and existing_trk != incoming_trk:
                owners = [c for c in tracking_to_existing.get((record["order_id"], incoming_trk), [])
                          if c[0] not in claimed_rows and same_package(c)]
                if len(owners) == 1:
                    # The new number already has a home row → update that one (keeping its identity).
                    row_number, existing_row = owners[0]
                    if name_hdr_idx < len(existing_row) and str(existing_row[name_hdr_idx]).strip():
                        sheet_row[name_field_idx] = existing_row[name_hdr_idx]
                    if shipment_hdr_idx < len(existing_row) and str(existing_row[shipment_hdr_idx]).strip():
                        sheet_row[shipment_field_idx] = existing_row[shipment_hdr_idx]
                else:
                    label = shipment_label(_next_shipment_number(
                        record["order_id"], existing, oid_idx, shipment_hdr_idx,
                        appends, oid_field_idx, shipment_field_idx,
                    ))
                    existing_qty = (_parse_display_number(existing_row[qty_field_idx])
                                    if qty_field_idx < len(existing_row) else None)
                    incoming_qty = _parse_display_number(str(record.get("quantity", "")))
                    if existing_qty == 1 and incoming_qty == 1:
                        # A RE-LABEL, not a split. One unit is one box,
                        # so a changed number on a Quantity-1 row can only be the carrier re-issuing
                        # the label — a split and a re-label look identical by number alone, and this
                        # is the one case the quantity settles. The LIVE row takes the new number and
                        # keeps its cost (it falls through to the update below); the DEAD number is
                        # kept as a `superseded` row — money blank, numbered after the live boxes —
                        # because it really was posted to the buying group. The live row's Tracking
                        # Submitted tick is cleared: the new number has not been posted yet.
                        retired = _preserve_from_stored(existing_row, raw_grid, row_number)
                        retired = [_coerce(field, val) for field, val in zip(FIELDNAMES, retired)]
                        retired[_STATUS_FIELD_IDX] = "superseded"
                        retired[shipment_field_idx] = _coerce("shipment", label)
                        retired[tracking_field_idx] = existing_trk
                        appends.append(_blank_money_for_status(retired))
                        sheet_row[submitted_field_idx] = False
                        relabel_events.append({
                            "order_id": record["order_id"],
                            "item_name": (existing_row[name_hdr_idx] if name_hdr_idx < len(existing_row)
                                          else record.get("item_name", "")),
                            "old_tracking": existing_trk,
                            "new_tracking": incoming_trk,
                            "retired_shipment": label,
                        })
                    else:
                        split_row = list(sheet_row)
                        split_row[shipment_field_idx] = _coerce("shipment", label)
                        split_row[qty_field_idx] = "*"      # unknown per-box split — user fills it in
                        split_row[total_field_idx] = ""     # can't compute Total Cost without a quantity
                        appends.append(_blank_money_for_status(split_row))
                        split_events.append({
                            "order_id": record["order_id"],
                            "item_name": (existing_row[name_hdr_idx] if name_hdr_idx < len(existing_row)
                                          else record.get("item_name", "")),
                            "existing_tracking": existing_trk,
                            "new_tracking": incoming_trk,
                            "new_shipment": label,
                        })
                        continue  # leave the existing box's row untouched

        merged = _merge_row(_preserve_from_stored(existing_row, raw_grid, row_number), sheet_row,
                            protected=protected.get(_row_key(existing_row), ()))
        # Preserved cells come back as strings from get_all_values(); re-coerce so a kept numeric
        # (e.g. a quantity carried over from a prior run) is written as a number, not text —
        # otherwise the ledger stores it as text.
        merged = [_coerce(field, val) for field, val in zip(FIELDNAMES, merged)]
        merged = _blank_money_for_status(merged)
        merged_status = str(merged[_STATUS_FIELD_IDX] or "").strip().lower()
        if merged_status in MONEY_FREE_STATUSES:
            money_free_rows.setdefault(merged_status, []).append(row_number)
        worksheet.update(range_name=f"A{row_number}", values=[_blank_to_none(merged)])
        claimed_rows.add(row_number)
        written_rows.append(row_number)
        updates += 1

    appends = _inherit_hand_edited_card_fields(worksheet, appends, existing, protected)
    if appends:
        # Write at an explicit column-A range rather than worksheet.append_rows(): an append that
        # auto-detects the "table" to append after once anchored to the wrong column (observed
        # shifting rows 10 columns right into K:AB). Positioning from column A
        # of the first empty row keeps every row aligned to the header. `existing` was read before any
        # updates and updates never add rows, so len(existing)+1 is the first free row.
        # NOT len(existing): a checkbox column materialises a real False in every empty row it covers,
        # which made len(existing) the GRID height rather than the data height — see _last_occupied_row.
        start_row = _last_occupied_row(existing) + 1
        _ensure_grid_rows(worksheet, start_row + len(appends) - 1)
        worksheet.update(range_name=f"A{start_row}", values=[_blank_to_none(r) for r in appends])
        written_rows.extend(range(start_row, start_row + len(appends)))

    _reprorate_order_level(worksheet, touched_order_ids, raw_totals_by_order)
    for money_free_status, rows_to_clear in money_free_rows.items():
        _clear_money_for_status(worksheet, money_free_status, rows_to_clear)
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

    if relabel_events:
        lines = [
            f"- Order {e['order_id']} / {e['item_name']}: {e['old_tracking']} -> {e['new_tracking']}; "
            f"the dead number is kept as shipment {e['retired_shipment']} (superseded, no money)"
            for e in relabel_events
        ]
        from alerts.notifier import alert

        alert(
            f"Re-labelled package on {len(relabel_events)} row(s) — new tracking number recorded",
            "A single-unit shipment's tracking number changed. One unit cannot split, so the carrier "
            "re-issued the label. The row now carries the NEW number (the buying-group sync submits it "
            "on its next run) and the old number is kept as a `superseded` row that carries no money. "
            "Nothing to set by hand — but if the buying group already holds the old number, tell them "
            "the new one:\n\n" + "\n".join(lines),
        )

    if suspect_tracking:
        lines = [
            f"- Order {e['order_id']}: tracking {e['tracking_number']} was reported for "
            f"shipments {', '.join(e['shipments'])}"
            for e in suspect_tracking
        ]
        from alerts.notifier import alert

        alert(
            f"Repeated tracking number on {len(suspect_tracking)} order(s) — tracking not updated",
            "One tracking number was reported for two different boxes of the same order, which is "
            "impossible: a carrier issues one number per package. That means the scrape MIS-READ the "
            "tracking (the Costco agent fallback has done this, repeating box 1's number for box 2 "
            "and dropping box 2's).\n\n"
            "The tracking numbers already on the ledger were LEFT ALONE for these orders, and no rows "
            "were added. Everything else on those rows — cost, status, delivery date — did update.\n\n"
            "Nothing to do if the ledger's numbers are right. If they aren't, re-run the retailer on "
            "its API path (not the agent) and it will correct them:\n\n" + "\n".join(lines),
        )

    log.info(
        "Ledger sync: %d row(s) updated, %d row(s) appended%s%s%s.",
        updates,
        len(appends),
        f", {len(split_events)} split-box row(s) added" if split_events else "",
        f", {len(suspect_tracking)} repeated tracking number(s) ignored" if suspect_tracking else "",
        f", {skipped_blank} skipped (blank Order ID)" if skipped_blank else "",
    )
    # Returned so the caller can decide whether a re-sort is needed: only APPENDS move rows out of
    # order (an update rewrites a row in place). Previously returned None; adding a return value is
    # additive, so callers that ignore it are unaffected.
    return {
        "updated": updates,
        "appended": len(appends),
        "split_rows": len(split_events),
        "suspect_tracking": suspect_tracking,
        "skipped_blank": skipped_blank,
        "skipped_conflicts": len(conflicts),
    }


# Sort order for the ledger: newest orders at the top, and an order's rows kept together beneath it.
# Order ID + Shipment are tie-breakers, not preferences — without them a multi-shipment order's rows
# can be scattered among other orders placed the same day. the worksheet contract spells descending "des" (not
# "desc"); the adapter's sort reads it the same way.
_SORT_SPEC = (("Order Date", "des"), ("Order ID", "asc"), ("Shipment", "asc"))


def sort_ledger_by_date_desc(worksheet=None) -> dict:
    """Sort the ledger newest-first, then re-stamp every Total Profit formula.

    Sorting is done as a SEPARATE step after sync_csv_to_ledger rather than inside it, and that
    ordering is load-bearing: sync caches each matched row's NUMBER from its pre-sync snapshot
    (key_to_existing and friends) and writes updates to `A{row_number}`. Moving rows while those
    numbers are in flight would write every update onto the wrong row — silently, since nothing
    downstream re-reads to check. So rows only ever move once sync has finished writing.

    This is also why appends still go to the BOTTOM (see sync_csv_to_ledger): a sort is a total
    ordering, so the insert position can't affect the final result, and appending leaves the
    "updates never add rows" invariant that the cached row numbers depend on completely intact.

    RE-STAMPING EVERY DATA ROW is required, not optional. _profit_formula emits same-row relative
    references (Payout/Total Cost/Shipping/Cashback Rate/Insurance all `{col}{n}`), so a row that
    moves needs the formula for its NEW position. scripts/audit_ledger.py's check_profit_formula_literal
    fails any row whose stored formula isn't exactly _profit_formula(row_number), which is the tripwire
    for getting this wrong.

    Returns {"sorted_rows": int, "already_sorted": bool}. Fails soft on the formula re-stamp only
    (_write_profit_formulas logs rather than raises); a sort failure itself propagates.
    """
    worksheet = worksheet or _get_worksheet()
    existing = worksheet.get_all_values()
    if not existing or not any(str(cell).strip() for cell in existing[0]):
        log.info("Ledger sort: the ledger is empty, nothing to sort.")
        return {"sorted_rows": 0, "already_sorted": True}

    header = [str(c) for c in existing[0]]
    # Same exact-order requirement as sync_csv_to_ledger: sorting addresses columns by position, so a
    # grid whose columns are in a different order would be sorted on the wrong ones.
    if header != list(HEADER):
        raise RuntimeError(
            f"Worksheet '{getattr(worksheet, "title", "ledger")}' has the ledger's columns in a "
            "different ORDER than the current schema, so sorting would target the wrong columns. "
            "Nothing was sorted. The ledger file migrates its own columns on open (ledger_db/store.py), "
            "so this should not happen."
        )

    oid_idx = header.index("Order ID")

    def ledger_row_numbers(grid) -> list[int]:
        """1-based row numbers of the rows the ledger owns. Same rule as sync_csv_to_ledger: a row
        with no Order ID isn't one (it can never be matched or updated)."""
        return [
            n for n, r in enumerate(grid[1:], start=2)
            if oid_idx < len(r) and str(r[oid_idx]).strip()
        ]

    row_numbers = ledger_row_numbers(existing)
    if len(row_numbers) < 2:
        log.info("Ledger sort: %d data row(s), nothing to reorder.", len(row_numbers))
        return {"sorted_rows": len(row_numbers), "already_sorted": True}

    # The range ends at the POSITION of the last ledger row, not at the ledger row COUNT. Those agree
    # only when every row in the block carries an Order ID, and a hand-added row doesn't have to:
    # a spacer, a note, a half-typed row all count as neither. Each one made the old count-derived
    # bound fall a row short, leaving that many rows off the bottom of the range — excluded from this
    # sort and every future one, silently, since being out of order is only a WARN in audit_ledger and
    # drift between appends is expected anyway. Locating the last row also keeps a note BELOW the
    # block outside the range, which simply using len(existing) would sweep into the middle of it.
    last_row = row_numbers[-1]
    # An EXPLICIT range matters for the same family of reasons: an unranged sort spans the
    # grid's full row_count, which drags the trailing empty rows through the data block and would
    # leave blank rows interleaved (which audit_ledger's check_content_outside_the_schema then flags).
    cell_range = f"A2:{_col_letter(len(HEADER) - 1)}{last_row}"
    specs = tuple((header.index(name) + 1, direction) for name, direction in _SORT_SPEC)

    worksheet.sort(*specs, range=cell_range)
    # Re-read instead of reusing row_numbers: the sort just moved the rows those numbers described.
    # Non-ledger rows inside the block move too, and not always to the bottom — the sort orders EMPTY
    # cells last, but a row blank only in Order ID still sorts on its Order Date and can land
    # mid-block. So which rows are ledger rows now is a fact about the ledger AFTER the sort, and
    # stamping a position-bound formula anywhere else is exactly what audit_ledger's
    # check_no_stray_formulas fails on. One extra read buys correctness in every arrangement.
    _write_profit_formulas(worksheet, ledger_row_numbers(worksheet.get_all_values()))
    log.info("Ledger sort: %d row(s) sorted newest-first over %s.", len(row_numbers), cell_range)
    return {"sorted_rows": len(row_numbers), "already_sorted": False}


# The money AMOUNTS a cancelled order must not carry. Blanked rather than zeroed so they read as
# "no such number" instead of "a real zero", and so a stray SUM over the raw column can't pick them up.
#
# cashback_rate and card_name are deliberately NOT here: they describe the CARD, not an amount, and
# nothing sums them — while blanking the rate would trip audit_ledger's card/rate coverage check on
# every cancelled row for no gain. COGS and Total Profit aren't here either; they're formulas that
# blank themselves on a cancelled row (see _cogs_formula).
_CANCELLED_BLANK_FIELDS = (
    "cost_per_item", "total_cost", "shipping", "insurance", "payout_amount", "payout_date",
    "gift_card", "sales_tax", "rewards_used", "expected_payout",
)
# A SUPERSEDED row blanks Quantity as well: it is the multiplier that booked the
# re-labelled package's cost twice, and "how many were ordered" is already told by the live row.
_SUPERSEDED_BLANK_FIELDS = ("quantity",) + _CANCELLED_BLANK_FIELDS
_BLANK_FIELDS_BY_STATUS = {
    "cancelled": _CANCELLED_BLANK_FIELDS,
    "superseded": _SUPERSEDED_BLANK_FIELDS,
}


def _blank_money_for_status(row: list) -> list:
    """Empty the money cells on a CANCELLED or SUPERSEDED row, keeping the row for bookkeeping.

    A cancelled order was refunded, so no money ever moved: leaving the scraped cost on the row makes
    it look like a real purchase to anything that sums the column, and at year end that is an
    overstated cost of goods. The row still says what was ordered, from whom, and that it was
    cancelled — which is the bookkeeping part worth keeping. A superseded row is
    the same shape for a different reason: its package was re-labelled and lives on under the live
    row, so any money here would be counted twice (see _BLANK_FIELDS_BY_STATUS for the one
    difference between the two field sets).

    Applied at WRITE time rather than by a one-off cleanup,
    because a cleanup only fixes the rows that exist when it runs. Both statuses are terminal, so a
    row blanked here is never re-scraped and never re-populated.

    Returns a new list; the input is not mutated.
    """
    try:
        status = str(row[FIELDNAMES.index("status")] or "").strip().lower()
    except (IndexError, ValueError):
        return list(row)
    fields = _BLANK_FIELDS_BY_STATUS.get(status)
    if not fields:
        return list(row)
    out = list(row)
    for field in fields:
        i = FIELDNAMES.index(field)
        if i < len(out):
            out[i] = ""
    return out


def _clear_cells(worksheet, row_numbers: list[int], fields: tuple[str, ...]) -> None:
    """Actually EMPTY the given cells on the given rows. RAISES on failure — the callers decide.

    Uses USER_ENTERED with "", which _blank_to_none's own measurements record as the one combination
    that clears the VALUE while preserving the cell's number format (RAW "" strips the format; RAW
    None doesn't write at all). Same reason _write_profit_formulas is a separate USER_ENTERED batch.
    """
    if not row_numbers:
        return
    data = [
        {"range": f"{_COL[field]}{n}", "values": [[""]]}
        for n in sorted(set(row_numbers))
        for field in fields
    ]
    worksheet.batch_update(data, value_input_option="USER_ENTERED")


def _clear_money_for_status(worksheet, status: str, row_numbers: list[int]) -> None:
    """Actually EMPTY the money cells on rows that just became cancelled / superseded.

    _blank_money_for_status puts "" in the row, which is enough for an APPEND (the cell was never
    populated) but not for an UPDATE: _blank_to_none turns "" into None on the way out, and None means
    "leave this cell alone", not "clear it". So a row that carried a real cost before it was cancelled
    would keep that cost forever — and both statuses are terminal, so nothing would ever come back.

    Fails soft: the row data is already written, and the next sync re-clears.
    """
    fields = _BLANK_FIELDS_BY_STATUS.get(status)
    if not row_numbers or not fields:
        return
    try:
        _clear_cells(worksheet, row_numbers, fields)
        log.info("Cleared the money cells on %d %s row(s).", len(set(row_numbers)), status)
    except Exception:
        log.exception(
            "Could not clear the money cells on %d %s row(s); they may still show a cost until the "
            "next sync.", len(set(row_numbers)), status,
        )


def _write_profit_formulas(worksheet, row_numbers: list[int]) -> None:
    """(Re)write BOTH derived formula columns — COGS and Total Profit — into every row this sync
    touched, in one batched API call.

    The name is historical: it predates COGS, and every caller means "restore this row's derived
    formulas", so it was kept rather than churned across six call sites. Both columns are stamped
    together because they have identical needs — same reason for existing, same failure mode, and
    Total Profit READS the COGS cell, so stamping one without the other would leave a live formula
    pointing at a frozen number.

    Why a SEPARATE write instead of putting the formula in the main row block: the row block is sent
    RAW so the ledger stores values exactly as scraped, which a formula string would land as literal
    text. This call is the only one using USER_ENTERED, and it's scoped to a single column — keeping
    USER_ENTERED away from the data columns, where it would reinterpret long numeric tracking numbers
    as numbers and render them in scientific notation.

    Rewriting on every touch is deliberate: get_all_values() returns a formula cell's EVALUATED text,
    so _merge_row carries that number forward and the RAW row write would replace the formula with a
    frozen value. Re-stamping the formula last restores it.

    Note this does NOT need to run again after _reprorate_order_level changes a sibling row's Shipping
    value: _profit_formula reads that cell by reference (same-row, no SUMIF), so the adapter recomputes
    Total Profit live the moment Shipping changes — no re-stamp required for rows this call doesn't
    otherwise touch.

    A failure here is logged, not raised: the scraped data is already safely written, and the next
    sync re-stamps the formula.
    """
    if not row_numbers:
        return
    profit_col, cogs_col = _COL["total_profit"], _COL["cogs"]
    data = []
    for n in sorted(set(row_numbers)):
        data.append({"range": f"{cogs_col}{n}", "values": [[_cogs_formula(n)]]})
        data.append({"range": f"{profit_col}{n}", "values": [[_profit_formula(n)]]})
    try:
        worksheet.batch_update(data, value_input_option="USER_ENTERED")
    except Exception:
        log.exception(
            "Could not write the COGS/Total Profit formulas into %d cell(s); the row data itself was "
            "written and the next sync will restore the formula.", len(data),
        )


# The ORDER-LEVEL amounts every mapping emits identically on all of an order's rows, in the field
# order they were added. Each one's cell holds that row's cost-weighted SHARE of the order
# total, written by _reprorate_order_level below.
_ORDER_LEVEL_FIELDS = ("shipping", "gift_card", "sales_tax", "rewards_used")


def _reprorate_order_level(worksheet, order_ids: set, raw_totals: dict) -> None:
    """Rewrite the Shipping / Gift Card / Sales Tax / Rewards Used cell of EVERY row belonging to `order_ids` to
    that row's cost-weighted SHARE of the order-level total (weighted by Total Cost), replacing the raw order-level value every scraper/agent emits.
    `raw_totals` is {field: {order_id: total}} over _ORDER_LEVEL_FIELDS.

    Why this is Python at sync time, not a derived column: the only place the true order-level
    total is ever known is the freshly-scraped record itself — every row of an order carries the SAME
    number (see OrderItem.shipping). Once this function overwrites a row's cell with its share, that
    raw total is gone from the ledger; a live formula would need it to live SOMEWHERE else to divide
    from (that's what a short-lived separate "Prorated Shipping" column existed for, added then
    reverted the same day — the user wants the split to just BE the column, not a second one). So
    it's computed once, here, from the total this sync just read off the CSV, and applied to EVERY
    row of the order currently on the ledger — not only the rows this sync happened to touch — so a
    shipment discovered LATER (the order grows a new box on a re-check) re-derives the whole order's
    split fresh rather than leaving its older siblings stale.

    The formulas read all three columns directly (same-row references, no SUMIF), and the adapter
    recomputes them on every read — so COGS and Total Profit
    update for every affected sibling row with no extra re-stamp needed here.

    Runs AFTER every update/append this sync has already written, so the fresh read below sees final
    row positions (including any rows just appended) rather than a stale pre-write snapshot. Each
    FIELD is skipped independently for an order this sync sent no figure for (a partial re-check
    that only refreshes tracking sends none of them) — those cells are left exactly as a previous
    sync last prorated them, or blank if nothing ever reported one.

    A failure here is logged, not raised: the scraped row data is already safely written; only the
    splits may still show the raw order-level number until the next sync repairs it.
    """
    if not order_ids:
        return
    grid = worksheet.get_all_values()
    header = grid[0] if grid else []
    if header != list(HEADER):
        return  # sync_csv_to_ledger already refused to write in this case; nothing to reprorate
    oid_i = header.index("Order ID")
    cost_i = header.index("Total Cost")
    status_i = header.index("Status")

    rows_by_order: dict[str, list[int]] = {}
    for row_number, row in enumerate(grid[1:], start=2):
        oid = row[oid_i].strip() if oid_i < len(row) else ""
        if oid in order_ids:
            rows_by_order.setdefault(oid, []).append(row_number)

    protected = _protected_cells(worksheet)
    data = []
    for oid, row_numbers in rows_by_order.items():
        totals = {field: raw_totals.get(field, {}).get(oid) for field in _ORDER_LEVEL_FIELDS}
        if all(t is None for t in totals.values()):
            continue
        costs = {n: _parse_display_number(grid[n - 1][cost_i]) if cost_i < len(grid[n - 1]) else None
                 for n in row_numbers}
        cost_sum = sum(c for c in costs.values() if c)
        for n in row_numbers:
            # A CANCELLED or SUPERSEDED row carries no money (see _blank_money_for_status), and this
            # runs AFTER the row write — so without this it would put a freshly-computed 0.0 back into
            # a cell the write had just emptied, undoing the blanking every single sync.
            row_status = grid[n - 1][status_i] if status_i < len(grid[n - 1]) else ""
            if str(row_status).strip().lower() in MONEY_FREE_STATUSES:
                continue
            weight = (costs[n] or 0) / cost_sum if cost_sum else 0.0
            hand = protected.get(_row_key(grid[n - 1]), ()) if protected else ()
            for field, total in totals.items():
                if total is None or field in hand:
                    continue  # a share typed by hand stays (ledger_db/hand_edits)
                data.append({"range": f"{_COL[field]}{n}", "values": [[round(total * weight, 2)]]})

    if not data:
        return
    try:
        worksheet.batch_update(data, value_input_option="RAW")
    except Exception:
        log.exception(
            "Could not reprorate the order-level amounts for %d order(s); the raw order-level "
            "totals may still be sitting on some of their rows until the next sync.",
            len(rows_by_order),
        )


_STATUS_FIELD_IDX = FIELDNAMES.index("status")


def _last_occupied_row(existing: list[list], checkbox_index: int | None = None) -> int:
    """The last row that really holds something — ignoring a row whose ONLY content is an unticked
    checkbox.

    `Tracking Submitted` carries checkbox data validation. An EMPTY cell under a checkbox
    materialises as a real `False`, so `get_all_values()` reports such a row as non-empty and
    `len(existing)` counts every grid row that the checkbox range covers, not just the data.

    OBSERVED LIVE: the grid was 991 rows, the checkbox had materialised `False` down to
    row 991, so `len(existing) + 1` anchored the append at A992 and the whole Amazon Business sync
    died with `Range (Sheet1!A992) exceeds grid limits`. Only Amazon Business was affected because it
    was the only retailer APPENDING that run — an update writes to a row number it already knows.

    Deliberately narrow: a row is only skipped when its sole non-blank cell is a FALSE checkbox. A
    genuine note parked below the ledger still counts, so appends continue to land after it rather
    than overwriting it — which is the behaviour `len(existing)` was chosen for in the first place.
    """
    # `checkbox_index` exists for a MIGRATION WINDOW (the 2026-08-12 column reorder). Defaulting to
    # FIELDNAMES is right whenever the grid matches the current schema, but a grid still in an OLD
    # column order has FIELDNAMES pointing at the wrong column, so the
    # materialised FALSEs are not recognised as checkbox padding, and every grid row counts as
    # occupied. That made the reorder rewrite 983 rows on a 42-row ledger. Callers holding the ledger's
    # own header pass the index from THAT.
    checkbox = FIELDNAMES.index("tracking_submitted") if checkbox_index is None else checkbox_index
    for number in range(len(existing), 0, -1):
        row = existing[number - 1]
        for i, cell in enumerate(row):
            if not str(cell).strip():
                continue
            if i == checkbox and str(cell).strip().lower() in ("false", "unchecked"):
                continue  # an empty checkbox cell, not content
            return number
    return 0


def _ensure_grid_rows(worksheet, needed: int) -> None:
    """Grow the grid if an append would land past the last row that exists.

    The worksheet contract rejects a write beyond the grid outright, and the rows are lost for that
    run — so capacity is checked BEFORE writing rather than discovered by a failed sync. Extra headroom is
    added so this is not paid once per append as the ledger fills up.
    """
    have = worksheet.row_count
    if needed <= have:
        return
    grow_by = needed - have + 200
    log.info("Ledger: growing the grid by %d row(s) to fit an append at row %d.", grow_by, needed)
    worksheet.add_rows(grow_by)


def _blank_to_none(row: list) -> list:
    """Send empty cells as None rather than "" — otherwise the write STRIPS their number format.

    Measured against the worksheet contract (live, 2026-08-14), which the adapter keeps:

        RAW ""            -> number format cleared
        RAW None          -> number format preserved
        USER_ENTERED ""   -> number format preserved

    This is why `Insurance`, `Actual Payout` and `Total Profit` kept reverting to raw floats while
    `Total Cost` never did: the scrapers always emit those three blank, so every append rewrote them
    as "" and wiped the currency format off the new row, and `_write_profit_formulas` then stamped the
    formula into an unformatted cell. Total Cost always carries a number, so it was never stripped.
    Formatting the column does not help — the write actively clears it afterwards.

    RAW cannot be swapped for USER_ENTERED here: the data rows are RAW deliberately, so a long numeric
    tracking number is not reinterpreted into scientific notation.

    SAFETY, and it rests entirely on _merge_row: None means "leave this cell alone", NOT "clear it"
    (verified live — a seeded value survived a None write). That is only equivalent to "" because
    _merge_row never emits a blank over a non-blank cell, so a "" in this block always means the cell
    is ALREADY empty. Appends are trivially safe for the same reason: the row does not exist yet.
    If that invariant ever changes, this must change with it.
    """
    return [None if (v is None or (isinstance(v, str) and v.strip() == "")) else v for v in row]


def _read_unformatted(worksheet) -> list[list]:
    """The ledger's stored VALUES (real types), or [] if that read is unavailable."""
    from ledger_db.worksheet import ValueRenderOption

    try:
        return worksheet.get_values(value_render_option=ValueRenderOption.unformatted) or []
    except Exception:  # noqa: BLE001 -- never let a second read stop the sync
        log.warning("Could not read the ledger unformatted; preserved cells will use display text.",
                    exc_info=True)
        return []


def _preserve_from_stored(existing_row: list, raw_grid: list[list], row_number: int) -> list:
    """existing_row (formatted text) with its numeric / checkbox cells swapped for the STORED values.

    Only cells whose stored value is a real number or bool are swapped -- a number stored as text
    stays text (numeric_columns_are_numeric owns that), and every non-numeric column keeps its
    formatted text. A missing or short raw row (the read failed, or the API trimmed trailing
    blanks) leaves the formatted cell in place, so this can only ever add fidelity, never remove it.
    """
    if not raw_grid or row_number - 1 >= len(raw_grid):
        return existing_row
    raw_row = raw_grid[row_number - 1]
    out = list(existing_row)
    for i, field in enumerate(FIELDNAMES):
        if i >= len(raw_row) or i >= len(out):
            break
        raw = raw_row[i]
        if field in _BOOL_FIELDS and isinstance(raw, bool):
            out[i] = raw
        elif field in _NUMERIC_FIELDS and isinstance(raw, (int, float)) and not isinstance(raw, bool):
            out[i] = raw
    return out


def _merge_row(existing_row: list, new_row: list, protected=()) -> list:
    """Overlay new_row onto existing_row, but never overwrite an existing non-empty cell with a
    blank -- and never overwrite a PROTECTED field at all (`protected`: the field names the user
    typed by hand on the dashboard; ledger_db/hand_edits). This makes partial refreshes safe: a tracking-only re-check leaves the static columns
    (item name, cost, address, ...) blank, and those blanks must not wipe already-captured data —
    while real new values (status, tracking, delivery date, last scraped at) still update.

    STATUS IS THE ONE FIELD THAT ALSO ONLY MOVES FORWARD. A row's lifecycle is monotonic — nothing a
    retailer can report legitimately walks it back — so a scrape claiming an earlier status is a
    mis-read, not news.

    OBSERVED LIVE: a forced agent run couldn't see the second box's tracking number,
    concluded the shipment hadn't shipped, and wrote `shipped` -> `ordered` over a row that had
    already been delivered. `_collapse_records` has always applied this rule when merging two
    INCOMING records against each other; it was never applied against what the ledger already holds,
    which is where it matters more — the ledger is the accumulated truth of every prior run.

    It also protects the hand-typed values (section 12b): `return` and `paid` outrank everything a
    scraper reports, so a MOD return typed in by hand survives a scrape that still sees `delivered`.
    That is the same guarantee sync_tracking.allocate_payouts documents for the payout path, which
    until now the far more frequent scraper path did not have.
    """
    merged = []
    protected = set(protected or ())
    for i, new_val in enumerate(new_row):
        old_val = existing_row[i] if i < len(existing_row) else ""
        if protected and i < len(FIELDNAMES) and FIELDNAMES[i] in protected:
            merged.append(old_val)  # typed by hand: the run does not get a say
        elif str(new_val).strip() == "" and str(old_val).strip() != "":
            merged.append(old_val)
        elif i == _STATUS_FIELD_IDX and _rank_of(new_val) < _rank_of(old_val):
            merged.append(old_val)
        else:
            merged.append(new_val)
    return merged


def _rank_of(status) -> int:
    """How far through the lifecycle a status is; unknown/blank ranks lowest (-1).

    Blank ranking below `ordered` is what keeps the guard from firing on a row the ledger has no
    status for yet — there is nothing to move backwards from.
    """
    return _STATUS_RANK.get(str(status or "").strip().lower(), -1)


def _rollup_status(statuses: list[str]) -> str:
    """Collapse several shipment statuses into one for display.

    A uniformly TERMINAL shipment reports that status back unchanged — all "cancelled" → "cancelled"
    (cancellation is order-level in practice), all "paid" → "paid". That is load-bearing, not cosmetic:
    load_order_state decides an order is finished from this rolled-up value, so a terminal status that
    fell through to "ordered" below would re-open the order and put it back in the re-check list on
    every run — the precise failure that marking a status terminal exists to prevent. It matters for
    the hand-entered "paid"/"return" especially, since no scraper will ever correct them.

    A MIX of terminal states reports "delivered": every box is finished, and delivered is the state
    they all passed through on the way. Otherwise shipped if anything has shipped, else ordered.
    """
    if statuses and all(s in TERMINAL_STATUSES for s in statuses):
        return statuses[0] if len(set(statuses)) == 1 else "delivered"
    if any(s in ("shipped", "delivered") for s in statuses):
        return "shipped"
    return "ordered"


def plan_buying_group_retag(header: list[str], data_rows: list[list[str]], warehouses) -> dict:
    """Read-only: work out what a retroactive Buying Group classification pass would do to rows
    ALREADY on the ledger, without writing anything. `apply_buying_group_retag` (or a caller script)
    turns this plan into real writes/deletes.

    This exists because the classifier only tags NEW/re-checked rows at scrape time (main.run_scrape) —
    rows recorded before the Buying Group column existed, or before warehouses.json had an entry that
    now matches them, are never revisited automatically. This is the one-off backfill.

    `header` is the ledger's CURRENT header row (may predate the Buying Group column — that's reported
    via `needs_header_migration`, not assumed). `data_rows` is `existing[1:]` (no header). Rows with a
    blank Order ID are skipped, same rule as sync_csv_to_ledger.

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
        # A DELIBERATELY UNROUTED tag is sticky. A gift card row is hand-entered bookkeeping, and
        # classify_address knows nothing about it: shipped to the user's own address it would classify
        # Personal and be DELETED, and shipped to a jig it would be retagged into a buying group it
        # was never part of. Neither is recoverable from the ledger afterwards, so the tag wins.
        if is_deliberately_unrouted(old_tag):
            group_counts[old_tag] = group_counts.get(old_tag, 0) + 1
            unchanged += 1
            continue

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
    """Read the ledger and return, for this profile:

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

    Each row IS one (shipment x item), so shipments are recovered by grouping an order's
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

    Fails soft (empty state) if the ledger isn't configured/readable → treat all as new.
    """
    empty: dict = {"delivered_ids": [], "cancelled_ids": [], "open_orders": []}
    try:
        worksheet = _get_worksheet()
        existing = worksheet.get_all_values()
    except Exception:
        # NOT "treating all orders as new" — that wording reads as conservative OVER-fetching, and the
        # real effect is the opposite. The fetch set is (new-in-window + still-open re-checks), and the
        # still-open half comes from THIS read; with it empty, FEWER orders are fetched, not more.
        # Live 10:00Z a transient 503 here took Amazon from its usual "fetching 1" to
        # "discovered 10 -> fetching 0 -> built 0 rows", so a tracking-number update on the open order
        # would have been missed for that cycle. Compare Costco in the same run, reading state fine:
        # "0 discovered + 1 open -> 1 order(s) to fetch".
        who = " / ".join(x for x in (profile_label, retailer) if x) or "all profiles"
        log.warning(
            "Could not read order state from the ledger (%s); OPEN-ORDER RE-CHECKS ARE SKIPPED this "
            "run — only brand-new orders in the date window will be fetched.", who, exc_info=True,
        )
        # Alerted because this degrades collection SILENTLY. A logged-out session shouts; this used to
        # log a warning and carry on looking like a normal run, which is the failure mode CLAUDE.md
        # ranks worst ("silently records nothing"). The run still continues — that part is correct.
        alert(
            f"Ledger: order state unreadable ({who}) — re-checks skipped this run",
            "The scrape could not read the ledger, so it did not re-check any already-recorded open "
            "order; it only looked for brand-new orders in the lookback window. Any status or "
            "tracking-number change on an open order was missed for this cycle and will be picked up "
            "on the next successful run. Check logs/run.log.",
        )
        return empty

    return classify_order_state(existing, profile_label, since, retailer)


def classify_order_state(existing: list[list], profile_label: str | None = None,
                         since: str | None = None, retailer: str | None = None) -> dict:
    """The PURE half of load_order_state: ledger grid (header row first) -> order state.

    Split out so it can be asked questions offline -- by tests, and by scripts/audit_ledger's
    `state_visibility` check, which runs it for every configured profile x retailer and reports
    what each run would see and, more importantly, which rows NO run can see. The Profile-strip
    bug lived here for weeks precisely because this loop only ever ran inside a
    live call and said nothing about what it excluded. Same semantics as before, moved verbatim.
    """
    empty: dict = {"delivered_ids": [], "cancelled_ids": [], "open_orders": []}
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
    # Optional: ledgers written before the Shipment column exist. Those rows group under "",
    # which behaves like any other single shipment.
    shipment_idx = header.index("Shipment") if "Shipment" in header else None
    # Retailer scoping (multi-retailer profiles): filter to this retailer's rows only.
    retailer_idx = header.index("Retailer") if "Retailer" in header else None

    orders: dict[str, dict] = {}
    for row in existing[1:]:
        if len(row) <= max(idx.values()):
            continue
        # .strip() on BOTH scoping columns. Retailer below was always stripped; Profile was not,
        # so one invisible trailing space in a Profile cell hid that row from its own run -- and
        # if the rows still visible were all delivered, the order was classed terminal with a real
        # open shipment frozen.
        #
        # ROWS OF ANOTHER PROFILE ARE NOT DROPPED -- they are kept and marked foreign, so that a
        # TERMINAL order recorded under any profile of this retailer still lands in the skip list.
        # An order id is unique per retailer, so "profile-alpha already has this delivered" is a
        # reason for profile-bravo not to fetch it. 58 imported rows stamped
        # profile-alpha were invisible to profile-bravo's 180-day sweep, which re-read the orders
        # and overwrote reconciled costs and rates. OPEN orders stay profile-scoped: another
        # profile's open order is not ours to re-check.
        foreign = profile_label is not None and str(row[idx["Profile"]]).strip() != profile_label
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
            oid, {"order_id": oid, "order_date": "", "order_url": "", "_groups": {}, "_mine": False}
        )
        o["_mine"] = o["_mine"] or not foreign
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
        mine = o.pop("_mine")

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
        if not mine:
            continue  # another profile's OPEN order: not ours to re-check, and not ours to skip

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
