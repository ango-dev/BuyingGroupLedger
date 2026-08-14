"""Post tracking numbers to the buying groups, and read their payouts back into the ledger.

This is the step that turns the ledger from a tracker into a P&L. The scrapers fill everything up to
`Total Cost`; `Insurance`, `Payout Amount` and `Payout Date` have been hand-entered until now, and
`Total Profit` (a live sheet formula) reads BLANK until `Payout Amount` is filled — so the profit
column is inert for any row nobody has typed into. This module fills those three cells from the
buying group that actually paid.

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

from gspread.utils import ValueInputOption, ValueRenderOption

from alerts.notifier import alert
from buying_groups.base import BuyingGroupError, PayoutRecord, TrackingSubmission
from buying_groups.registry import PROVIDERS, get_client, resolve_group
from sheets.ledger_sync import (
    HEADER,
    _col_letter,
    _get_worksheet,
    _STATUS_RANK,
    _write_profit_formulas,
)

log = logging.getLogger("sync_tracking")

#: The three columns this module owns. Everything else on the row belongs to the scrapers.
INSURANCE_COL = "Insurance"
PAYOUT_AMOUNT_COL = "Payout Amount"
PAYOUT_DATE_COL = "Payout Date"
STATUS_COL = "Status"
SUBMITTED_COL = "Tracking Submitted"

#: Statuses that can never be submitted. A cancelled order was never shipped to anyone.
_UNPOSTABLE_STATUSES = {"cancelled"}

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
          "costs_by_row":        {row_number: float},   # for pro-rata payout allocation
          "status_by_row":       {row_number: str},     # so a payout can never DOWNGRADE a status
          "insurance_by_row":    {row_number: str},     # so an inferred 0 can't clobber a typed one
          "submitted_by_row":    {row_number: bool},    # already ticked? don't re-tick
          "unresolved_split":    [(row_number, order_id, tracking_number), ...],   # Quantity "*"
          "unroutable_tracked":  [(row_number, order_id, tracking_number, group_as_written), ...],
          "skipped_no_tracking": int,
          "skipped_unroutable":  {buying_group_as_written: count},
          "skipped_cancelled":   int,
          "cancelled_by_group":  {group_key: [(row_number, order_id), ...]},
        }
    """
    idx = {name: header.index(name) for name in (
        "Order ID", "Order Date", "Item Name", "Quantity", "Tracking Number",
        "Shipment", "Status", "Total Cost", "Buying Group", SUBMITTED_COL, INSURANCE_COL,
    )}

    by_group: dict[str, list[TrackingSubmission]] = {}
    rows_by_tracking: dict[str, list[int]] = {}
    costs_by_row: dict[int, float] = {}
    status_by_row: dict[int, str] = {}
    insurance_by_row: dict[int, str] = {}
    submitted_by_row: dict[int, str] = {}
    unresolved_split: list[tuple] = []
    unroutable_tracked: list[tuple] = []
    cancelled_by_group: dict[str, list[tuple]] = {}
    skipped_unroutable: dict[str, int] = {}
    skipped_no_tracking = 0
    skipped_cancelled = 0

    for offset, row in enumerate(data_rows):
        row_number = offset + 2  # row 1 is the header

        def cell(name: str) -> str:
            i = idx[name]
            return str(row[i]).strip() if i < len(row) else ""

        order_id = cell("Order ID")
        if not order_id:
            continue  # same rule as sync_csv_to_sheet: a blank Order ID is not a real row

        if cell("Status").lower() in _UNPOSTABLE_STATUSES:
            skipped_cancelled += 1
            # Kept, not discarded. A cancelled RETAILER order whose buying-group purchase is still
            # open is a live divergence someone has to resolve by hand — see _alert_on_cancelled.
            group_key = resolve_group(cell("Buying Group"))
            if group_key:
                cancelled_by_group.setdefault(group_key, []).append((row_number, order_id))
            continue

        tracking = cell("Tracking Number")
        if not tracking:
            skipped_no_tracking += 1
            continue

        group_written = cell("Buying Group")
        group_key = resolve_group(group_written)
        if not group_key:
            label = group_written or "(blank)"
            skipped_unroutable[label] = skipped_unroutable.get(label, 0) + 1
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
        insurance_by_row[row_number] = cell(INSURANCE_COL)
        submitted_by_row[row_number] = _is_ticked(cell(SUBMITTED_COL))
        rows_by_tracking.setdefault(tracking, []).append(row_number)

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
        ))

    return {
        "by_group": by_group,
        "rows_by_tracking": rows_by_tracking,
        "costs_by_row": costs_by_row,
        "status_by_row": status_by_row,
        "insurance_by_row": insurance_by_row,
        "submitted_by_row": submitted_by_row,
        "unresolved_split": unresolved_split,
        "unroutable_tracked": unroutable_tracked,
        "skipped_no_tracking": skipped_no_tracking,
        "skipped_unroutable": skipped_unroutable,
        "skipped_cancelled": skipped_cancelled,
        "cancelled_by_group": cancelled_by_group,
    }


def allocate_payouts(
    records: list[PayoutRecord],
    rows_by_tracking: dict[str, list[int]],
    costs_by_row: dict[int, float],
    status_by_row: dict[int, str] | None = None,
    insurance_by_row: dict[int, str] | None = None,
) -> dict[int, dict]:
    """Spread each package's payout across the ledger rows that make up that package.

    A payout is reported per tracking number; the ledger is per (shipment x item). When a box holds
    two line items, writing the whole payout onto both rows would double-count it in every column
    sum. Each row instead takes its share of the package's `Total Cost` — the same pro-rata rule
    ledger_sync._profit_formula already applies to order-level shipping, and for the same reason.

    Rows whose costs are all zero (or missing) split the payout evenly rather than dividing by zero;
    that only happens for rows the scraper never priced, and an even split is at least defensible.

    Returns {row_number: {"Insurance": ..., "Payout Amount": ..., "Payout Date": ...}}.
    """
    status_by_row = status_by_row or {}
    insurance_by_row = insurance_by_row or {}
    totals: dict[str, dict] = {}
    for record in records:
        bucket = totals.setdefault(
            record.tracking_number,
            {"amount": None, "insurance": None, "date": "", "status": ""},
        )
        if record.payout_amount is not None:
            bucket["amount"] = (bucket["amount"] or 0.0) + record.payout_amount
        if record.insurance is not None:
            bucket["insurance"] = (bucket["insurance"] or 0.0) + record.insurance
        if record.payout_date and not bucket["date"]:
            bucket["date"] = record.payout_date
        if _status_rank(record.status) > _status_rank(bucket["status"]):
            bucket["status"] = record.status

    writes: dict[int, dict] = {}
    for tracking, bucket in totals.items():
        row_numbers = rows_by_tracking.get(tracking) or []
        if not row_numbers:
            continue
        costs = [costs_by_row.get(n, 0.0) for n in row_numbers]
        total_cost = sum(costs)
        for row_number, cost in zip(row_numbers, costs):
            share = (cost / total_cost) if total_cost else (1 / len(row_numbers))
            cells: dict = {PAYOUT_DATE_COL: bucket["date"]}
            # An UNPAID package gets no Payout Amount at all — not a zero. `_profit_formula` treats a
            # blank as "not paid out yet" and renders blank, but a literal 0 makes it compute
            # `0 - Total Cost - ...`, i.e. a large fictitious LOSS on a perfectly healthy order. Same
            # trap as the insurance cell below, and the reason both track None separately from 0.
            if bucket["amount"] is not None:
                cells[PAYOUT_AMOUNT_COL] = round(bucket["amount"] * share, 2)
            # A group that reports NO insurance figure leaves the cell alone. Writing 0.0 for
            # "unknown" would be a silent data loss: BFMR's two insurance-read endpoints both 404,
            # so BFMR always reports None — and a zero written over a premium the user typed by hand
            # would quietly inflate that row's profit by exactly the amount they paid to insure it.
            # MOD reports a real 0.0 (it never charges any), which DOES get written.
            if bucket["insurance"] is not None:
                cells[INSURANCE_COL] = round(bucket["insurance"] * share, 2)
            elif bucket["amount"] is not None and not str(
                insurance_by_row.get(row_number, "")
            ).strip():
                # SETTLED, and the group reported no premium at all -> a real zero.
                #
                # Only once settled: BFMR posts the premium line before it pays, so a missing one on
                # an OPEN package may simply not have been posted yet, where a missing one on a paid
                # package means there was never a charge. And only into a BLANK cell — Insurance was
                # hand-entered for months, and an inferred 0 written over a figure someone typed
                # would erase a real cost and overstate that row's profit. A premium the group DOES
                # report still wins, since that number is authoritative.
                cells[INSURANCE_COL] = 0.0
            # STATUS ONLY EVER MOVES FORWARD. A group's report is a snapshot, so a stale or partial
            # read must not walk a row backwards — and the case that matters is a MOD return, which
            # has no API signal at all and is therefore typed onto the sheet by hand. MOD keeps
            # reporting that package as received (= "paid") forever, so without this guard every
            # single run would overwrite the human's "return" and the reversal would vanish.
            if _status_rank(bucket["status"]) > _status_rank(status_by_row.get(row_number, "")):
                cells[STATUS_COL] = bucket["status"]
            # A package the group knows about but has nothing to say about yet contributes no cells;
            # recording it would only queue a pointless formula re-stamp.
            if any(v not in ("", None) for v in cells.values()):
                writes[row_number] = cells
    return writes


def _status_rank(status: str) -> int:
    """Order two statuses by how far through the lifecycle they are. Unknown/blank ranks lowest.

    Reuses ledger_sync's map so "how final is this status?" has ONE answer in the codebase; a second
    private ordering here would drift from the collapse rule the moment either changed.
    """
    return _STATUS_RANK.get((status or "").strip().lower(), -1)


# --- the orchestrator ---------------------------------------------------------------------------


def run(apply: bool = False, limit: int | None = None, only_group: str | None = None) -> dict:
    """One full pass: push tracking numbers, file BFMR insurance, pull payouts back.

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
    _alert_on_unroutable(plan, apply)

    all_writes: dict[int, dict] = {}
    outcomes: dict[str, dict] = {}

    for group_key, rows in sorted(plan["by_group"].items()):
        if only_group and group_key != only_group:
            continue
        if limit is not None:
            rows = _first_n_packages(rows, limit)
        try:
            outcomes[group_key] = _run_one_group(group_key, rows, plan, all_writes, apply)
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


def _run_one_group(group_key, rows, plan, all_writes, apply) -> dict:
    client = get_client(group_key, dry_run=not apply)

    # Keyed on (order_id, tracking_number), not tracking alone: a Best Buy COMBINED BOX puts two
    # orders under one tracking number, and each still needs its own submission.
    known = client.already_submitted(rows)
    fresh = [r for r in rows if (r.order_id, r.tracking_number) not in known]
    if known:
        log.info("%s: %d package(s) already recorded there", group_key, len(known))

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
    if hasattr(client, "file_insurance"):
        # Only for shipments the group now has on file — insurance is filed against a known
        # shipment, so a row whose submission just failed must not be insured.
        blocked = {t for t, _ in push.failed} | {t for t, _ in push.needs_manual}
        insurable = [r for r in rows if r.tracking_number not in blocked]
        insurance = client.file_insurance(insurable)
        log.info("%s insurance: %s", group_key, insurance.summary())

    payouts = client.fetch_payouts([r.tracking_number for r in rows])
    _merge_writes(all_writes, allocate_payouts(
        payouts, plan["rows_by_tracking"], plan["costs_by_row"], plan["status_by_row"],
        plan["insurance_by_row"],
    ))
    log.info("%s: %d payout record(s) read back", group_key, len(payouts))

    _alert_on_cancelled_orders(group_key, client, plan, apply)

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
    address to warehouses.json, or set the row's Buying Group by hand, and the next run submits it.
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
        "Add the delivery address to warehouses.json (or set Buying Group on the row by hand) and "
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
    parser.add_argument("--void", nargs="+", metavar="TRACKING",
                        help="Void a BFMR insurance filing for these tracking numbers and exit")
    args = parser.parse_args()

    if args.void:
        client = get_client("BFMR", dry_run=not args.apply)
        result = client.void_insurance(args.void)
        print(f"Insurance void: {result.summary()}")
        return

    run(apply=args.apply, limit=args.limit, only_group=args.group)
    if not args.apply:
        print("\nDry run only — nothing sent, nothing written. Re-run with --apply to act.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    main()
