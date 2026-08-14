"""BFMR (BuyForMeRetail) adapter, written against their published Swagger 2.0 spec.

Spec: https://api.bfmr.com/storage/api-docs.json  (https://api.bfmr.com/ just renders it in ReDoc.)

AUTH IS TWO HEADERS, NOT A BEARER TOKEN: `API-KEY` and `API-SECRET`, both from Developer Tools in
your BFMR account settings.

THE DATA MODEL IS A THREE-STAGE CHAIN, and it does not key on your retailer's order number:

    reserve_id  ->  purchase_id  ->  shipment_id

`order_no` (the retailer's order id, which is what our ledger stores) is a user-supplied ATTRIBUTE
of a purchase, not a key. So we cannot address a BFMR record by ledger data alone — we have to read
`my-tracker` first and match. Per POST /api/v2/my-tracker's own description:

    create a Purchase:  reserve_id, order_no, qty            (purchase_id/shipment_id/tracking null)
    create a Shipment:  reserve_id, purchase_id, order_no, tracking_number, qty   (shipment_id null)
    update a Shipment:  reserve_id, purchase_id, shipment_id, tracking_number, qty

and at every stage, **qty can only ever be REDUCED**.

WHAT THIS ADAPTER DELIBERATELY DOES NOT DO: create purchases. Choosing the right `reserve_id` means
matching a reservation (described by item name / model / UPC / retailer) against a ledger row that
carries only a free-text item name and an order URL — no SKU or ASIN. A wrong `reserve_id` books the
wrong deal, and since qty only reduces, it is not cheaply undone. So v1 attaches tracking numbers to
purchases that already exist, and a row whose order BFMR has never heard of is reported, not guessed
at.

SPLITS ARE THE HARD CASE, and `shipment_id: null` meaning CREATE is why. Blind-sending null for a
tracking number BFMR already holds would either duplicate the shipment or be rejected, so the
`my-tracker` read is load-bearing rather than an optimisation: a known tracking number is sent as an
UPDATE carrying its `shipment_id`, and only a genuinely new one is sent as a create. When an order
splits, the original shipment's quantity DROPS (a qty-3 box becoming 2+1), so reductions are sent
before creates within an order — otherwise the order's shipment quantities transiently exceed the
purchase. A quantity that would have to INCREASE is reported as a failure rather than silently
dropped: BFMR would reject it anyway, and it means our ledger and their record genuinely disagree.

IDEMPOTENCY EVIDENCE (required by the plan before this may run outside dry-run): NOT YET GATHERED.
`GET /api/v2/shipments/status?tracking_number=` is the documented per-number check and is what
`already_submitted` uses, but whether re-sending a create for a known number is a no-op or a
duplicate has not been observed live. Until `scripts/bg_probe.py` answers it, treat a non-dry-run
BFMR push as a validation event.
"""

import logging

from buying_groups.base import (
    BuyingGroupError,
    HttpClient,
    PayoutRecord,
    SubmissionResult,
    TrackingSubmission,
    parse_money,
)
from config.settings import settings

__all__ = ["BFMRClient"]

log = logging.getLogger(__name__)

#: POST /api/v2/my-tracker documents "Max 500 objects per request".
MAX_TRACKER_OBJECTS = 500

#: BFMR's own `status` -> the ledger's Status vocabulary. Observed live: cancelled, paid, processed,
#: returned, shipped.
#:
#: Only the two OUTCOMES map. `paid` and `returned` describe what the buying group did, which no
#: retailer scrape can ever know, so BFMR is the authority on them. `shipped`/`processed`/`cancelled`
#: map to "" ON PURPOSE — those describe the package's journey, the retailer scrape already tracks it
#: far more precisely, and letting BFMR write them would have the two sources overwriting each other
#: every run. (BFMR "cancelled" also means *the purchase* was cancelled, which is a different fact
#: from the retailer cancelling the order.)
LEDGER_STATUS_BY_BFMR_STATUS = {
    "paid": "paid",
    "returned": "return",  # the ledger spells it "return"
}

#: BFMR's handling of the **Best Buy duplicate tracking issue**:
#: https://support.bfmr.com/hc/en-us/articles/50968170907547
#:
#: Best Buy sometimes issues the SAME tracking number for two different orders. BFMR's tracker
#: enforces uniqueness, so the second one used to be rejected as invalid, and their instruction was
#: to "resubmit the tracking number and add a letter ("B", "C", or "D") to the very end of the number
#: until the system accepts it".
#:
#: **AS OF 2026-08-13 BFMR DOES THE SUFFIXING ITSELF.** Submitting a number another earner already
#: used now triggers a Best Buy check, BFMR appends the letter, emails a follow-up on receipt, and
#: the suffixed number can be insured. So the suffix is no longer ours to CHOOSE — only ours to
#: RECOGNISE, which is what everything below does. That makes this set more load-bearing than before,
#: not less: it is now the ONLY thing standing between us and treating BFMR's own success as a
#: failure, since every spelling that reaches us is one BFMR invented rather than one we sent.
#:
#: The consequence for us is a JOIN failure, and a silent one. The retailer — and therefore the
#: ledger — only ever knows the bare number; BFMR stores the suffixed one. Compared literally, the
#: package looks absent from BFMR in all four places we ask about it: it reads as never submitted
#: (so we re-submit and BFMR rejects it), as having no existing shipment (so we send a CREATE where
#: an UPDATE was needed), as having no payout (so the money never reaches the row), and as
#: uninsured (so we file insurance a second time on an already-insured package, spending a real
#: premium). Live example: ledger `529900000009` vs BFMR `529900000009B`.
#:
#: THE WHOLE ALPHABET, not just the B/C/D their article names. Their three are an
#: example, not a limit — a carton can hold more orders than that, and the cost of the two choices is
#: wildly asymmetric. Recognising a suffix we didn't need costs nothing; failing to recognise one
#: loses the package at all four join points, silently. Matching a letter we never suggest is
#: therefore the cheap side of the trade.
BFMR_DUPLICATE_SUFFIXES = tuple(chr(c) for c in range(ord("A"), ord("Z") + 1))


def bfmr_spellings(tracking_number: str) -> list[str]:
    """Every spelling BFMR might hold for one ledger tracking number, bare form FIRST.

    Deliberately one-directional: we EXTEND our own number rather than stripping a trailing letter
    off BFMR's. Stripping would be unsafe — it would merge two genuinely different packages onto one
    row. Extending cannot collide with a real tracking number from these carriers: UPS `1Z…` ends in
    an 8-digit package number, Amazon `TBA…` in digits, and Best Buy's are numeric throughout, so
    "some real number plus a trailing letter" is not a valid number in any of them.
    """
    return [tracking_number, *(tracking_number + s for s in BFMR_DUPLICATE_SUFFIXES)]


def _spelling_lookup(tracking_numbers) -> dict[str, str]:
    """{any spelling BFMR might use -> the ledger's spelling}, so joins survive the suffix.

    EXACT MATCHES ARE CLAIMED FIRST, and that ordering is load-bearing now that the suffix set is the
    whole alphabet. If the ledger holds both `T` and `T-plus-a-letter` as two distinct rows — which
    happens the moment someone records the suffixed number by hand — then `T`'s variants would
    otherwise swallow the second row's own number and attribute its payout to the first.
    """
    numbers = list(tracking_numbers)
    lookup: dict[str, str] = {number: number for number in numbers}
    for number in numbers:
        for spelling in bfmr_spellings(number)[1:]:
            lookup.setdefault(spelling, number)
    return lookup


class BFMRClient(HttpClient):
    group_key = "BFMR"

    def __init__(self, *, dry_run: bool = True):
        super().__init__(settings.bfmr_api_base_url, dry_run=dry_run)
        self.api_key = settings.bfmr_api_key
        self.api_secret = settings.bfmr_api_secret
        self.min_insurance_value = settings.bfmr_min_insurance_value

    def _headers(self) -> dict[str, str]:
        return {
            "API-KEY": self.api_key,
            "API-SECRET": self.api_secret,
            "Accept": "application/json",
        }

    def _check_configured(self) -> None:
        if not self.api_key or not self.api_secret:
            raise BuyingGroupError(
                "BFMR is not configured — set BFMR_API_KEY and BFMR_API_SECRET in .env "
                "(both come from Developer Tools in your BFMR account settings)."
            )

    # --- reading BFMR's state ---------------------------------------------------------------

    def fetch_tracker(self, page_size: int = 200) -> list[dict]:
        """Every row of My Tracker: reservations, purchases and shipments, with payouts.

        Paginated defensively. The spec documents `page_size`/`page_no` but not a total or a
        last-page flag, so the loop stops on a short page — which also terminates correctly if the
        API ignores paging entirely and returns everything at once.
        """
        rows: list[dict] = []
        page = 1
        while True:
            payload = self.get_json(
                "/api/v2/my-tracker", params={"page_size": page_size, "page_no": page}
            )
            batch = payload.get("my_tracker") or []
            rows.extend(batch)
            if len(batch) < page_size:
                return rows
            page += 1

    def already_submitted(self, rows: list[TrackingSubmission]) -> set[tuple[str, str]]:
        """Which `(order_id, tracking_number)` pairs BFMR holds — from ONE My Tracker read.

        The published spec documents no tracking number on the `my-tracker` response, so this
        originally fell back to one `shipments/status` call per candidate. The live probe showed the
        field is simply undocumented: `tracking_number` is present on 65 of 76 real rows. Reading the
        tracker once is both cheaper and more useful, since `submit_tracking` needs the very same
        payload to decide create-vs-update.

        THE ORDER ID IS PART OF THE KEY because Best Buy combines several ORDERS into one box. Two
        ledger rows then share a tracking number while still needing two separate BFMR shipments (the
        second carrying a B/C/D suffix). Matching on the tracking number alone would let order A's
        submission mark order B as already done, and order B would never be submitted or reimbursed.

        A tracker row with no `order_id` cannot be attributed to an order, so it contributes nothing
        here — deliberately. The cost of ignoring it is one redundant submission attempt, which the
        post-submit verification catches; the cost of trusting it would be a silently skipped package.
        """
        lookup = _spelling_lookup(r.tracking_number for r in rows)
        held: set[tuple[str, str]] = set()
        for entry in self.fetch_tracker():
            if _is_insurance_fee_row(entry):
                continue
            number = lookup.get(_tracking_of(entry))
            order_id = _order_id_of(entry)
            if number and order_id:
                held.add((order_id, number))
        return held

    def active_purchases_for(self, order_ids) -> set[str]:
        """Which of these orders BFMR still holds an ACTIVE (non-cancelled) purchase for.

        Used to spot the divergence that costs a deal: the retailer cancelled the order, so the
        ledger says `cancelled`, but BFMR still has the purchase open against the reservation.
        """
        wanted = {str(o).strip() for o in order_ids if str(o).strip()}
        return {
            order_id for entry in self.fetch_tracker()
            if (order_id := _order_id_of(entry)) in wanted
            and entry.get("purchase_id")
            and not _is_cancelled_purchase(entry)
            and not _is_insurance_fee_row(entry)
        }

    def active_reservations(self) -> list[dict]:
        """Reservations not yet turned into purchases.

        **`reservation_list` IS NOT ALWAYS A LIST.** When there are none, BFMR returns the STRING
        `"No reservations available"` in the same field. `len()` of that is 25 and iterating it
        yields characters, so `scripts/bg_probe.py` cheerfully reported "active reservations: 25"
        against an account that had zero — a wrong number that looked entirely plausible. Anything
        that isn't a list is normalised to `[]` here so no caller can inherit that.
        """
        payload = self.get_json("/api/v2/deal/reservations/active")
        listing = payload.get("reservation_list")
        if not isinstance(listing, list):
            if listing:
                log.info("BFMR reports no active reservations (%r)", listing)
            return []
        return listing

    def shipment_status(self, tracking_number: str) -> dict:
        """One ad-hoc lookup. A 404 means "BFMR has never heard of this", not a failure.

        NB the live response nests `tracker_data` as an OBJECT (`{"tracking_number":…, "status":
        "pending"}`), where the spec draws an ARRAY. Callers should not index it.
        """
        try:
            payload = self.get_json(
                "/api/v2/shipments/status", params={"tracking_number": tracking_number}
            )
        except BuyingGroupError as exc:
            if "404" in str(exc):
                return {}
            raise
        return payload.get("tracker_data") or {}

    # --- pushing tracking numbers -----------------------------------------------------------

    def submit_tracking(self, rows: list[TrackingSubmission]) -> SubmissionResult:
        """Attach each row's tracking number to the BFMR shipment it belongs to.

        Rows whose order BFMR has no purchase for are reported as failures rather than being turned
        into purchases — see the module docstring for why creating a purchase is not v1's job.
        """
        result = SubmissionResult()
        if not rows:
            return result

        tracker = self.fetch_tracker()
        purchases = _index_purchases_by_order(tracker)
        shipments = _index_shipments(tracker)

        objects: list[dict] = []
        for row in rows:
            purchase = purchases.get(row.order_id)
            if purchase is None:
                result.needs_manual.append((row.tracking_number, _no_purchase_hint(row)))
                continue
            if _is_cancelled_purchase(purchase):
                result.needs_manual.append((row.tracking_number, _cancelled_purchase_hint(row)))
                continue

            existing = _find_shipment(shipments, row.order_id, row.tracking_number)
            if existing is not None and _as_int(existing.get("qty")) == row.quantity:
                result.skipped.append((row.tracking_number, "already recorded with this quantity"))
                continue

            if existing is not None and _as_int(existing.get("qty")) < row.quantity:
                result.failed.append((
                    row.tracking_number,
                    f"{row.describe()}: BFMR records qty {existing.get('qty')} but the ledger says "
                    f"{row.quantity}, and BFMR only allows a quantity to be REDUCED. Fix whichever "
                    f"side is wrong by hand.",
                ))
                continue

            objects.append(_tracker_object(row, purchase, existing))

        for batch in _batched(_reductions_first(objects), MAX_TRACKER_OBJECTS):
            self._post_tracker_batch(batch, result)
        return result

    def _post_tracker_batch(self, batch: list[dict], result: SubmissionResult) -> None:
        response = self.request(
            "POST", "/api/v2/my-tracker", mutating=True, json_body={"tracker_data": batch}
        )
        if response is None:  # dry run — the request was logged, not sent
            result.skipped.extend((obj["tracking_number"], "dry run") for obj in batch)
            return

        payload = self._as_json(response, "/api/v2/my-tracker")
        reservations = payload.get("reservations_response") or {}
        invalid = reservations.get("invalid_items") or []
        invalid_numbers = {
            item.get("tracking_number") for item in invalid if isinstance(item, dict)
            if item.get("tracking_number")
        }

        # DON'T TRUST A QUIET RESPONSE. A submission of a Best Buy duplicate tracking number came
        # back with an empty `invalid_items` and was reported as "1 submitted" — while My Tracker's
        # row count did not move, i.e. BFMR had silently dropped it. Trusting the response shape
        # meant reporting success for a package that had, in fact, never been handed over: the
        # quietest possible way to lose a reimbursement.
        #
        # So success is confirmed by RE-READING the tracker and checking the number is actually
        # there. That is shape-independent, which matters for an undocumented response, and it costs
        # one GET per batch.
        landed = {_tracking_of(entry) for entry in self.fetch_tracker()}
        for obj in batch:
            number = obj["tracking_number"]
            if number in invalid_numbers:
                result.failed.append((number, f"BFMR rejected it: {invalid}"))
            elif any(spelling in landed for spelling in bfmr_spellings(number)):
                # ANY SPELLING COUNTS, not just the one we sent. BFMR now appends the duplicate
                # letter ITSELF (their 2026-08-13 change): submitting a number another earner already
                # used triggers a Best Buy check, and the shipment is recorded as "…B". Testing the
                # bare number alone would therefore read BFMR's own success as a silent drop —
                # alerting the user to do three obsolete manual steps, and worse, marking the package
                # `needs_manual`, which excludes it from `file_insurance` (see sync_tracking's
                # `blocked` set). That would skip insuring precisely the cartons BFMR just announced
                # you CAN insure. The ledger's own spelling is what gets recorded as submitted, since
                # that is the key the checkbox and every later join use.
                result.submitted.append(number)
            else:
                # Landed under NO spelling at all. Since BFMR handles the suffixing itself, this is
                # no longer the routine combined-carton case — it is either their Best Buy check
                # still running, or a genuine silent drop. Reported as needing a human rather than as
                # a failure, because no retry of ours distinguishes the two.
                #
                # ALERTED IMMEDIATELY, not once the package is delivered. Their check being
                # asynchronous means this can occasionally clear itself on the next run — but MOST
                # BUYING GROUPS ONLY INSURE A PACKAGE WHOSE TRACKING NUMBER ARRIVED BEFORE DELIVERY,
                #so waiting for certainty would forfeit exactly the cover this
                # is meant to protect. A false alarm costs one glance at My Tracker.
                result.needs_manual.append((number, _duplicate_tracking_hint(number)))

    # --- payouts and insurance --------------------------------------------------------------

    def fetch_payouts(self, tracking_numbers: list[str]) -> list[PayoutRecord]:
        """Payout, insurance premium and outcome per tracking number, from My Tracker.

        **EACH PACKAGE HAS TWO TRACKER ROWS**, and they mean different things:

          - the DEAL row — `order_id` and `deal_title` set, positive `amount_paid`: the gross payout;
          - the FEE row  — no `order_id`, `retail_price` 0, a small NEGATIVE amount: **the insurance
            premium**, ~0.47% of the deal with a ~$2.00 floor.

        They are split rather than summed. Summing nets to the right bottom line — and did — but it
        made a real cost invisible: the sheet would book $2,199.80 with no hint that $10.20 of
        insurance had been deducted from a $2,210 payout. Splitting puts the gross in Payout Amount
        and the premium in Insurance, which the Total Profit formula already subtracts, so the
        arithmetic is unchanged and the deduction is finally legible.

        `status` is BFMR's own view of the package, and the two values the retailer can never
        produce — `paid` and `returned` — are exactly what the ledger's statuses record. Anything
        else (`shipped`, `processed`, `cancelled`) is deliberately mapped to "" and left alone: the
        retailer scrape owns those, and letting BFMR write them would fight it.
        """
        lookup = _spelling_lookup(tracking_numbers)

        records: list[PayoutRecord] = []
        for entry in self.fetch_tracker():
            spelling = _tracking_of(entry)
            number = lookup.get(spelling, "")
            if not number:
                continue

            if _is_insurance_fee_row(entry):
                # `total_payout`, not `amount_paid`: the premium is committed when the package is
                # insured, whereas `amount_paid` stays "0.00" until BFMR settles the whole package.
                # Recording it early costs nothing — Total Profit stays blank until Payout Amount
                # lands anyway — and it means the cost is visible while the package is still open.
                premium = parse_money(entry.get("total_payout"))
                records.append(PayoutRecord(
                    tracking_number=number,
                    insurance=abs(premium) if premium is not None else None,
                ))
                continue

            status = LEDGER_STATUS_BY_BFMR_STATUS.get(str(entry.get("status") or "").lower(), "")
            paid = status == "paid"
            records.append(PayoutRecord(
                tracking_number=number,
                # `amount_paid`, not `total_payout`: the latter is what the deal is WORTH and is
                # populated from the moment a purchase exists, so writing it would fill Payout Amount
                # — and light up Total Profit — for money that has not arrived. `amount_paid` reads
                # "0.00" until BFMR actually pays. And only when BFMR says paid at all: a `returned`
                # package's figures are ambiguous, and a blank cell correctly reads "not paid out"
                # where a wrong number would quietly overstate profit.
                payout_amount=parse_money(entry.get("amount_paid")) if paid else None,
                payout_date=_bfmr_date_to_iso(entry.get("date_paid")) if paid else "",
                insurance=None,
                order_id=_order_id_of(entry),
                status=status,
            ))
        return records

    def insured_tracking_numbers(self) -> dict[str, str]:
        """`{tracking_number: insurance_status}` from My Tracker — the ONLY working insurance read.

        BOTH documented insurance-read endpoints 404 against the live API (verified 2026-08-13):

            GET /api/v2/insurance/shipments            -> 404 "route could not be found"
            GET /api/v2/shipment/insured/{tracking}    -> 404 "route could not be found"

        That mattered far more than a missing feature. `file_insurance` used the first of those as
        its never-double-file guard, and a guard that raises on every run would have made an
        automatic filing pass file the SAME shipment again on every scheduled run — paying a real
        premium each time. My Tracker's own `insurance_status` field replaces it, observed as
        `insured` or `not_eligible`.
        """
        return {
            number: str(entry.get("insurance_status") or "")
            for entry in self.fetch_tracker()
            if (number := _tracking_of(entry))
        }

    def file_insurance(self, rows: list[TrackingSubmission]) -> SubmissionResult:
        """File insurance for shipments that don't already have it.

        THIS SPENDS REAL MONEY on an unattended schedule, so four things are deliberate:

        - `package_value` is NOT sent. BFMR derives it from the shipment items it already holds,
          which removes any chance of over-declaring and overpaying a premium we invented.
        - Already-insured numbers are filtered out first, from My Tracker's `insurance_status`, so a
          re-run can never double-file. (The endpoint originally used for this 404s — see
          `insured_tracking_numbers` for why that was dangerous rather than merely broken.)
        - **`not_eligible` is skipped.** BFMR reports this on 73 of 76 real rows, so in practice
          almost nothing is filable and this pass is close to a no-op. Trying anyway would burn a
          call per row to earn an error.
        - `bfmr_min_insurance_value` gates the rest. The default is 0 — insure everything.

        NB `POST /api/v2/insurance/file` itself is UNVERIFIED: the sibling read routes are absent, so
        it may well 404 too. A 404 is harmless here (it raises, nothing is charged), but it does mean
        a successful filing has never actually been observed.
        """
        result = SubmissionResult()
        if not rows:
            return result

        insurance_status = self.insured_tracking_numbers()
        for row in rows:
            state = _lookup_any_spelling(insurance_status, row.tracking_number, "")
            # File against the spelling BFMR actually HOLDS. Sending the ledger's bare number for a
            # package they filed under "…B" posts against a shipment they have no record of: a 2xx
            # that matches nothing, reported as success, leaving the package uninsured. Observed
            # live on 529900000009 (BFMR holds 529900000009B).
            spelling = _held_spelling(insurance_status, row.tracking_number)
            if state == "insured":
                result.skipped.append((row.tracking_number, "already insured"))
                continue
            if state == "not_eligible":
                result.skipped.append((row.tracking_number, "BFMR reports it as not eligible"))
                continue
            if (row.total_cost or 0) < self.min_insurance_value:
                result.skipped.append((
                    row.tracking_number,
                    f"below the BFMR_MIN_INSURANCE_VALUE threshold of {self.min_insurance_value}",
                ))
                continue
            response = self.request(
                "POST",
                "/api/v2/insurance/file",
                mutating=True,
                data={"tracking_number": spelling},
            )
            if response is None:
                result.skipped.append((row.tracking_number, "dry run"))
            else:
                result.submitted.append(row.tracking_number)
        return result

    def void_insurance(self, tracking_numbers: list[str]) -> SubmissionResult:
        """Undo a filing. Exists from day one so a mistaken automatic run is reversible.

        Resolves each number to the spelling BFMR holds first, for the same reason `file_insurance`
        does — voiding against a number they have no record of would silently do nothing, and a void
        that quietly fails is worse than one that errors.
        """
        result = SubmissionResult()
        held = self.insured_tracking_numbers()
        for number in tracking_numbers:
            spelling = _held_spelling(held, number)
            if spelling != number:
                log.info("BFMR holds %s as %s; voiding that", number, spelling)
            response = self.request(
                "POST", "/api/v2/insurance/void", mutating=True, data={"tracking_number": spelling}
            )
            if response is None:
                result.skipped.append((number, "dry run"))
            else:
                result.submitted.append(number)
        return result

    def deadline_warnings(self, within_seconds: int = 48 * 3600) -> list[dict]:
        """Purchases whose tracking deadline is close and which have no tracking number yet.

        BFMR CANCELS a purchase whose tracking arrives late, and the box that ships late is exactly
        the second half of a split. This read is free once My Tracker is being fetched anyway, and
        it protects money that would otherwise vanish with no error anywhere.
        """
        warnings = []
        for entry in self.fetch_tracker():
            if _tracking_of(entry):
                continue
            remaining = parse_money(entry.get("order_deadline"))
            if remaining is not None and 0 < remaining <= within_seconds:
                warnings.append(entry)
        return warnings


# --- pure helpers (unit-tested without a network) -----------------------------------------------


def _tracker_object(row: TrackingSubmission, purchase: dict, existing: dict | None) -> dict:
    """Build one `tracker_data` object: an UPDATE if BFMR already knows the number, else a CREATE.

    `shipment_id` is the switch — null means create. Getting this wrong on a split would duplicate
    a shipment rather than adjust one, which is why the existing-shipment lookup is required and
    not merely an optimisation.
    """
    return {
        "reserve_id": purchase.get("reserve_id"),
        "purchase_id": purchase.get("purchase_id"),
        "shipment_id": existing.get("shipment_id") if existing else None,
        "order_no": row.order_id,
        # Echo back the spelling BFMR already holds. If their record is the suffixed
        # "529900000009B", sending the bare number would read as a different package and undo the
        # very de-duplication the suffix exists to provide.
        "tracking_number": _tracking_of(existing) if existing else row.tracking_number,
        "qty": row.quantity,
        # carried for ordering and error messages only; stripped before sending
        "_is_reduction": bool(existing) and _as_int(existing.get("qty")) > row.quantity,
    }


def _reductions_first(objects: list[dict]) -> list[dict]:
    """Order a batch so quantity reductions precede new shipments, and drop the private marker.

    When an order splits, the original shipment's quantity falls (3 -> 2) while a new shipment
    appears (1). Applying the create first would momentarily claim 3 + 1 = 4 units against a
    3-unit purchase. Sorting is stable, so rows otherwise keep their planner order.
    """
    ordered = sorted(objects, key=lambda o: not o.get("_is_reduction"))
    return [{k: v for k, v in obj.items() if not k.startswith("_")} for obj in ordered]


def _batched(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _index_purchases_by_order(tracker: list[dict]) -> dict[str, dict]:
    """Map the retailer order number -> the BFMR purchase recorded against it.

    Only rows that reached the Purchase stage qualify; a bare reservation has no `purchase_id` and
    cannot receive a shipment.

    AN ACTIVE PURCHASE ALWAYS WINS over a cancelled one for the same order. A cancelled purchase is
    still indexed when it is the only one, so the caller can say *that* rather than guess: BFMR halts
    on an inactive purchase, so submitting against one fails in exactly the same
    accepted-but-absent way a dropped submission does — and reporting a cancellation as a
    combined-carton problem would send someone hunting for a suffixed shipment against a reservation
    that no longer exists.
    """
    index: dict[str, dict] = {}
    for entry in tracker:
        order_id = _order_id_of(entry)
        if not order_id or not entry.get("purchase_id"):
            continue
        existing = index.get(order_id)
        if existing is None or (
            _is_cancelled_purchase(existing) and not _is_cancelled_purchase(entry)
        ):
            index[order_id] = entry
    return index


def _is_cancelled_purchase(entry: dict) -> bool:
    return str(entry.get("status") or "").lower() == "cancelled"


def _no_purchase_hint(row) -> str:
    """No purchase at all for this order.

    Worth stating what that IMPLIES rather than just the fact. A reservation only stays valid if its
    order number is submitted right after ordering, so by the time a package has shipped its purchase
    should already exist — a missing one means the reservation lapsed or the number never went in,
    not that this tool skipped a step.
    """
    return (
        f"{row.tracking_number}: BFMR has no purchase recorded for order {row.order_id}, so there "
        f"is nothing to attach the tracking to.\n"
        f"\n"
        f"A reservation only stays valid if its order number is submitted right after ordering, so a "
        f"SHIPPED package with no purchase usually means the reservation LAPSED, or the order number "
        f"was never entered. This tool does not create purchases: picking the right reservation "
        f"means matching on item name alone, and a wrong pick books the wrong deal.\n"
        f"\n"
        f"Check My Tracker for order {row.order_id}. If the reservation is still live, enter the "
        f"order number and the tracking number by hand — the next run picks it up with no sheet edit."
    )


def _cancelled_purchase_hint(row) -> str:
    """The purchase exists but BFMR cancelled it, so tracking cannot be attached to it."""
    return (
        f"{row.tracking_number}: BFMR has CANCELLED the purchase for order {row.order_id}. Their own "
        f"docs say the process halts on a purchase that is not active, so the tracking cannot be "
        f"attached and this package will not be paid.\n"
        f"\n"
        f"The usual cause is tracking arriving after BFMR's deadline. This is NOT the Best Buy "
        f"combined-package case, which BFMR now resolves itself by appending a letter — the problem "
        f"here is the reservation rather than the number, so no spelling of it will be accepted.\n"
        f"\n"
        f"Check My Tracker for order {row.order_id}. If the package is genuinely on its way, raise a "
        f"BFMR support ticket with proof of purchase and ask them to reinstate it."
    )


def _is_insurance_fee_row(entry: dict) -> bool:
    """Is this the insurance-premium line rather than the deal itself?

    Three signals together, not one: a NEGATIVE amount (a real payout never is), no `order_id`, and
    no `deal_title`. Requiring all three keeps a genuinely negative deal — a chargeback, say — from
    being silently reclassified as insurance and quietly removed from the payout column.
    """
    amount = parse_money(entry.get("total_payout"))
    return (
        amount is not None
        and amount < 0
        and not _order_id_of(entry)
        and not entry.get("deal_title")
    )


def _duplicate_tracking_hint(tracking_number: str) -> str:
    """The message for a submission BFMR accepted the request for but recorded under NO spelling.

    This used to be the routine Best Buy combined-carton case, and it told the user to append letters
    by hand, file insurance by hand, and raise a support ticket. **BFMR AUTOMATED ALL THREE on
    2026-08-13**: a tracking number another earner already used now triggers a Best Buy check, BFMR
    appends the letter itself, emails a follow-up on receipt, and the suffixed number can be insured.
    `_post_tracker_batch` recognises any of those spellings as landed, so a carton no longer reaches
    this function at all.

    What's left is the residue, and it is genuinely ambiguous: either their Best Buy check hasn't
    finished, or the submission was silently dropped (which BFMR has done before — the reason the
    tracker is re-read at all). Those two want opposite responses, and nothing in the API tells them
    apart, so the message leads with the cheap check and stops short of prescribing a fix.
    """
    return (
        f"{tracking_number}: BFMR accepted the request, but the number is in My Tracker under "
        f"NO spelling — neither as sent nor with any letter appended.\n"
        f"\n"
        f"LOOK IN MY TRACKER FIRST. Since 2026-08-13 BFMR handles duplicate tracking numbers "
        f"itself: it runs a Best Buy check, appends a letter (e.g. {tracking_number}B), and emails "
        f"you once the package is received. If that check is still running, the shipment simply "
        f"isn't visible yet and THIS RESOLVES ITSELF — the next run will find it under whichever "
        f"letter BFMR chose, tick Tracking Submitted, and file the insurance.\n"
        f"\n"
        f"IF IT IS STILL ABSENT ON THE NEXT RUN, the submission was dropped rather than delayed, "
        f"and this package is neither submitted nor insured. Add the tracking to the purchase by "
        f"hand in My Tracker (the purchase already carries the order number you submitted when you "
        f"ordered, which is what lets the next run match it back), and raise a support ticket with "
        f"the tracking number and proof of purchase if BFMR won't take it.\n"
        f"\n"
        f"Background: https://support.bfmr.com/hc/en-us/articles/50968170907547\n"
        f"\n"
        f"NOTHING TO EDIT ON THE SHEET either way. The next run reads My Tracker, matches whichever "
        f"letter ends up there back to {tracking_number}, and fills the payout, premium and status "
        f"as they arrive."
    )


def _find_shipment(index: dict, order_id: str, tracking_number: str) -> dict | None:
    """This order's BFMR shipment, under whichever spelling BFMR used for it."""
    for spelling in bfmr_spellings(tracking_number):
        if (order_id, spelling) in index:
            return index[(order_id, spelling)]
    return None


def _held_spelling(index: dict, tracking_number: str) -> str:
    """The spelling BFMR uses for this package, falling back to ours if they hold no record.

    The fallback is the right default for a genuinely new package — BFMR has never seen it, so the
    bare number is exactly what to send.
    """
    for spelling in bfmr_spellings(tracking_number):
        if spelling in index:
            return spelling
    return tracking_number


def _lookup_any_spelling(index: dict, tracking_number: str, default):
    """Tracking-keyed lookup, tolerant of the B/C/D suffix. Used where the PACKAGE is the right unit.

    Insurance is the case: a combined box is one physical package, insured once, so finding "insured"
    under any order's spelling is the correct — and money-safe — answer.
    """
    for spelling in bfmr_spellings(tracking_number):
        if spelling in index:
            return index[spelling]
    return default


def _order_id_of(entry: dict) -> str:
    """Read the retailer order number off a tracker row.

    **THE REQUEST AND THE RESPONSE USE DIFFERENT NAMES.** `POST /api/v2/my-tracker` takes `order_no`;
    the GET returns the same value as **`order_id`** (confirmed live on 44 of 76 rows, holding exactly
    the ids the ledger stores — `114-9990004-9990004`, `BBY01-809900000006`). Reading only `order_no`
    matched NOTHING, so every submission would have failed with "BFMR has no purchase for this
    order" — a total no-op that looks like a configuration problem rather than a field-name typo.
    Both spellings are accepted so the day BFMR aligns them nothing breaks.
    """
    for key in ("order_id", "order_no"):
        value = entry.get(key)
        if value:
            return str(value).strip()
    return ""


def _index_shipments(tracker: list[dict]) -> dict[tuple[str, str], dict]:
    """`(order_id, tracking spelling) -> shipment`.

    Keyed on the PAIR for the same reason `already_submitted` is: in a Best Buy combined box, order
    A's shipment and order B's shipment share a base tracking number. Keyed on tracking alone, order
    B would find order A's record and be sent as an UPDATE carrying A's `shipment_id` — rewriting
    A's shipment with B's details instead of creating B's.
    """
    index: dict[tuple[str, str], dict] = {}
    for entry in tracker:
        number = _tracking_of(entry)
        order_id = _order_id_of(entry)
        if number and order_id and entry.get("shipment_id"):
            index.setdefault((order_id, number), entry)
    return index


def _tracking_of(entry: dict) -> str:
    """Read a tracker row's tracking number, tolerating the field name the spec never documents.

    The published `my-tracker` response schema lists neither `order_no` nor `tracking_number`, yet
    both are accepted by the POST — almost certainly a gap in a hand-maintained Swagger file rather
    than the API genuinely withholding them. Accepting either spelling means the probe's answer
    changes a docstring, not this code.
    """
    for key in ("tracking_number", "tracking_no", "tracking"):
        value = entry.get(key)
        if value:
            return str(value).strip()
    return ""


def _as_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _bfmr_date_to_iso(value) -> str:
    """`08/10/2026` -> `2026-08-10`. The ledger's date columns are plain ISO text.

    The spec types `date_paid` as an INTEGER, which reads as a unix epoch — it is not. Live rows
    carry US-format `MM/DD/YYYY` strings (`reserved_at` adds a time). Parsing those as an epoch would
    have produced 1970 dates, and the sheet would have looked merely odd rather than wrong.

    Anything unrecognised returns "" rather than a guess: `Payout Date` is displayed, sorted and
    read by a human, and an invented date is worse than an empty cell.
    """
    from datetime import datetime

    text = str(value or "").strip()
    if not text:
        return ""
    for fmt in ("%m/%d/%Y", "%m/%d/%Y %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    log.warning("BFMR returned an unparseable date %r; leaving the cell blank", text)
    return ""
