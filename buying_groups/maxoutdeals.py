"""MaxOutDeals (MOD) adapter.

Two endpoints, both POST, both requiring `user` (int) and `email` in the BODY as well as a bearer
token in the header:

    /p/it@api@order-management/cmd/addtracking      the push    limit 30 calls/day
    /p/it@api@received-items/cmd/search             the pull    limit 10 calls/day, <=10,000 rows

MOD ALSO ENFORCES AN IP ALLOWLIST. The machine running this must be added under the firewall tab in
the MOD profile, or every call is rejected no matter how valid the token is. That makes deployment a
real consideration rather than a detail: a new host, a changed home IP, or routing these calls
through a profile proxy (the way scrapers/costco_api.py does) all break it until the new address is
registered.

THE DAILY LIMITS SHAPE THE DESIGN. A 6-hourly schedule is 4 runs a day, so each endpoint gets
exactly ONE batched call per run. Both endpoints take arrays, so that is comfortable — but it rules
out any per-row retry, and it means an afternoon of manual testing spends the same budget as
production. `DailyCallBudget` refuses locally rather than letting a loop burn the allowance and
leave the next scheduled run silently unable to submit anything.

IDEMPOTENCY IS GUARANTEED IN WRITING, which is unusual and worth relying on. The addtracking docs
state: "All tracking numbers are validated. Any tracking numbers already in the system are ignored."
So MOD needs no dedupe read at all, and `already_submitted` deliberately returns an empty set rather
than spending one of the ten daily receipt searches to compute something the server already handles.

GRANULARITY DIFFERS FROM BFMR: MOD takes one object per TRACKING NUMBER, not per ledger row. A box
holding two distinct items is two ledger rows but one MOD object, so rows are grouped and their
costs summed — see `_group_by_tracking`.

THE PULL RETURNS CSV, not JSON (JSON appears only on error). **THE LIVE HEADER DIFFERS FROM THE
DOCUMENTED ONE** — confirmed 2026-08-12 via scripts/bg_probe.py. Documented vs actual:

    docs:   "VERIFIED","VOID","ID","RECEIPT ID","CREATED DATE","USER","ITEM","WAREHOUSE","QTY",
            "PRICE","TAX","COMMISSION","EXT TOTAL","STATUS","DELIVERY","TRACKING NUMBERS"
    actual: "VOID","VERIFIED","COMMITTED","ID","RECEIPT ID","ITEM","QTY","PRICE","TAX",
            "COMMISSION","TOTAL PRICE","EXT TOTAL","CREATED DATE","WAREHOUSE","STATUS",
            "DELIVERY","TRACKING NUMBERS"

VOID and VERIFIED are swapped, USER is gone, and COMMITTED / TOTAL PRICE are new. Parsing is
therefore by COLUMN NAME (`csv.DictReader`), never by position — with positional parsing this
reshuffle would have silently read the wrong numbers into the money columns.

`TRACKING NUMBERS` is the join key back to the ledger, but it is EXCEL-ESCAPED as `="529900000008"`
and must be unwrapped — see `_split_tracking_cell`.

MOD INSURANCE IS ALWAYS $0, so this adapter never files insurance and reports 0.

STATUS: **a package appearing in the received-items report IS the confirmation MOD has paid for it**,
so those rows are marked `paid`. There is nothing finer available — MOD exposes
no payment flag and every live row reads `STATUS=RECEIVING`.

**RETURNS ARE MANUAL FOR MOD.** MOD gives no return/rejection signal in either endpoint, and there
are no data points to reverse-engineer one from, so a returned MOD package will keep reading `paid`
until someone sets its Status to `return` on the sheet by hand. That hand-typed value is safe:
`return` outranks `paid` in `ledger_sync._STATUS_RANK`, so a later sync will not undo it. BFMR has no
such gap — it reports `returned` directly.

"""

import csv
import io
import logging

from buying_groups.base import (
    BuyingGroupError,
    DailyCallBudget,
    HttpClient,
    PayoutRecord,
    SubmissionResult,
    TrackingSubmission,
    parse_money,
)
from config.settings import settings

__all__ = ["MaxOutDealsClient"]

log = logging.getLogger(__name__)

ADD_TRACKING_PATH = "/p/it@api@order-management/cmd/addtracking"
RECEIVED_ITEMS_PATH = "/p/it@api@received-items/cmd/search"

#: MOD's published daily allowances. Local ceilings only — MOD remains the real authority.
ADD_TRACKING_DAILY_LIMIT = 30
RECEIVED_ITEMS_DAILY_LIMIT = 10

#: What MOD actually reimburses. CONFIRMED against 49 real receipts (2026-08-12, scripts/bg_probe.py):
#:
#:      TOTAL PRICE = PRICE + TAX + COMMISSION      (per unit)
#:      EXT TOTAL   = TOTAL PRICE * QTY             (the whole line — matched on 49/49 rows)
#:
#: So COMMISSION is only the per-unit MARGIN, not the payout. This distinction is the difference
#: between a right and a catastrophically wrong profit column: `_profit_formula` computes
#: `Payout Amount - Total Cost - ...`, so feeding it COMMISSION (6) instead of EXT TOTAL (905) for a
#: $899 PS5 would book a $893 LOSS on a profitable order — a plausible-looking number that nothing
#: downstream would ever flag.
_PAYOUT_COLUMN = "EXT TOTAL"

#: NB this is the RECEIPT date, not a payment date — MOD's report has no paid-date column and every
#: row in the capture read STATUS=RECEIVING. It is the best available approximation of "when this
#: package turned into money"; treat Payout Date as "date MOD took delivery" and correct it by hand
#: if their remittance date matters to you.
_PAYOUT_DATE_COLUMN = "CREATED DATE"

#: MOD insurance is always zero, so rows get a real 0 rather than a blank. A blank would leave the
#: Insurance cell untouched forever by _merge_row's blank-never-overwrites rule; 0 states the fact.
_MOD_INSURANCE = 0.0


class MaxOutDealsClient(HttpClient):
    group_key = "MOD"

    def __init__(self, *, dry_run: bool = True):
        super().__init__(settings.maxoutdeals_api_base_url, dry_run=dry_run)
        self.api_key = settings.maxoutdeals_api_key
        self.user_id = settings.maxoutdeals_user_id
        self.email = settings.maxoutdeals_email
        self.push_budget = DailyCallBudget(ADD_TRACKING_DAILY_LIMIT, "MOD addtracking")
        self.pull_budget = DailyCallBudget(RECEIVED_ITEMS_DAILY_LIMIT, "MOD received-items")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _check_configured(self) -> None:
        missing = [
            name for name, value in (
                ("MAXOUTDEALS_API_KEY", self.api_key),
                ("MAXOUTDEALS_USER_ID", self.user_id),
                ("MAXOUTDEALS_EMAIL", self.email),
            ) if not value
        ]
        if missing:
            raise BuyingGroupError(
                f"MaxOutDeals is not configured — set {', '.join(missing)} in .env. "
                "Also add this machine's IP under the firewall tab in your MOD profile."
            )

    def _body(self, **extra) -> dict:
        """Every MOD request carries the account identity in the body, not just the header."""
        return {"user": _as_int(self.user_id), "email": self.email, **extra}

    # --- pushing tracking numbers -----------------------------------------------------------

    def already_submitted(self, rows: list[TrackingSubmission]) -> set[tuple[str, str]]:
        """Always empty — MOD dedupes server-side and says so in its docs.

        Spending one of the ten daily receipt searches to pre-compute this would buy nothing: an
        already-known number is ignored by addtracking either way, and the receipts search only
        covers items MOD has physically RECEIVED, so it could not answer the question anyway.
        """
        return set()

    def submit_tracking(self, rows: list[TrackingSubmission]) -> SubmissionResult:
        """One batched call for every eligible row — the daily limit permits nothing else."""
        result = SubmissionResult()
        if not rows:
            return result

        groups = _group_by_tracking(rows)
        trackings = [_tracking_object(number, members) for number, members in groups.items()]

        self.push_budget.spend()
        response = self.request(
            "POST",
            ADD_TRACKING_PATH,
            mutating=True,
            json_body=self._body(trackings=trackings),
        )
        if response is None:  # dry run
            result.skipped.extend((obj["tracking"], "dry run") for obj in trackings)
            return result

        payload = self._as_json(response, ADD_TRACKING_PATH)
        if not payload.get("success", False):
            raise BuyingGroupError(
                f"MOD rejected the batch: {payload.get('response') or payload}"
            )

        failed = _failed_trackings(payload)
        for obj in trackings:
            number = obj["tracking"]
            if number in failed:
                result.failed.append((number, str(failed[number])))
            else:
                result.submitted.append(number)
                result.submitted_for.extend((member.order_id, number) for member in groups[number])

        # MOD reports how many rows it actually inserted; the rest were duplicates it ignored.
        # Recording that distinction keeps a run that legitimately changed nothing from reading as
        # a run that submitted everything afresh.
        affected = _as_int(payload.get("affected"), default=len(result.submitted))
        if affected < len(result.submitted):
            log.info(
                "MOD accepted %d of %d tracking numbers as new; the rest were already in their "
                "system and were ignored.", affected, len(result.submitted),
            )
        return result

    # --- payouts ----------------------------------------------------------------------------

    def fetch_payouts(self, tracking_numbers: list[str]) -> list[PayoutRecord]:
        """Received items for these tracking numbers, parsed out of MOD's CSV report."""
        if not tracking_numbers:
            return []

        self.pull_budget.spend()
        response = self.request(
            "POST",
            RECEIVED_ITEMS_PATH,
            mutating=False,
            json_body=self._body(trackings=list(tracking_numbers)),
        )
        return parse_received_items_csv(response.text)


# --- pure helpers (unit-tested without a network) -----------------------------------------------


def _group_by_tracking(rows: list[TrackingSubmission]) -> dict[str, list[TrackingSubmission]]:
    """Collapse ledger rows onto tracking numbers, preserving order.

    The ledger is one row per (shipment x item), but MOD wants one object per physical package. A
    box with two distinct items must therefore become ONE object whose amount is the sum of both
    rows — sending two objects with the same tracking number would have the second silently ignored
    as a duplicate, under-reporting the box's value.
    """
    groups: dict[str, list[TrackingSubmission]] = {}
    for row in rows:
        groups.setdefault(row.tracking_number, []).append(row)
    return groups


def _tracking_object(number: str, members: list[TrackingSubmission]) -> dict:
    """One MOD `trackings[]` entry for a physical package.

    `amount` is the summed cost of everything in the box; `order` is the retailer order number
    (identical across members, since a package cannot span two orders); `notes` mirrors the shape
    MOD's own example uses ("2-Nintendo-switches") so the dashboard reads naturally.

    **ONLY `tracking` MATTERS TO MOD**. They run their own cost calculator in the
    receiving tab, and that — surfaced as `EXT TOTAL` in the received-items report — is what they
    actually reimburse. Everything else here is for the user's own reference in the dashboard.

    That is worth stating because of what it means for a CORRECTED cost. `addtracking` ignores a
    tracking number already in the system and MOD has no update endpoint, so an `amount` can never be
    revised after the fact: a discount netted late, a price adjustment, a partial cancel or a
    ship-and-split re-cost all leave MOD's copy stale forever. Since MOD doesn't read the field, that
    is cosmetic and needs no detection — do not add drift alerting for it.
    """
    amount = sum(m.total_cost for m in members if m.total_cost is not None)
    return {
        "tracking": number,
        "order": members[0].order_id,
        "amount": round(amount, 2),
        "notes": ", ".join(f"{m.quantity}-{m.item_name}" for m in members)[:250],
    }


def _failed_trackings(payload: dict) -> dict[str, object]:
    """Pull per-object errors out of the response.

    The docs mention an `errors` key without specifying its shape ("not shown"), so both a mapping
    keyed by tracking number and a list of objects carrying one are accepted. Anything unrecognised
    is ignored rather than guessed at — a mis-parsed error would mark a good row as failed.
    """
    errors = payload.get("errors")
    if isinstance(errors, dict):
        return {str(k): v for k, v in errors.items()}
    if isinstance(errors, list):
        found = {}
        for item in errors:
            if isinstance(item, dict) and item.get("tracking"):
                found[str(item["tracking"])] = item
        return found
    return {}


def parse_received_items_csv(text: str) -> list[PayoutRecord]:
    """Parse MOD's received-items CSV into payout records, one per tracking number.

    MOD answers with CSV on success and JSON on failure, so a body that isn't CSV is an error to
    surface, not an empty result to shrug at — an empty list would read as "nothing received yet"
    and quietly stall every payout.

    A receipt row can list several tracking numbers in one cell, and several rows can share one
    tracking number (a box holding two line items). Payouts are therefore SUMMED per tracking
    number, matching how `_tracking_object` sums cost on the way out.
    """
    stripped = (text or "").strip()
    if not stripped:
        return []
    if stripped.startswith("{") or stripped.startswith("["):
        raise BuyingGroupError(f"MOD returned an error instead of a CSV report: {stripped[:300]}")

    reader = csv.DictReader(io.StringIO(stripped))
    if not reader.fieldnames or "TRACKING NUMBERS" not in reader.fieldnames:
        raise BuyingGroupError(
            f"MOD's CSV is missing the TRACKING NUMBERS column; got {reader.fieldnames}"
        )

    totals: dict[str, float] = {}
    dates: dict[str, str] = {}
    for row in reader:
        if _is_truthy(row.get("VOID")):
            continue
        amount = parse_money(row.get(_PAYOUT_COLUMN))
        date = _iso_date(row.get(_PAYOUT_DATE_COLUMN))
        for number in _split_tracking_cell(row.get("TRACKING NUMBERS")):
            totals[number] = totals.get(number, 0.0) + (amount or 0.0)
            if date and not dates.get(number):
                dates[number] = date

    return [
        PayoutRecord(
            tracking_number=number,
            payout_amount=round(total, 2),
            payout_date=dates.get(number, ""),
            insurance=_MOD_INSURANCE,
            # APPEARING IN THIS REPORT AT ALL IS THE PAYMENT SIGNAL. MOD has no
            # "paid" flag and every live row reads STATUS=RECEIVING, so there is nothing finer to
            # key on: a package MOD has received and reported is a package MOD has paid for.
            # RETURNS HAVE NO SIGNAL HERE — see the module docstring; mark those by hand.
            status="paid",
        )
        for number, total in totals.items()
    ]


def _split_tracking_cell(value) -> list[str]:
    """Read a TRACKING NUMBERS cell into bare numbers.

    MOD EXCEL-ESCAPES this column: the cell literally contains `="529900000008"`, a spreadsheet
    formula wrapper that stops Excel rendering a long digit string in scientific notation. Left in,
    the value matches NO ledger row, and since the tracking number is the ONLY join key between MOD
    and the ledger, every single payout would silently fail to land — a total no-op that looks
    exactly like "MOD hasn't paid anything yet". Found by scripts/bg_probe.py against real data;
    it is not mentioned anywhere in MOD's documentation.

    One cell can also hold several numbers, and MOD doesn't state the separator, so the usual
    candidates are all accepted.
    """
    if not value:
        return []
    parts = str(value).replace(";", ",").replace("\n", ",").split(",")
    return [cleaned for cleaned in (_unescape_excel(p) for p in parts) if cleaned]


def _unescape_excel(text: str) -> str:
    """`="529900000008"` -> `529900000008`. A plain value passes through untouched."""
    cleaned = str(text).strip()
    if cleaned.startswith("="):
        cleaned = cleaned[1:].strip()
    return cleaned.strip('"').strip()


def _is_truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "void"}


def _iso_date(value) -> str:
    """MOD dates arrive as 'YYYY-MM-DD HH:MM:SS'; the ledger's date columns are plain ISO text."""
    text = str(value or "").strip()
    return text.split(" ")[0] if text else ""


def _as_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


