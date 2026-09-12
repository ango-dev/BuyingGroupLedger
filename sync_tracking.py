"""Post tracking numbers to the buying groups, and read their payouts back into the ledger.

This is the step that turns the ledger from a tracker into a P&L. The scrapers fill everything up to
`Total Cost`; `Insurance`, `Payout Amount` and `Payout Date` have been hand-entered until now, and
`Total Profit` (a live sheet formula) reads BLANK until `Payout Amount` is filled — so the profit
column is inert for any row nobody has typed into. This module fills those three cells from the
buying group that actually paid.

SINCE 2026-09-11 `Payout Amount` FILLS EARLY, WITH THE COMMITTED PRICE. BFMR publishes the payout price it has committed to from the moment a purchase exists, so
that lands in Payout Amount as soon as the reservation is linked to an order — with NO Payout Date,
which together with a non-terminal Status is what now says "committed, not settled". Total Profit
therefore shows the PROJECTED profit on open BFMR rows, deliberately. A later read that disagrees
means BFMR moved the price: the cell is updated to the new commitment and an alert names old ->
new. Settlement always wins — the real amount, date and status overwrite the commitment exactly as
before — and a settlement that arrives at a different figure than the commitment is alerted once.

ROUTING IS THE `Buying Group` COLUMN, which config/warehouses.py already derives from the delivery
address. A row goes to exactly one group; `Personal` rows never reach the sheet at all, and
`Unclassified` is skipped and COUNTED rather than guessed at — an unconfigured warehouse is a real
warehouse, and posting it to whichever group sorted first would be far worse than leaving it visible.

WHAT TO SEND IS DERIVED, NOT STORED. The authoritative answer to "have I submitted this?" lives at
the group, and a local mirror drifts both ways — a post that succeeds while the sheet write fails
would re-post forever, and a number pasted into their dashboard by hand would read as unposted. Both
providers answer it themselves (MOD ignores duplicates by contract; BFMR's tracker lists them), so
each run asks. `Tracking Submitted` is a CHECKBOX written for the reader's benefit only and never
consulted to decide what to send — an unticked box beside a shipped package is the thing worth
noticing at a glance. It is ticked once and never cleared.

WITH ONE EXCEPTION: A PACKAGE THE GROUP HAS PAID FOR IS NOT RE-SUBMITTED. That is not a local mirror
being trusted — a payout is proof the group holds the number, which is exactly the evidence the
checkbox lacks. It exists because MOD's `already_submitted` is empty BY DESIGN (it has no endpoint
that can answer the question), so every run re-posted every number ever recorded
that was 17 packages per run, of which 3 were actually new. One batched call hides the cost today,
but the batch grows with the ledger forever. "Paid" alone is NOT enough — BFMR flips the status
before `amount_paid` lands — so a non-zero payout is required too.

Settled packages are still READ for payouts, deliberately. BFMR reports `returned`, which outranks
`paid`, so a post-payment clawback is a real forward transition; dropping settled rows from the read
is the one way to never see it. Both providers' reads are bulk, so keeping them costs nothing.

STATUS ONLY MOVES FORWARD. The groups own the two outcomes a retailer scrape can never see — `paid`
and `return` — but their reports are snapshots, so a write that would walk a row BACKWARDS is
dropped. That guard is load-bearing for MOD returns specifically: MOD publishes no return signal at
all, so a return is typed onto the sheet by hand, while MOD goes on reporting that package as
received (= paid) forever. Without the guard every run would silently undo the correction.

PAYOUTS ARE ALLOCATED PRO-RATA, for the same reason shipping is in `ledger_sync._profit_formula`. A
payout arrives per PACKAGE, but the ledger is one row per (shipment x item), so a box holding two
items has two rows sharing one tracking number. Writing the full payout to each would book it twice.
Splitting it by each row's share of the package's `Total Cost` makes the column sum to exactly what
the group actually paid.

DRY RUN BY DEFAULT — reads live state, writes nothing, sends nothing, spends nothing:
    python -m sync_tracking

Apply for real:
    python -m sync_tracking --apply
    python -m sync_tracking --apply --limit 1        # one package per group, for first validation
    python -m sync_tracking --apply --group BFMR
    python -m sync_tracking --void 1Z999...          # undo an insurance filing

`run()` is also called from main.py so a scheduled run does this inside the same run lock.
"""

import argparse
import logging
import re

from gspread.utils import ValueInputOption, ValueRenderOption

from alerts.notifier import alert
from buying_groups.base import BuyingGroupError, PayoutRecord, TrackingSubmission
from config.warehouses import is_deliberately_unrouted
from buying_groups.registry import PROVIDERS, get_client, resolve_group
from models.order import RETIRED_STATUSES
from sheets.ledger_sync import (
    HEADER,
    _col_letter,
    _get_worksheet,
    _STATUS_RANK,
    _write_profit_formulas,
)

log = logging.getLogger("sync_tracking")

#: The three columns this module owns. Everything else on the row belongs to the scrapers.
#: NB Payout Amount holds two kinds of number now (2026-09-11): the group's COMMITTED price while
#: the package is open (no Payout Date beside it), and the settled figure once paid (the date and
#: status land with it). The pair (Payout Date, Status) — never the amount alone — is what tells
#: them apart, which is why the settled test at plan time requires `paid` status, not just money.
INSURANCE_COL = "Insurance"
PAYOUT_AMOUNT_COL = "Payout Amount"
PAYOUT_DATE_COL = "Payout Date"
STATUS_COL = "Status"
SUBMITTED_COL = "Tracking Submitted"

#: Statuses that can never be submitted. A cancelled order was never shipped to anyone.
_UNPOSTABLE_STATUSES = {"cancelled"}

#: A Tracking Number cell Google Sheets float-ified: a long all-digit number typed without a leading
#: apostrophe is stored as a double and rendered like '9.339589752066617e+21' — with its trailing
#: digits already lost. See the guard in plan_tracking_submissions.
_FLOAT_CORRUPTED_TRACKING = re.compile(r"\d+(\.\d+)?[eE][+-]?\d+")

#: What the undisclosed-split safety net writes into Quantity when it cannot tell how a multi-box
#: line was divided. Not a number, so no API will take it.
_UNRESOLVED_QUANTITY = "*"


# --- the pure planner ---------------------------------------------------------------------------
# Kept here rather than in sheets/ledger_sync.py on purpose: `sheets` is a lower layer than
# `buying_groups`, and having it import a provider registry would invert the dependency — the same
# reason config/cards.py hand-copies KNOWN_RETAILERS instead of importing main. scripts/
# backfill_profit_columns.py:plan_profit_backfill sets the precedent for a pure planner living
# beside its caller.


def plan_tracking_submissions(header: list[str], data_rows: list[list]) -> dict:
    """Read-only: which rows would be posted where, and what would be skipped. Writes nothing.

    Pure `f(header, rows)` so the whole eligibility policy is testable offline, with no sheet and no
    network. `data_rows` is `existing[1:]` (no header row).

    Returns:
        {
          "by_group":            {group_key: [TrackingSubmission, ...]},
          "rows_by_tracking":    {tracking_number: [row_number, ...]},
          "rows_by_order":       {group_key: {order_id: [row_number, ...]}},  # incl. rows with no
                                                        # tracking yet — the expected-payout write
                                                        # starts at purchase link, before shipping
          "order_of_row":        {row_number: order_id},   # so a payout scoped to an order lands only there
          "costs_by_row":        {row_number: float},   # for pro-rata payout allocation
          "status_by_row":       {row_number: str},     # so a payout can never DOWNGRADE a status
          "insurance_by_row":    {row_number: str},     # so an inferred 0 can't clobber a typed one
          "payout_by_row":       {row_number: float | None},  # the cell as it stands — the recorded
                                                        # COMMITMENT until a settlement overwrites it
          "date_by_row":         {row_number: str},     # blank = not settled; gates the commitment
                                                        # writes and makes the paid-vs-committed
                                                        # mismatch alert fire exactly once
          "submitted_by_row":    {row_number: bool},    # already ticked? don't re-tick
          "unresolved_split":    [(row_number, order_id, tracking_number), ...],   # Quantity "*"
          "corrupted_tracking":  [(row_number, order_id, tracking_number), ...],   # float-ified cell
          "unroutable_tracked":  [(row_number, order_id, tracking_number, group_as_written), ...],
          "skipped_no_tracking": int,
          "skipped_unroutable":  {buying_group_as_written: count},
          "skipped_cancelled":   int,
          "cancelled_by_group":  {group_key: [(row_number, order_id), ...]},   # retailer said no
          "awaiting_by_group":   {group_key: [(row_number, order_id), ...]},   # ordered, no tracking yet
        }
    """
    idx = {name: header.index(name) for name in (
        "Order ID", "Order Date", "Item Name", "Quantity", "Tracking Number",
        "Shipment", "Status", "Total Cost", "Buying Group", SUBMITTED_COL, INSURANCE_COL,
        PAYOUT_AMOUNT_COL, PAYOUT_DATE_COL,
    )}
    # Columns a sheet may predate, resolved only if present. They still have to be IN `idx` —
    # `optional_cell` looks them up there, so a name missing from this map reads as "" on every row
    # rather than as "absent from this sheet". That silently emptied `Retailer` for every submission,
    # which switched the Best Buy suffix retry off entirely: `_is_bestbuy("")` is False, so no carton
    # ever reached it. The feature was dead in production while its own unit tests passed, because
    # they build a TrackingSubmission directly and never cross this seam.
    idx.update({name: header.index(name) for name in ("Retailer",) if name in header})

    by_group: dict[str, list[TrackingSubmission]] = {}
    rows_by_tracking: dict[str, list[int]] = {}
    order_of_row: dict[int, str] = {}
    costs_by_row: dict[int, float] = {}
    status_by_row: dict[int, str] = {}
    insurance_by_row: dict[int, str] = {}
    submitted_by_row: dict[int, str] = {}
    unresolved_split: list[tuple] = []
    unroutable_tracked: list[tuple] = []
    cancelled_by_group: dict[str, list[tuple]] = {}
    awaiting_by_group: dict[str, list[tuple]] = {}
    skipped_unroutable: dict[str, int] = {}
    skipped_no_tracking = 0
    skipped_cancelled = 0
    skipped_superseded = 0
    superseded_rows: list[tuple[int, str, str]] = []
    settled_keys: set[tuple[str, str]] = set()
    corrupted_tracking: list[tuple[int, str, str]] = []
    item_of_row: dict[int, str] = {}
    rows_by_order: dict[str, dict[str, list[int]]] = {}
    payout_by_row: dict[int, float | None] = {}
    date_by_row: dict[int, str] = {}

    for offset, row in enumerate(data_rows):
        row_number = offset + 2  # row 1 is the header

        def cell(name: str) -> str:
            i = idx[name]
            return str(row[i]).strip() if i < len(row) else ""

        def optional_cell(name: str) -> str:
            """A column the ledger may predate. Absent -> "", never a KeyError, because a sheet
            missing a newer column must still sync every other field on the row."""
            i = idx.get(name)
            return str(row[i]).strip() if i is not None and i < len(row) else ""

        order_id = cell("Order ID")
        if not order_id:
            continue  # same rule as sync_csv_to_sheet: a blank Order ID is not a real row

        # A RETIRED row (superseded, the design notes) holds a tracking number Amazon re-issued. That dead
        # number really was posted, and the groups hold it -- but it will never move, so it must
        # never be re-posted (MOD's already_submitted is empty by design, so it would go out on
        # every run), never insured, and never handed a payout. Skipping BEFORE rows_by_tracking /
        # costs_by_row are filled is what makes allocate_payouts unable to find the row even if a
        # group reports the number: a zero-cost row would otherwise take an equal share at :434.
        if cell("Status").lower() in RETIRED_STATUSES:
            skipped_superseded += 1
            superseded_rows.append((row_number, order_id, cell("Tracking Number")))
            continue

        if cell("Status").lower() in _UNPOSTABLE_STATUSES:
            skipped_cancelled += 1
            # Kept, not discarded. A cancelled RETAILER order whose buying-group purchase is still
            # open is a live divergence someone has to resolve by hand — see _alert_on_cancelled.
            group_key = resolve_group(cell("Buying Group"))
            if group_key:
                cancelled_by_group.setdefault(group_key, []).append((row_number, order_id))
            continue

        tracking = cell("Tracking Number")
        if _FLOAT_CORRUPTED_TRACKING.fullmatch(tracking):
            # Google Sheets stored a long all-digit tracking number as a NUMBER and rendered it in
            # scientific notation — the trailing digits are gone from the stored double, so the real
            # number is UNRECOVERABLE from the sheet. Submitting the mangled form would post garbage
            # to a group (not undoable at MOD), so the row is withheld and alerted instead: the fix
            # is re-typing the number as text (leading apostrophe). 
            # '9.339589752066617e+21' on an Amazon Business row very nearly went to MOD.
            corrupted_tracking.append((row_number, order_id, tracking))
            continue
        if not tracking:
            skipped_no_tracking += 1
            # Kept, not just counted. An order still AWAITING SHIPMENT is the only state BFMR can
            # cancel a purchase from — their deadline is for submitting tracking, so once a number is
            # attached there is nothing left to cancel over. That makes this the one population worth
            # cross-checking against their cancellations, and until now it never reached a BFMR call
            # at all. See _alert_on_cancelled_purchases.
            group_key = resolve_group(cell("Buying Group"))
            if group_key:
                awaiting_by_group.setdefault(group_key, []).append((row_number, order_id))
                # An awaiting row is exactly where the EXPECTED payout lands first: the purchase
                # exists at the group (the user types the order number in right after ordering)
                # while no tracking number does — so these rows join the order index and carry the
                # maps allocate_expected_payouts prorates and compares with. They stay out of
                # rows_by_tracking, so the real-payout allocation still can't touch them.
                costs_by_row[row_number] = _as_float(cell("Total Cost")) or 0.0
                status_by_row[row_number] = cell("Status").lower()
                item_of_row[row_number] = cell("Item Name")
                payout_by_row[row_number] = _as_float(cell(PAYOUT_AMOUNT_COL))
                date_by_row[row_number] = cell(PAYOUT_DATE_COL)
                rows_by_order.setdefault(group_key, {}).setdefault(order_id, []).append(row_number)
            continue

        group_written = cell("Buying Group")
        group_key = resolve_group(group_written)
        if not group_key:
            label = group_written or "(blank)"
            skipped_unroutable[label] = skipped_unroutable.get(label, 0) + 1
            if is_deliberately_unrouted(group_written):
                # A gift card is not a resale: it routes nowhere BY DESIGN, will never be paid out,
                # and ships with a tracking number like anything else. Without this it would land in
                # unroutable_tracked below and alert on EVERY run for the life of the row, training
                # the user to ignore the one alert that means real money is about to be lost.
                continue
            # A SHIPPED package routing to no group is unsubmittable to ANY of them, which is the
            # same loss as a rejected submission and needs the same urgency — see
            # _alert_on_unroutable. Only rows that HAVE a tracking number are collected: an
            # unclassified row with nothing to submit yet is a config gap to fix at leisure, while
            # this one has a clock on it.
            unroutable_tracked.append((row_number, order_id, tracking, label))
            continue

        quantity_text = cell("Quantity")
        if quantity_text == _UNRESOLVED_QUANTITY:
            # The undisclosed-split net created this row because the retailer rotated a tracking
            # number on a same-SKU multi-box line and never disclosed how the units were divided.
            # Skipping it SILENTLY would mean that box is never submitted and never paid, which is
            # exactly the missed-reimbursement failure this project is built to avoid — so the
            # caller alerts on these rather than logging them away.
            unresolved_split.append((row_number, order_id, tracking))
            continue

        cost = _as_float(cell("Total Cost"))
        costs_by_row[row_number] = cost or 0.0
        status_by_row[row_number] = cell("Status").lower()
        item_of_row[row_number] = cell("Item Name")
        insurance_by_row[row_number] = cell(INSURANCE_COL)
        submitted_by_row[row_number] = _is_ticked(cell(SUBMITTED_COL))
        rows_by_tracking.setdefault(tracking, []).append(row_number)
        order_of_row[row_number] = order_id
        payout_by_row[row_number] = _as_float(cell(PAYOUT_AMOUNT_COL))
        date_by_row[row_number] = cell(PAYOUT_DATE_COL)
        rows_by_order.setdefault(group_key, {}).setdefault(order_id, []).append(row_number)

        # SETTLED = paid, with money actually recorded. Such a package needs no further SUBMITTING:
        # being paid for it is proof the group holds the number, which is far stronger evidence than
        # the `Tracking Submitted` checkbox this module deliberately refuses to trust (a local mirror
        # drifts both ways; a payout cannot).
        #
        # This matters most for MOD, whose `already_submitted` is empty BY DESIGN — it has no way to
        # answer the question, so every run re-posted every number ever recorded. One batched call
        # hides the cost today, but that batch grows with the ledger forever.
        #
        # The zero check is load-bearing: BFMR marks a package paid BEFORE `amount_paid` lands, so
        # "paid" alone would drop packages the group has not actually settled.
        if status_by_row[row_number] == "paid" and (_as_float(cell(PAYOUT_AMOUNT_COL)) or 0.0) != 0.0:
            settled_keys.add((order_id, tracking))
        # A RETURNED package's submission story is equally over (found live: a scheduled
        # run raised ACTION NEEDED for a `return` row's tracking that BFMR would not take). Whether
        # the goods went back to the retailer or the group returned them, there is nothing left to
        # submit or insure — but the row deliberately STAYS in the payout read below, because that
        # is where the group's clawback lands. `return` being TERMINAL only ever governed
        # re-scraping (load_order_state); this is the sync-side half of that finality. Unlike
        # `cancelled` (unpostable above, the order never shipped), a return DID ship, so its row
        # keeps flowing everywhere except the submit and insurance calls.
        if status_by_row[row_number] == "return":
            settled_keys.add((order_id, tracking))

        by_group.setdefault(group_key, []).append(TrackingSubmission(
            row_number=row_number,
            order_id=order_id,
            tracking_number=tracking,
            quantity=_as_int(quantity_text, default=1),
            item_name=cell("Item Name"),
            total_cost=cost,
            shipment=cell("Shipment"),
            order_date=cell("Order Date"),
            buying_group=group_key,
            retailer=optional_cell("Retailer"),
        ))

    return {
        "by_group": by_group,
        "rows_by_tracking": rows_by_tracking,
        "rows_by_order": rows_by_order,
        "order_of_row": order_of_row,
        "item_of_row": item_of_row,
        "costs_by_row": costs_by_row,
        "status_by_row": status_by_row,
        "insurance_by_row": insurance_by_row,
        "payout_by_row": payout_by_row,
        "date_by_row": date_by_row,
        "submitted_by_row": submitted_by_row,
        "unresolved_split": unresolved_split,
        "corrupted_tracking": corrupted_tracking,
        "unroutable_tracked": unroutable_tracked,
        "skipped_no_tracking": skipped_no_tracking,
        "skipped_unroutable": skipped_unroutable,
        "skipped_cancelled": skipped_cancelled,
        "skipped_superseded": skipped_superseded,
        "superseded_rows": superseded_rows,
        "cancelled_by_group": cancelled_by_group,
        "awaiting_by_group": awaiting_by_group,
        "settled_keys": settled_keys,
    }


def allocate_payouts(
    records: list[PayoutRecord],
    rows_by_tracking: dict[str, list[int]],
    costs_by_row: dict[int, float],
    status_by_row: dict[int, str] | None = None,
    insurance_by_row: dict[int, str] | None = None,
    order_of_row: dict[int, str] | None = None,
    item_of_row: dict[int, str] | None = None,
) -> dict[int, dict]:
    """Spread each package's payout across the ledger rows that make up that package.

    A payout is reported per tracking number; the ledger is per (shipment x item). When a box holds
    two line items, writing the whole payout onto both rows would double-count it in every column
    sum. Each row instead takes its share of the package's `Total Cost` — the same pro-rata rule
    ledger_sync._profit_formula already applies to order-level shipping, and for the same reason.

    ONE TRACKING NUMBER CAN COVER TWO ORDERS WITH DIFFERENT OUTCOMES. BFMR held
    TBA999000000005 as order 111-2281555 `paid` ($2,392) AND order 111-8231919 `returned` — and a
    tracking-only bucket merged them, walking the paid order's row to `return` and writing it the
    NETTED amount, again on every later run. So a record that NAMES its order (BFMR's deal rows do)
    lands in a per-order sub-bucket allocated only to that order's rows. Records without an order id
    (every MOD record; BFMR's insurance FEE rows) stay tracking-level: MOD's report has no order
    column, and insurance is a per-package charge shared by every row of the package. An order the
    sheet does not know under this tracking number folds back into the tracking-level remainder
    rather than being dropped.

    AND ONE ORDER CAN HOLD TWO DEALS WITH DIFFERENT OUTCOMES IN ONE BOX. Live on
    114-5684551: the AirTag deal `returned`, the Fitbit deal `paid` $97 — same order, same tracking
    — and the order-level merge walked BOTH rows to `return`. Records carrying an `item_hint`
    (BFMR's deal_title + item name) are therefore kept apart per deal, and the order's rows are
    partitioned among them by WORD OVERLAP with each row's Item Name (BFMR shortens names, but the
    distinctive words — model, color — survive). The partition must be clean: every row picks a
    unique best-matching deal and every deal claims at least one row, or the whole order falls back
    to the merged order-level behavior rather than guessing where money and outcomes land.

    Rows whose costs are all zero (or missing) split the payout evenly rather than dividing by zero;
    that only happens for rows the scraper never priced, and an even split is at least defensible.

    Returns {row_number: {"Insurance": ..., "Payout Amount": ..., "Payout Date": ...}}.
    """
    status_by_row = status_by_row or {}
    insurance_by_row = insurance_by_row or {}
    order_of_row = order_of_row or {}
    item_of_row = item_of_row or {}
    totals: dict[str, dict] = {}
    for record in records:
        bucket = totals.setdefault(
            record.tracking_number,
            {"amount": None, "insurance": None, "date": "", "status": "", "orders": {}},
        )
        target = bucket
        if record.order_id:
            # Sub-bucketed PER DEAL within the order (see the docstring): records about different
            # items must not merge, or one deal's `return` outranks the other deal's `paid` on
            # every row. Records with no item information share the "" key and merge as before.
            deals = bucket["orders"].setdefault(record.order_id, {})
            key = " ".join(sorted(_hint_tokens(record.item_hint)))
            target = deals.setdefault(
                key, {"amount": None, "date": "", "status": "", "hint": record.item_hint})
        if record.payout_amount is not None:
            target["amount"] = (target["amount"] or 0.0) + record.payout_amount
        if record.insurance is not None:
            bucket["insurance"] = (bucket["insurance"] or 0.0) + record.insurance
        if record.payout_date and not target["date"]:
            target["date"] = record.payout_date
        if _status_rank(record.status) > _status_rank(target["status"]):
            target["status"] = record.status

    writes: dict[int, dict] = {}
    for tracking, bucket in totals.items():
        row_numbers = rows_by_tracking.get(tracking) or []
        if not row_numbers:
            continue

        # Per-order groups where the record named one, and a tracking-level remainder for the rest.
        groups: list[tuple[list[int], dict]] = []
        claimed: set[int] = set()
        fallback = {"amount": bucket["amount"], "date": bucket["date"], "status": bucket["status"]}
        for oid, deals in bucket["orders"].items():
            scoped = [n for n in row_numbers if order_of_row.get(n) == oid]
            if not scoped:
                for sub in deals.values():
                    if sub["amount"] is not None:
                        fallback["amount"] = (fallback["amount"] or 0.0) + sub["amount"]
                    if sub["date"] and not fallback["date"]:
                        fallback["date"] = sub["date"]
                    if _status_rank(sub["status"]) > _status_rank(fallback["status"]):
                        fallback["status"] = sub["status"]
                continue
            claimed.update(scoped)
            subs = list(deals.values())
            if len(subs) == 1:
                groups.append((scoped, subs[0]))
                continue
            partition = _split_rows_by_deal(scoped, subs, item_of_row)
            if partition is not None:
                groups.extend((rows, sub) for sub, rows in zip(subs, partition))
            else:
                # Can't tell which row is which deal — merge to order level (the pre-item_hint
                # behavior) rather than guess where the money and the outcomes land.
                log.warning(
                    "%s / %s: %d deal record(s) but the rows' item names don't partition cleanly; "
                    "allocating order-level (a mixed outcome may over-mark rows -- check by hand).",
                    tracking, oid, len(subs),
                )
                groups.append((scoped, _merge_deal_subs(subs)))
        rest = [n for n in row_numbers if n not in claimed]
        if rest:
            groups.append((rest, fallback))

        # Insurance is PER PACKAGE (the fee row names no order), shared across every row of it.
        package_costs = {n: costs_by_row.get(n, 0.0) for n in row_numbers}
        package_total = sum(package_costs.values())

        for group_rows, sub in groups:
            costs = [costs_by_row.get(n, 0.0) for n in group_rows]
            total_cost = sum(costs)
            for row_number, cost in zip(group_rows, costs):
                share = (cost / total_cost) if total_cost else (1 / len(group_rows))
                insurance_share = ((package_costs[row_number] / package_total)
                                   if package_total else (1 / len(row_numbers)))
                cells: dict = {}
                if sub["date"]:
                    cells[PAYOUT_DATE_COL] = sub["date"]
                # An UNPAID package gets no Payout Amount at all — not a zero. `_profit_formula`
                # treats a blank as "not paid out yet" and renders blank, but a literal 0 makes it
                # compute `0 - Total Cost - ...`, a large fictitious LOSS on a healthy order. Same
                # trap as the insurance cell below; both track None separately from 0.
                if sub["amount"] is not None:
                    cells[PAYOUT_AMOUNT_COL] = round(sub["amount"] * share, 2)
                # A group that reports NO insurance figure leaves the cell alone. Writing 0.0 for
                # "unknown" would be silent data loss: BFMR's insurance-read endpoints both 404, so
                # BFMR always reports None — and a zero over a premium typed by hand would inflate
                # that row's profit by the amount paid to insure it. MOD reports a real 0.0 (it
                # never charges any), which DOES get written.
                if bucket["insurance"] is not None:
                    cells[INSURANCE_COL] = round(bucket["insurance"] * insurance_share, 2)
                elif sub["amount"] is not None and not str(
                    insurance_by_row.get(row_number, "")
                ).strip():
                    # SETTLED, and the group reported no premium at all -> a real zero. Only once
                    # settled (BFMR posts the premium before it pays), and only into a BLANK cell —
                    # Insurance was hand-entered for months, and an inferred 0 over a typed figure
                    # would erase a real cost. A premium the group DOES report still wins.
                    cells[INSURANCE_COL] = 0.0
                # STATUS ONLY EVER MOVES FORWARD. A group's report is a snapshot, so a stale or
                # partial read must not walk a row backwards — the case that matters is a MOD
                # return, typed by hand, which MOD's endless "received" reports must not undo.
                if _status_rank(sub["status"]) > _status_rank(status_by_row.get(row_number, "")):
                    cells[STATUS_COL] = sub["status"]
                # A package the group knows but has nothing to say about contributes no cells.
                if any(v not in ("", None) for v in cells.values()):
                    writes[row_number] = cells
    return writes


_HINT_STOPWORDS = {"the", "a", "an", "and", "of", "with", "for", "in", "by", "to"}


def _hint_tokens(text: str) -> set[str]:
    """Comparable words of an item description: lowercase alphanumeric runs, minus filler."""
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t not in _HINT_STOPWORDS}


def _split_rows_by_deal(scoped: list[int], subs: list[dict],
                        item_of_row: dict[int, str]) -> list[list[int]] | None:
    """Partition one order's rows among its deal records by word overlap, or None when unsure.

    Each row goes to the deal whose hint shares the MOST words with the row's Item Name (BFMR
    shortens names, but the distinctive words — model, color, brand — survive both sides). The
    partition only stands when it is CLEAN: every row has a unique, nonzero best match and every
    deal claims at least one row. Anything less falls back to the merged order-level allocation —
    mis-attributing a `return` or a payout to the wrong row is worse than the old coarseness.
    """
    assignment: list[list[int]] = [[] for _ in subs]
    sub_tokens = [_hint_tokens(sub.get("hint", "")) for sub in subs]
    for n in scoped:
        row_tokens = _hint_tokens(item_of_row.get(n, ""))
        scores = [len(row_tokens & tokens) for tokens in sub_tokens]
        best = max(scores)
        if best == 0 or scores.count(best) > 1:
            return None
        assignment[scores.index(best)].append(n)
    if any(not rows for rows in assignment):
        return None  # a deal with money or an outcome would land on no row and silently vanish
    return assignment


def _merge_deal_subs(subs: list[dict]) -> dict:
    """One order-level bucket from several deal records — the pre-item_hint behavior."""
    merged: dict = {"amount": None, "date": "", "status": ""}
    for sub in subs:
        if sub["amount"] is not None:
            merged["amount"] = (merged["amount"] or 0.0) + sub["amount"]
        if sub["date"] and not merged["date"]:
            merged["date"] = sub["date"]
        if _status_rank(sub["status"]) > _status_rank(merged["status"]):
            merged["status"] = sub["status"]
    return merged


def _status_rank(status: str) -> int:
    """Order two statuses by how far through the lifecycle they are. Unknown/blank ranks lowest.

    Reuses ledger_sync's map so "how final is this status?" has ONE answer in the codebase; a second
    private ordering here would drift from the collapse rule the moment either changed.
    """
    return _STATUS_RANK.get((status or "").strip().lower(), -1)


def allocate_expected_payouts(
    records: list[PayoutRecord],
    rows_by_order: dict[str, list[int]],
    costs_by_row: dict[int, float],
    item_of_row: dict[int, str],
    status_by_row: dict[int, str],
    payout_by_row: dict[int, float | None],
    date_by_row: dict[int, str] | None = None,
    settling_rows: set[int] | None = None,
) -> tuple[dict[int, dict], list[str]]:
    """Fill Payout Amount with each order's COMMITTED payout, and spot a commitment that moved.

    The forward-looking half of the payout write: BFMR publishes the payout price from the moment a purchase exists, so Payout
    Amount carries it from purchase link onward — Payout Date stays blank, which is what marks the
    figure as a commitment rather than a settlement — and a tracker read that disagrees with the
    cell is BFMR CHANGING the committed price. That is the event this function detects: the cell
    is updated to the current commitment (the sheet mirrors what BFMR will actually pay) and the
    change is reported for an alert naming old -> new.

    Same shape as allocate_payouts on purpose: records are bucketed per (order, deal-hint), a
    multi-deal order's rows are partitioned by `_split_rows_by_deal` (falling back to one order-
    level bucket when the partition isn't clean), and each bucket's total prorates by the rows'
    share of Total Cost — the standing rule for every order-level amount. `rows_by_order` is ONE
    group's index from the plan, so a record about an order the sheet doesn't know yet (purchase
    typed before the first scrape) simply finds no rows and waits for the next run.

    THREE KINDS OF ROW ARE NEVER TOUCHED, because on them the amount is (or is becoming) real
    money: a row already `paid` or `return`, a row whose Payout Date is set (settled even if the
    status write hasn't landed yet — MOD's dateless payouts are covered by their `paid` status
    instead), and a row in `settling_rows` — the rows THIS run's allocate_payouts is writing a
    settlement onto, which also keeps a partial settlement from reading as a price drop: the
    settled entry leaves the commitment records AND its rows leave this allocation in the same
    run, so the totals still agree.

    The comparison is BETWEEN TOTALS, not per cell — 2dp proration drift can't fake a price
    change — and a bucket whose total still agrees only rewrites cells when membership shifted
    (a new row appended, a blank to fill), silently, so an unchanged commitment queues no writes.

    Returns ({row_number: {PAYOUT_AMOUNT_COL: value}}, [change detail lines]).
    """
    date_by_row = date_by_row or {}
    settling_rows = settling_rows or set()
    totals: dict[str, dict[str, dict]] = {}
    for record in records:
        if record.expected_amount is None or not record.order_id:
            continue
        deals = totals.setdefault(record.order_id, {})
        key = " ".join(sorted(_hint_tokens(record.item_hint)))
        sub = deals.setdefault(key, {"expected": 0.0, "hint": record.item_hint})
        sub["expected"] += record.expected_amount

    writes: dict[int, dict] = {}
    changes: list[str] = []
    for order_id, deals in totals.items():
        scoped = [n for n in rows_by_order.get(order_id) or []
                  if status_by_row.get(n, "") not in ("paid", "return")
                  and not str(date_by_row.get(n, "")).strip()
                  and n not in settling_rows]
        if not scoped:
            continue
        subs = list(deals.values())
        if len(subs) == 1:
            groups = [(scoped, subs[0])]
        else:
            partition = _split_rows_by_deal(scoped, subs, item_of_row)
            if partition is not None:
                groups = [(rows, sub) for sub, rows in zip(subs, partition)]
            else:
                # Can't tell which row is which deal — compare and write at order level rather
                # than guess. The commitment still sums correctly; only its split is coarse.
                merged = {"expected": sum(s["expected"] for s in subs), "hint": ""}
                groups = [(scoped, merged)]

        for group_rows, sub in groups:
            costs = [costs_by_row.get(n, 0.0) for n in group_rows]
            total_cost = sum(costs)
            new_total = round(sub["expected"], 2)
            if new_total <= 0:
                # Same rule as the settlement path: never write a 0 into Payout Amount — the
                # profit formula would compute a large fictitious loss out of it.
                continue
            new_values = {
                n: round(new_total * ((cost / total_cost) if total_cost else (1 / len(group_rows))), 2)
                for n, cost in zip(group_rows, costs)
            }
            old_values = {n: payout_by_row.get(n) for n in group_rows}
            known = [v for v in old_values.values() if v is not None]
            if not known:
                # First sighting: record the commitment, nothing to compare against yet.
                for n, value in new_values.items():
                    writes[n] = {PAYOUT_AMOUNT_COL: value}
                continue
            old_total = round(sum(known), 2)
            # Totals, with slack for one rounding step per row, so proration drift never
            # masquerades as BFMR moving the price.
            if abs(old_total - new_total) > max(0.02, 0.01 * len(group_rows)):
                for n, value in new_values.items():
                    writes[n] = {PAYOUT_AMOUNT_COL: value}
                label = sub["hint"] or item_of_row.get(group_rows[0], "") or "order"
                changes.append(
                    f"  order {order_id} / {label[:60]}: ${old_total:,.2f} -> ${new_total:,.2f}")
            elif any(old_values[n] is None or abs(old_values[n] - new_values[n]) > 0.01
                     for n in group_rows):
                # Same commitment, different split (a row appended, a blank to fill): re-prorate
                # quietly so the cells sum back to the committed total.
                for n, value in new_values.items():
                    writes[n] = {PAYOUT_AMOUNT_COL: value}
    return writes, changes


def _expected_payment_mismatches(records: list[PayoutRecord], plan: dict) -> list[str]:
    """Settled packages whose PAID amount disagrees with the COMMITTED price — detail lines, or [].

    The second half of the price watch: the first (allocate_expected_payouts) catches a commitment
    that moves before payment; this catches a settlement that lands at a different figure than the
    commitment on the same tracker row — a silent short-pay, or a clawback folded into the amount.
    The paid figure still lands on the sheet exactly as always; this only decides whether a human
    hears about the difference.

    ALERTED EXACTLY ONCE: only while some row of the package still has a BLANK Payout Date — the
    run that first writes the settlement. (The AMOUNT cell can't be the gate any more: it holds
    the commitment long before settlement.) Every later run sees the date filled and stays quiet,
    because a mismatch repeated forever is a mismatch that gets muted. MOD's dateless payouts
    can't leak through: every MOD record has no expected_amount and is skipped above.
    """
    lines: list[str] = []
    date_by_row = plan.get("date_by_row") or {}
    for record in records:
        if record.payout_amount is None or record.expected_amount is None:
            continue
        if abs(record.payout_amount - record.expected_amount) <= 0.02:
            continue
        rows = plan["rows_by_tracking"].get(record.tracking_number) or []
        if not any(not str(date_by_row.get(n, "")).strip() for n in rows):
            continue
        label = record.item_hint[:60] or "package"
        lines.append(
            f"  order {record.order_id or '?'} / {label} ({record.tracking_number}): "
            f"paid ${record.payout_amount:,.2f} against a committed "
            f"${record.expected_amount:,.2f}")
    return lines


# --- the orchestrator ---------------------------------------------------------------------------


def run(apply: bool = False, limit: int | None = None, only_group: str | None = None,
        payouts_only: bool = False) -> dict:
    """One full pass: push tracking numbers, file BFMR insurance, pull payouts back.

    `payouts_only` skips the two WRITES to the groups -- no tracking submitted, no insurance filed --
    and only reads payouts back and ticks the packages the group already holds. For a ledger that
    carries orders from buying-group accounts other than the connected ones (imported history):
    those numbers must never be posted as new packages into these accounts.

    Per-group try/except so one provider being down (or unconfigured) never stops the other, and one
    alert per failure — mirroring how main.run_scrape isolates each scraper.
    """
    worksheet = _get_worksheet()
    # UNFORMATTED, so a currency-formatted Total Cost comes back as 3402.0 rather than "$3,402.00".
    existing = worksheet.get_values(value_render_option=ValueRenderOption.unformatted)
    if not existing or not any(str(c).strip() for c in existing[0]):
        log.info("Sheet is empty — nothing to submit.")
        return {}

    header = [str(c) for c in existing[0]]
    if header != list(HEADER):
        raise SystemExit(
            "Sheet header doesn't match the current schema; run `python -m scripts.reorder_sheet` "
            f"first.\n  sheet:    {header}\n  expected: {list(HEADER)}"
        )

    plan = plan_tracking_submissions(header, existing[1:])
    _report_plan(plan, apply)
    _alert_on_unresolved_splits(plan, apply)
    _alert_on_corrupted_tracking(plan, apply)
    _alert_on_unroutable(plan, apply)

    all_writes: dict[int, dict] = {}
    outcomes: dict[str, dict] = {}

    for group_key, rows in sorted(plan["by_group"].items()):
        if only_group and group_key != only_group:
            continue
        if limit is not None:
            rows = _first_n_packages(rows, limit)
        try:
            outcomes[group_key] = _run_one_group(group_key, rows, plan, all_writes, apply, payouts_only)
        except BuyingGroupError as exc:
            log.error("%s: %s", group_key, exc)
            _alert(apply, f"{group_key}: buying-group sync failed", str(exc))
        except Exception:
            log.exception("%s: unexpected failure", group_key)
            _alert(apply, f"{group_key}: buying-group sync failed",
                   "Unexpected error — check logs/run.log.")

    if all_writes:
        _write_payout_cells(worksheet, all_writes, apply)
    return {"plan": plan, "outcomes": outcomes, "writes": all_writes}


def _alert_on_cancelled_orders(group_key, client, plan, apply) -> None:
    """The retailer cancelled the order, but the buying group still holds an open purchase.

    **THIS TOOL NEVER CANCELS ANYTHING, and that is a deliberate invariant, not an omission.**
    BFMR exposes `purchase/cancel` and `reservation/cancel`; neither is called from anywhere in this
    codebase, and `tests/test_buying_groups.py` asserts that stays true. Cancelling gives up the
    RESERVATION — the spot in the deal — which is often still wanted, because a retailer-cancelled
    order is usually one worth re-ordering. Reclaiming a lost spot may be impossible; undoing a
    cancellation certainly is. So the asymmetry says alert, and let a human decide.
    """
    cancelled = plan["cancelled_by_group"].get(group_key) or []
    if not cancelled or not hasattr(client, "active_purchases_for"):
        return

    still_open = client.active_purchases_for(order_id for _row, order_id in cancelled)
    affected = [(row, order_id) for row, order_id in cancelled if order_id in still_open]
    if not affected:
        return

    detail = "\n".join(f"  row {row}: order {order_id}" for row, order_id in affected)
    log.warning("%s: %d cancelled order(s) still have an open purchase", group_key, len(affected))
    _alert(
        apply,
        f"ACTION NEEDED — {group_key}: {len(affected)} cancelled order(s) still open there",
        f"These orders are CANCELLED at the retailer, but {group_key} still shows an active "
        f"purchase against the reservation:\n{detail}\n\n"
        f"Decide and act by hand in My Tracker. This tool deliberately never cancels: cancelling "
        f"releases the RESERVATION, and a retailer-cancelled order is often one you want to "
        f"re-order into the same spot — which may not be reclaimable once given up.\n\n"
        f"If you are not re-ordering, cancel the purchase there so it doesn't sit against your "
        f"quota.",
    )


def _alert_on_cancelled_purchases(group_key, client, plan, apply) -> None:
    """BFMR cancelled the purchase, but the retailer order is alive and still coming.

    The mirror of `_alert_on_cancelled_orders`, and the more likely direction. BFMR cancels a
    purchase whose tracking number missed their deadline — which by definition happens while the
    order is still AWAITING SHIPMENT, the one state that never reached a BFMR call before this.
    Once a package ships and its number is attached there is nothing left for them to cancel over,
   so `submit_tracking`'s own cancelled-purchase check was guarding a state this
    can barely occur in.

    Silence here is expensive and completely invisible: the deal is gone, so the package arrives, the
    warehouse receives it, and nothing is ever paid for it. Worse, it is only actionable in the gap
    between the cancellation and delivery — long enough to raise a support ticket and ask for
    reinstatement, or to decide not to keep the goods. By the time the row ships and the existing
    check finally notices, that window has closed.
    """
    awaiting = plan.get("awaiting_by_group", {}).get(group_key) or []
    if not awaiting or not hasattr(client, "cancelled_purchases_for"):
        return

    dead = client.cancelled_purchases_for(order_id for _row, order_id in awaiting)
    affected = [(row, order_id) for row, order_id in awaiting if order_id in dead]
    if not affected:
        return

    detail = "\n".join(f"  row {row}: order {order_id}" for row, order_id in affected)
    log.warning("%s: %d awaiting order(s) have a CANCELLED purchase", group_key, len(affected))
    _alert(
        apply,
        f"ACTION NEEDED — {group_key}: {len(affected)} incoming order(s) have a CANCELLED purchase",
        f"{group_key} has CANCELLED the purchase for these orders, but the retailer has NOT "
        f"cancelled them — they are still on their way:\n{detail}\n\n"
        f"The usual cause is the tracking number missing {group_key}'s deadline. The deal is gone, so "
        f"as things stand the package will arrive at the warehouse and NOTHING WILL BE PAID for it.\n\n"
        f"This is only fixable NOW, before it lands: raise a support ticket with proof of purchase "
        f"and ask them to reinstate the purchase, or decide not to keep the goods and cancel at the "
        f"retailer while you still can. Once it ships and the tracking is submitted there is nothing "
        f"for the number to attach to.\n\n"
        f"This tool never cancels anything at a buying group, so nothing has been changed for you.",
    )


def _alert(apply: bool, subject: str, message: str) -> None:
    """Alert only on a real run.

    Alerts exist to reach someone who ISN'T watching — a scheduled run at 3am. A dry run is a person
    sitting at a terminal reading the output, so emailing them the same text is pure noise, and a
    channel that cries wolf during routine inspection gets muted right when it matters.
    """
    if apply:
        alert(subject, message)
    else:
        log.warning("%s: %s (no alert sent — dry run)", subject, message)


def _run_one_group(group_key, rows, plan, all_writes, apply, payouts_only: bool = False) -> dict:
    client = get_client(group_key, dry_run=not apply)

    # Keyed on (order_id, tracking_number), not tracking alone: a Best Buy COMBINED BOX puts two
    # orders under one tracking number, and each still needs its own submission.
    known = client.already_submitted(rows)
    # A SETTLED package is not re-submitted: the group paid for it, which is proof it holds the
    # number. This is the ledger answering a question MOD cannot — its `already_submitted` is empty
    # by design, so without this every run re-posted every number ever recorded, in a batch that
    # grows with the sheet forever.
    settled = plan.get("settled_keys") or set()
    fresh = [] if payouts_only else [
        r for r in rows
        if (r.order_id, r.tracking_number) not in known
        and (r.order_id, r.tracking_number) not in settled]
    if payouts_only:
        log.info("%s: payouts-only -- nothing submitted, nothing insured", group_key)
    if known:
        log.info("%s: %d package(s) already recorded there", group_key, len(known))
    settled_here = sum(1 for r in rows if (r.order_id, r.tracking_number) in settled)
    if settled_here:
        log.info("%s: %d package(s) settled (paid or returned), not re-submitting",
                 group_key, settled_here)

    push = client.submit_tracking(fresh)
    log.info("%s push: %s", group_key, push.summary())
    for tracking, reason in push.failed:
        log.error("%s: %s", group_key, reason)
    if push.failed:
        _alert(
            apply,
            f"{group_key}: {len(push.failed)} tracking number(s) rejected",
            "\n".join(reason for _t, reason in push.failed),
        )

    for _tracking, reason in push.needs_manual:
        log.warning("%s: %s", group_key, reason)
    if push.needs_manual:
        # Its own alert, deliberately not folded into the one above. This is not a transient error
        # to look at when convenient: the package stays unsubmitted — and therefore unreimbursed —
        # until someone performs the specific steps in the message. Burying it among retryable
        # failures is how a Best Buy combined carton goes unnoticed for weeks.
        #
        # FIRED IMMEDIATELY, NEVER HELD. An earlier version waited until the package was `delivered`
        # to avoid crying wolf while BFMR's asynchronous Best Buy check was still running. That was
        # backwards: **most buying groups only insure a package if its tracking number was submitted
        # BEFORE delivery**, so delivery is precisely the moment the alert stops
        # being actionable. Waiting for certainty costs the cover the alert exists to protect, while
        # a false alarm costs one glance at My Tracker.
        _alert(
            apply,
            f"ACTION NEEDED — {group_key}: {len(push.needs_manual)} package(s) could not be submitted",
            "\n\n".join(reason for _t, reason in push.needs_manual),
        )

    insurance = None
    if hasattr(client, "file_insurance") and not payouts_only:
        # Only for shipments the group now has on file — insurance is filed against a known
        # shipment, so a row whose submission just failed must not be insured.
        blocked = {t for t, _ in push.failed} | {t for t, _ in push.needs_manual}
        # Settled packages are excluded too — insuring one BFMR has already paid out is pointless, and
        # BFMR reports them `not_eligible` anyway (its own `status` is past the insurable window).
        insurable = [r for r in rows
                     if r.tracking_number not in blocked
                     and (r.order_id, r.tracking_number) not in settled]
        insurance = client.file_insurance(insurable)
        log.info("%s insurance: %s", group_key, insurance.summary())

        # BFMR's DONATION program: a 1-cent deal is submitted like any other
        # package but is never insured — the client skips its filing with DONATION_SKIP_REASON, and
        # a real $0.00 lands in the row's Insurance cell so the sheet reads "no premium, by design"
        # rather than "still waiting". Blank cells only; a typed figure always wins.
        from buying_groups.bfmr import DONATION_SKIP_REASON  # local: MOD has no insurance at all
        donation_rows = sorted({
            n
            for tracking, reason in insurance.skipped
            if reason == DONATION_SKIP_REASON
            for n in plan["rows_by_tracking"].get(tracking, [])
            if not str(plan["insurance_by_row"].get(n, "")).strip()
        })
        if donation_rows:
            log.info("%s: %d donation-shipment row(s) marked Insurance $0.00.",
                     group_key, len(donation_rows))
            _merge_writes(all_writes, {n: {INSURANCE_COL: 0.0} for n in donation_rows})

        # Anything the donation skip did not explain is a real refusal on a package that SHOULD be
        # covered. Before 2026-09-01 one of these aborted the whole sync (loud but useless); now it
        # is caught per shipment, so it must be alerted or a genuinely uninsured package goes quiet.
        if insurance.failed:
            _alert(
                apply,
                f"{group_key}: {len(insurance.failed)} insurance filing(s) rejected",
                "\n".join(reason for _t, reason in insurance.failed),
            )

    payouts = client.fetch_payouts([r.tracking_number for r in rows])
    payout_writes = allocate_payouts(
        payouts, plan["rows_by_tracking"], plan["costs_by_row"], plan["status_by_row"],
        plan["insurance_by_row"], plan.get("order_of_row"), plan.get("item_of_row"),
    )
    _merge_writes(all_writes, payout_writes)
    log.info("%s: %d payout record(s) read back", group_key, len(payouts))

    # A settlement that landed at a different figure than the group COMMITTED to. Checked on the
    # run that first writes the payout date (see _expected_payment_mismatches), so it fires once.
    mismatches = _expected_payment_mismatches(payouts, plan)
    if mismatches:
        _alert(
            apply,
            f"ACTION NEEDED — {group_key}: {len(mismatches)} payout(s) disagree with the "
            "committed price",
            f"{group_key} settled these packages at a different amount than the payout price it "
            "committed to. The PAID amount is what landed on the sheet — check the deal terms in "
            "My Tracker and raise it with them if the shortfall is real:\n"
            + "\n".join(mismatches),
        )

    # The committed price, written into Payout Amount (no Payout Date) from the moment the
    # purchase links the reservation to an order — and watched: a commitment that MOVED is
    # rewritten to the new figure and alerted old -> new. Rows this run is SETTLING are handed to
    # the allocator so the commitment pass can never overwrite real money or misread a partial
    # settlement as a price drop. hasattr-gated like file_insurance: only BFMR publishes a price
    # (MOD's API has none).
    orders = (plan.get("rows_by_order") or {}).get(group_key) or {}
    if orders and hasattr(client, "fetch_expected_payouts"):
        settling = {
            n for n, cells in payout_writes.items()
            if PAYOUT_AMOUNT_COL in cells or PAYOUT_DATE_COL in cells
            or cells.get(STATUS_COL) in ("paid", "return")
        }
        commitments = client.fetch_expected_payouts(list(orders))
        expected_writes, price_changes = allocate_expected_payouts(
            commitments, orders, plan["costs_by_row"], plan["item_of_row"],
            plan["status_by_row"], plan["payout_by_row"], plan.get("date_by_row"),
            settling_rows=settling,
        )
        _merge_writes(all_writes, expected_writes)
        if expected_writes:
            log.info("%s: %d committed-price cell(s) planned from %d open commitment(s).",
                     group_key, len(expected_writes), len(commitments))
        if price_changes:
            log.warning("%s: %d committed payout price(s) moved", group_key, len(price_changes))
            _alert(
                apply,
                f"{group_key}: {len(price_changes)} committed payout price(s) changed",
                f"{group_key} changed the payout price it commits to on these orders. Payout "
                "Amount now carries the NEW commitment (no Payout Date — nothing has been paid "
                "yet). If a drop is not one you agreed to, take it up with them before the "
                "package settles:\n"
                + "\n".join(price_changes),
            )

    _alert_on_cancelled_orders(group_key, client, plan, apply)
    _alert_on_cancelled_purchases(group_key, client, plan, apply)

    pushed = set(push.submitted)
    ticked = {
        row.row_number for row in rows
        if (row.order_id, row.tracking_number) in known or row.tracking_number in pushed
    }
    _merge_writes(all_writes, _tick_submitted(ticked, plan, apply))
    return {"push": push, "insurance": insurance, "payouts": payouts}


def _tick_submitted(row_numbers, plan, apply) -> dict[int, dict]:
    """Tick the checkbox for every package the group HOLDS — not merely the ones we just sent.

    The column answers "is this tracking number with the buying group?", so the set is everything
    `already_submitted` reported PLUS whatever this run pushed. Ticking only our own submissions
    would leave a permanently empty box beside any package that reached the group another way — one
    submitted through their dashboard, or before this tool existed — which is precisely the reading
    an unticked box is supposed to rule out. (As a timestamp this column meant "when WE sent it", and
    that distinction mattered; as a checkbox it means "is it there", and it doesn't.)

    Already-ticked rows are skipped rather than re-written. Harmless in itself, but it would queue a
    cell write and a formula re-stamp for every package on every run.

    Nothing is ticked on a dry run, because nothing was submitted. And nothing is ever UNTICKED: a
    box is cleared only by a human who has decided the package really wasn't handed over.
    """
    if not apply:
        return {}
    return {
        row_number: {SUBMITTED_COL: True}
        for row_number in row_numbers
        if not plan["submitted_by_row"].get(row_number)
    }


def _is_ticked(value) -> bool:
    """Is this checkbox cell ticked? Tolerates the real boolean, "TRUE", and a hand-typed variant."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "yes", "y", "1", "checked"}


def _merge_writes(target: dict[int, dict], incoming: dict[int, dict]) -> None:
    """Fold per-row cell updates together so two providers (or two passes) can touch one row."""
    for row_number, cells in incoming.items():
        target.setdefault(row_number, {}).update(cells)


def _write_payout_cells(worksheet, writes: dict[int, dict], apply: bool) -> None:
    """Write Insurance / Payout Amount / Payout Date, then RE-STAMP the profit formula.

    The re-stamp is not optional. `_write_profit_formulas` exists because a RAW write over a row
    carrying the Total Profit formula freezes it into whatever number it last evaluated to, and this
    module writes precisely the cells that formula depends on — so skipping it would leave every
    touched row showing a stale profit that nothing downstream would flag.
    `scripts/audit_sheet.py:check_profit_formula_literal` is the tripwire for getting this wrong.
    """
    data = [
        {"range": f"{_col_letter(HEADER.index(col))}{row_number}", "values": [[value]]}
        for row_number, cells in sorted(writes.items())
        for col, value in cells.items()
        if value not in ("", None)
    ]
    if not apply:
        print(f"\nDry run: would write {len(data)} payout cell(s) across "
              f"{len(writes)} row(s); nothing was written.")
        return
    if data:
        worksheet.batch_update(data, value_input_option=ValueInputOption.raw)
        log.info("Wrote %d payout cell(s) across %d row(s).", len(data), len(writes))
    _write_profit_formulas(worksheet, sorted(writes))


def _alert_on_unroutable(plan: dict, apply: bool) -> None:
    """A SHIPPED package whose Buying Group routes to no provider — unsubmittable to ANY of them.

    Until now this was only printed in the run summary, on the reasoning that an unrecognised
    warehouse is a config gap rather than an error. That reasoning holds for a row with nothing to
    submit yet; it does not hold once the package is in the carrier's hands, because then it is the
    same loss as a rejected submission — the group never learns about the package, so it is neither
    reimbursed nor insured — and it carries the same deadline (see `_run_one_group`: most groups only
    insure a package whose tracking number arrived BEFORE delivery).

    The fix is quick, which is exactly why it is worth interrupting someone for: add the warehouse's
    address to config.json `warehouses`, or set the row's Buying Group by hand, and the next run submits it.
    """
    rows = plan.get("unroutable_tracked") or []
    if not rows:
        return
    detail = "\n".join(
        f"  row {n}: order {oid}, tracking {t}, Buying Group {label!r}"
        for n, oid, t, label in rows
    )
    log.warning("%d shipped row(s) route to no buying group", len(rows))
    _alert(
        apply,
        f"ACTION NEEDED — {len(rows)} shipped package(s) route to no buying group",
        "These rows have a tracking number but their Buying Group matches no configured provider, "
        "so they cannot be submitted anywhere. Nobody is expecting these packages, and no insurance "
        "can be filed on them.\n\n"
        "MOST GROUPS ONLY INSURE A PACKAGE IF ITS TRACKING NUMBER WAS SUBMITTED BEFORE DELIVERY, so "
        "this is worth fixing now rather than at the end of the week.\n\n"
        "Add the delivery address to config.json `warehouses` (or set Buying Group on the row by hand) "
        "and "
        f"the next run will submit them:\n{detail}",
    )


def _alert_on_unresolved_splits(plan: dict, apply: bool) -> None:
    """A `Quantity: *` row is an un-submittable package, i.e. money that will never be reimbursed.

    It is alerted rather than logged because nothing else will ever surface it: the safety net that
    created the row alerted once at creation, and after that the row simply sits there looking
    ordinary while its box goes unpaid.
    """
    rows = plan["unresolved_split"]
    if not rows:
        return
    detail = "\n".join(
        f"  row {n}: order {oid}, tracking {t}" for n, oid, t in rows
    )
    log.warning("%d row(s) have an unresolved split quantity and cannot be submitted", len(rows))
    _alert(
        apply,
        f"{len(rows)} package(s) cannot be submitted to a buying group",
        "These rows carry Quantity '*' from the undisclosed-split safety net, so no buying group "
        "will accept them. Set the real per-box quantity on each row and they'll go out on the "
        f"next run:\n{detail}",
    )


def _alert_on_corrupted_tracking(plan: dict, apply: bool) -> None:
    """A Tracking Number cell Sheets float-ified is garbage with the real digits already lost.

    Alerted rather than logged for the same reason as unresolved splits: the row looks ordinary
    while its package can never be submitted — and the mangled number is one --apply away from
    being posted to a group, which at MOD is not undoable. Only a human can fix it, by re-typing
    the number from the carrier email/page AS TEXT (a leading apostrophe: '9339...).
    """
    rows = plan.get("corrupted_tracking") or []
    if not rows:
        return
    detail = "\n".join(f"  row {n}: order {oid}, cell shows {t!r}" for n, oid, t in rows)
    log.warning("%d row(s) have a float-corrupted tracking number and cannot be submitted", len(rows))
    _alert(
        apply,
        f"{len(rows)} tracking number(s) were mangled by Sheets and need re-typing",
        "These Tracking Number cells were stored as NUMBERS, so Sheets rendered them in scientific "
        "notation and the trailing digits are permanently gone from the sheet. The rows are "
        "withheld from every submission until fixed. Re-type each number from the carrier "
        "email/page AS TEXT — start the cell with an apostrophe ('):\n" + detail,
    )


def _first_n_packages(rows: list[TrackingSubmission], limit: int) -> list[TrackingSubmission]:
    """Take the first `limit` PACKAGES, not rows — a package's rows must never be split apart.

    Submitting half a box would under-report its value to MOD (whose amount is summed per tracking
    number) and leave the rest permanently unsubmitted, since the number would then read as known.
    """
    keep: list[str] = []
    for row in rows:
        if row.tracking_number not in keep:
            if len(keep) >= limit:
                continue
            keep.append(row.tracking_number)
    return [r for r in rows if r.tracking_number in keep]


def _report_plan(plan: dict, apply: bool) -> None:
    mode = "APPLYING" if apply else "DRY RUN — nothing will be sent or written"
    print(f"Buying-group sync ({mode}):\n")
    for group_key, rows in sorted(plan["by_group"].items()):
        packages = len({r.tracking_number for r in rows})
        print(f"  {group_key}: {len(rows)} row(s) across {packages} package(s)")
        for row in rows[:8]:
            print(f"    row {row.row_number:>3}  {row.order_id:<20} {row.tracking_number:<24} "
                  f"qty {row.quantity}")
        if len(rows) > 8:
            print(f"    ... and {len(rows) - 8} more")
    if not plan["by_group"]:
        print("  nothing eligible to submit")

    print()
    if plan["skipped_no_tracking"]:
        print(f"  {plan['skipped_no_tracking']} row(s) have no tracking number yet")
    if plan["skipped_cancelled"]:
        print(f"  {plan['skipped_cancelled']} cancelled row(s) skipped")
    if plan.get("skipped_superseded"):
        print(f"  {plan['skipped_superseded']} superseded row(s) skipped -- re-labelled packages; "
              "their dead numbers are never submitted, insured or paid")
    for label, count in sorted(plan["skipped_unroutable"].items()):
        print(f"  {count} row(s) tagged {label!r} route to no configured buying group")
    if plan["unresolved_split"]:
        print(f"  {len(plan['unresolved_split'])} row(s) have Quantity '*' and CANNOT be "
              f"submitted — set the real per-box quantity (an alert has been sent)")


def _as_float(value) -> float | None:
    text = str(value or "").strip().replace("$", "").replace(",", "")
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _as_int(value, default: int = 1) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--apply", action="store_true",
                        help="Actually submit and write (default: dry run, nothing sent)")
    parser.add_argument("--limit", type=int,
                        help="Only handle the first N packages per group — use for first validation")
    parser.add_argument("--group", choices=sorted(PROVIDERS),
                        help="Only run this buying group")
    parser.add_argument("--payouts-only", action="store_true",
                        help="Read payouts back and tick what each group holds; submit NOTHING and file no insurance")
    parser.add_argument("--void", nargs="+", metavar="TRACKING",
                        help="Void a BFMR insurance filing for these tracking numbers and exit")
    args = parser.parse_args()

    if args.void:
        client = get_client("BFMR", dry_run=not args.apply)
        result = client.void_insurance(args.void)
        print(f"Insurance void: {result.summary()}")
        return

    run(apply=args.apply, limit=args.limit, only_group=args.group, payouts_only=args.payouts_only)
    if not args.apply:
        print("\nDry run only — nothing sent, nothing written. Re-run with --apply to act.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    main()
