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

#: How many suffixed re-sends to spend on ONE refused number. Candidates already visible in My Tracker
#: are skipped for free, so a real multi-order carton normally lands on the first attempt; this cap
#: exists for the other case — a number BFMR refuses for a reason a letter cannot fix — so it costs a
#: few requests rather than a march through the alphabet.
MAX_SUFFIX_ATTEMPTS = 5

#: The suffix retry is for BEST BUY ONLY. The duplicate-tracking problem is Best
#: Buy reusing ONE tracking number across the orders it packs into a carton; Amazon and Costco do not
#: do that, so a number THEY refuse means something else entirely and appending a letter would paper
#: over it — inventing a spelling no carrier ever issued and hiding the real reason behind a package
#: that now looks submitted. Those keep their previous handling: rejected -> failed, vanished ->
#: needs_manual.
#:
#: Matched on the ORDER-ID SHAPE rather than a retailer field, which keeps the guard inside this
#: module instead of threading a new column through TrackingSubmission and every caller. Best Buy
#: order numbers are `BBY01-<digits>` (the same shape scrapers/bestbuy_api.py keys on). If that ever
#: changes the retry simply stops firing and the package goes to needs_manual WITH an alert — it
#: degrades to the old manual chore rather than being lost.
BESTBUY_ORDER_PREFIX = "BBY01-"


def _is_bestbuy_order(order_id) -> bool:
    return str(order_id or "").upper().startswith(BESTBUY_ORDER_PREFIX)

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

#: BFMR statuses past the point where insuring a package makes sense — the purchase is settled.
#:
#: This is what `insurance_status: "not_eligible"` actually tracks. The probe (2026-08-13) found it
#: on 62 rows, every one of them in one of these states, and 58 of those were INSURED — so it marks
#: the end of the lifecycle, not a refusal to cover. Keying off BFMR's own `status` says that
#: plainly, and stops "not eligible" being read as "BFMR won't insure this".
_TERMINAL_BFMR_STATUSES = {"paid", "returned", "cancelled"}

#: BFMR's handling of the **Best Buy duplicate tracking issue**:
#: https://support.bfmr.com/hc/en-us/articles/50968170907547
#:
#: Best Buy sometimes issues the SAME tracking number for two different orders. BFMR's tracker
#: enforces uniqueness, so the second one used to be rejected as invalid, and their instruction was
#: to "resubmit the tracking number and add a letter ("B", "C", or "D") to the very end of the number
#: until the system accepts it".
#:
#: **BFMR briefly did the suffixing itself (2026-08-13); AS OF 2026-08-23 IT DOES NOT.** So the
#: suffix is ours to CHOOSE again — `_resubmit_with_suffix` picks the letter and re-sends — as well as
#: ours to RECOGNISE. Recognition stays exactly as load-bearing as before: a spelling can still reach
#: us that we did not send (BFMR may hold one from the period when it suffixed, or from a manual fix),
#: and every join below has to survive it.
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

    def cancelled_purchases_for(self, order_ids) -> set[str]:
        """Which of these orders BFMR has CANCELLED the purchase for, with none still active.

        The mirror of `active_purchases_for`, and it catches the divergence that costs the payout:
        BFMR cancelled the purchase — almost always because the tracking number missed their
        deadline — while the retailer order is alive and on its way.

        **THAT CAN ONLY HAPPEN WHILE THE ORDER IS STILL `ordered`**: the deadline
        is for submitting tracking, so once a package ships and its number is attached there is
        nothing left for BFMR to cancel over. Which means `submit_tracking`'s own cancelled-purchase
        check — reached only by rows that HAVE a tracking number — is guarding a state this can
        barely occur in, while the state it does occur in never reached a BFMR call at all.

        AN ACTIVE PURCHASE ANYWHERE WINS. An order can carry more than one purchase row (a cancelled
        first attempt plus a live re-book), and reporting that as cancelled would send someone to
        support about a deal they still hold — the same precedence `_index_purchases_by_order`
        applies, for the same reason.
        """
        wanted = {str(o).strip() for o in order_ids if str(o).strip()}
        cancelled: set[str] = set()
        active: set[str] = set()
        for entry in self.fetch_tracker():
            order_id = _order_id_of(entry)
            if order_id not in wanted or not entry.get("purchase_id"):
                continue
            if _is_insurance_fee_row(entry):
                continue
            (cancelled if _is_cancelled_purchase(entry) else active).add(order_id)
        return cancelled - active

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
        # Confirm PER ORDER, not per tracking number. In a combined carton several orders share one
        # base number, so "is this number in the tracker?" would see the FIRST order's shipment and
        # report the second as landed when nothing was recorded for it — the exact silent loss the
        # re-read exists to catch. `_find_shipment` is keyed on (order, spelling) and tolerates the
        # suffix. `taken` stays number-only on purpose: it answers "is this LETTER free anywhere?".
        entries = self.fetch_tracker()
        landed = _landed_pairs(entries)
        taken = {_tracking_of(entry) for entry in entries}
        for obj in batch:
            number = obj["tracking_number"]
            if _confirmed(landed, obj.get("order_no", ""), number):
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
            elif _is_bestbuy_order(obj.get("order_no")):
                # Refused outright (invalid_items) or accepted-then-silently-dropped. For a Best Buy
                # order both are the duplicate-carton case, and since 2026-08-23 BFMR no longer
                # resolves it, so we append the letter ourselves rather than handing over a chore.
                #
                # `invalid` is passed through only so an exhausted retry can quote BFMR's own words
                # instead of guessing at why the number was refused.
                self._resubmit_with_suffix(
                    obj, result, taken, rejected=number in invalid_numbers, invalid=invalid,
                )
            elif number in invalid_numbers:
                # Not a Best Buy carton, so a letter cannot fix it — report it as it always was.
                result.failed.append((number, f"BFMR rejected it: {invalid}"))
            else:
                result.needs_manual.append((number, _duplicate_tracking_hint(number)))

    def _resubmit_with_suffix(self, obj: dict, result: SubmissionResult, taken: set[str],
                              *, rejected: bool = False, invalid=None) -> None:
        """Re-send one refused tracking number with a letter appended, until BFMR takes it.

        Best Buy issues the SAME tracking number for several orders when it combines them into one
        carton, and BFMR's tracker enforces uniqueness, so every order after the first is refused.
        Their fix is to "add a letter to the very end of the number until the system accepts it".

        STARTS AT B, because the bare number is the original — matching their article's "B", "C", "D".
        Candidates already present in My Tracker are skipped WITHOUT spending a request, which is what
        keeps a real carton to one attempt: by the time the third order in it is sent, B is visibly
        taken, so C is tried first.

        Success is confirmed by RE-READING the tracker, for the same reason the first attempt is: BFMR
        has been seen to accept a request and record nothing. The ledger keeps the BARE number — every
        later join runs through `bfmr_spellings`, and the sheet only ever knows what the retailer
        issued.
        """
        bare = obj["tracking_number"]
        attempts = 0
        for letter in BFMR_DUPLICATE_SUFFIXES[1:]:   # skip "A": the bare number IS the original
            candidate = bare + letter
            if candidate in taken:
                continue
            if attempts >= MAX_SUFFIX_ATTEMPTS:
                break
            attempts += 1
            response = self.request(
                "POST", "/api/v2/my-tracker", mutating=True,
                json_body={"tracker_data": [{**obj, "tracking_number": candidate}]},
            )
            if response is None:  # dry run — nothing was sent, so nothing can be confirmed
                result.skipped.append((bare, f"dry run (refused; would retry as {candidate})"))
                return
            entries = self.fetch_tracker()
            taken |= {_tracking_of(entry) for entry in entries}
            if _confirmed(_landed_pairs(entries), obj.get("order_no", ""), candidate):
                log.info("BFMR: %s was refused (Best Buy duplicate carton); accepted as %s.",
                         bare, candidate)
                result.submitted.append(bare)
                return
        result.needs_manual.append(
            (bare, _duplicate_tracking_hint(bare, attempts, rejected=rejected, invalid=invalid))
        )

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
        fee_row_premiums: dict[str, int] = {}   # ledger number -> its index in `records`
        for entry in self.fetch_tracker():
            spelling = _tracking_of(entry)
            number = lookup.get(spelling, "")
            if not number:
                continue

            if _is_insurance_fee_row(entry):
                fee_row_premiums[number] = len(records)
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

            # BFMR FLIPS `status` TO "paid" BEFORE `amount_paid` IS POPULATED, so a paid row routinely
            # still reports "0.00". The
            # docstring above already noted amount_paid reads "0.00" until BFMR pays — what was missing
            # is that `status` does NOT wait for it, so the two disagree for a while.
            #
            # A zero here is therefore "not settled yet", NOT "paid nothing". The distinction is the
            # whole ballgame: _profit_formula renders BLANK while Payout Amount is empty, but a literal
            # 0 makes it compute `0 - Total Cost - Insurance` — a large fictitious LOSS on a perfectly
            # healthy order. Live it put **-$1,678.46** against a $1,796 order that BFMR had not yet
            # paid. No buying group pays $0 for a package it accepted, so reading 0 as "not yet" can
            # never discard a real payout.
            #
            # The amount, the date and the `paid` status are suppressed TOGETHER. Recording "paid" on
            # a row with no money is not just cosmetically odd — `paid` is TERMINAL, so it would stop
            # the retailer re-checking a row whose payout had never arrived. All three land together on
            # a later sync once BFMR settles; if that never happens, the row simply stays open and
            # `audit_sheet`'s open_row_staleness surfaces it rather than it going quietly wrong.
            settled = parse_money(entry.get("amount_paid")) if paid else None
            if paid and not settled:
                # Scoped to `paid` deliberately: a `returned` package reports no amount either, and
                # blanking its status here would silently drop the one outcome BFMR is authoritative
                # about besides payment.
                paid, status, settled = False, "", None

            records.append(PayoutRecord(
                tracking_number=number,
                # `amount_paid`, not `total_payout`: the latter is what the deal is WORTH and is
                # populated from the moment a purchase exists, so writing it would fill Payout Amount
                # — and light up Total Profit — for money that has not arrived. `amount_paid` reads
                # "0.00" until BFMR actually pays. And only when BFMR says paid at all: a `returned`
                # package's figures are ambiguous, and a blank cell correctly reads "not paid out"
                # where a wrong number would quietly overstate profit.
                payout_amount=settled,
                payout_date=_bfmr_date_to_iso(entry.get("date_paid")) if paid else "",
                insurance=None,
                order_id=_order_id_of(entry),
                status=status,
            ))

        # The insurance list is authoritative for the premium, and unlike the fee row it exists from
        # the moment a package is insured. Both sources agreed on all 30 real filings to the cent
        # (2026-08-13), so this is about TIMING rather than accuracy: a package insured DURING this
        # run gets its Insurance cell filled the same run instead of waiting for BFMR to post the fee
        # line. Read second, and only used to OVERRIDE — so a package the list doesn't mention keeps
        # its fee-row figure, and a read failure changes nothing at all.
        try:
            for spelling, premium in self.insured_shipments().items():
                number = lookup.get(spelling, "")
                index = fee_row_premiums.get(number)
                if premium is None or not number:
                    continue
                if index is None:
                    records.append(PayoutRecord(tracking_number=number, insurance=premium))
                else:
                    records[index] = PayoutRecord(tracking_number=number, insurance=premium)
        except Exception as exc:  # noqa: BLE001 — the fee rows already covered this; don't fail a run
            log.info("BFMR: insured list unavailable (%s); premiums came from the fee rows.", exc)
        return records

    def insured_shipments(self) -> dict[str, float | None]:
        """`{tracking_number: premium}` for every shipment BFMR has insured. THE authoritative read.

        `GET /api/v2/insurance/shipments` came back to life on 2026-08-13 (it 404'd with a Laravel
        route-not-found when this adapter was written, which is why everything below used to be
        inferred). Paginated, 20 per page, and `insurance.paging.last_page` must be followed — a
        single-page read would silently report the older half of the account as UNINSURED, which on
        the filing path means paying a second premium for each one.

        THE LIST, NOT THE PER-TRACKING ENDPOINT, and that is a correctness choice rather than a
        performance one. `GET /api/v2/insurance/shipments/{tracking}` demands the exact spelling BFMR
        holds: `…/529900000009B` returns the record while `…/529900000009` — the bare number our
        ledger stores — returns 404 "Insurance not found". Since BFMR now picks the duplicate letter
        itself we never know which spelling to ask for, so a per-number lookup would have to try all
        27. The list returns every spelling BFMR actually used and lets `bfmr_spellings` do the join
        locally, exactly as `fetch_payouts` does.

        NB the two endpoints spell the same fields differently, and the single-record one is a trap:

            list                    single           meaning
            cost_of_insurance       insured_amount   THE PREMIUM — not the coverage, despite the name
            package_value           package_cost     declared value
            certificate_number      cert_no          certificate

        Returns `{}` only when the account genuinely has none; a transport failure RAISES, because
        callers must be able to tell "nothing is insured" from "I could not find out" — see
        `file_insurance`, where confusing the two costs a duplicate premium.
        """
        insured: dict[str, float | None] = {}
        page = 1
        while True:
            payload = self.get_json("/api/v2/insurance/shipments", params={"page": page})
            block = payload.get("insurance") or {}
            for entry in block.get("shipments") or []:
                number = str(entry.get("tracking_number") or "").strip()
                if number:
                    insured[number] = parse_money(entry.get("cost_of_insurance"))
            paging = block.get("paging") or {}
            last = _as_int(paging.get("last_page"), default=page)
            if page >= last:
                return insured
            page += 1

    def insured_tracking_numbers(self) -> dict[str, str]:
        """`{tracking_number: insurance_status}` from My Tracker. NOT a record of what is insured.

        Kept because it is the only insurance-ish signal that survives if the real endpoint vanishes
        again — but it must never be used to decide whether to FILE, and the live data shows why:

            insured  insurance_status  BFMR status        rows
            yes      not_eligible      paid / processed     58
            yes      insured           shipped               3
            no       not_eligible      paid / returned       4

        It is a LIFECYCLE field — `insured` while the purchase is open, flipping to `not_eligible`
        once it is terminal — so it reads `not_eligible` for all 30 genuinely insured shipments.
        `file_insurance` used it as the never-double-file guard and survived only because `insured`
        and `not_eligible` both happen to skip; it cannot tell "already insured" from "not
        insurable", which is exactly why nothing was ever filed. Use `insured_shipments()` instead.
        """
        return {
            number: str(entry.get("insurance_status") or "")
            for entry in self.fetch_tracker()
            if (number := _tracking_of(entry))
        }

    def file_insurance(self, rows: list[TrackingSubmission]) -> SubmissionResult:
        """File insurance for open shipments BFMR has no insurance record for.

        THIS SPENDS REAL MONEY on an unattended schedule, so every decision here is deliberate:

        - **Already insured is decided by `insured_shipments()`, never by `insurance_status`.** The
          old guard read the latter and worked only by coincidence — it says `not_eligible` for all
          30 genuinely insured shipments, and survived purely because `insured` and `not_eligible`
          both skipped. See `insured_tracking_numbers` for the live distribution.
        - **A TERMINAL purchase is skipped, on BFMR's own `status`.** That is what `not_eligible`
          actually meant: it flips once the purchase is paid/returned/cancelled, not because BFMR
          refuses to insure. Reading it as "uninsurable" is why this pass had never once filed
          anything — an open shipment reads `insured` only when it already is, so every row hit one
          skip or the other.
        - **If the authoritative set cannot be read, NOTHING is filed.** These routes have vanished
          before, and the fallback provably cannot tell uninsured from ineligible, so degrading to it
          would risk a second premium on an already-insured package. Never spend money on a guess.
        - `package_value` is NOT sent. BFMR derives it from the shipment items it already holds,
          which removes any chance of over-declaring and overpaying a premium we invented.
        - `bfmr_min_insurance_value` gates the rest. BFMR charges ~0.45% with a $2.00 MINIMUM, so
          below roughly $450 the floor dominates and the effective rate passes 1%.
        - **A filing is confirmed by RE-READING, not by the response.** BFMR has already once
          returned success for a my-tracker submission it silently dropped; here real money moved, so
          a filing that does not appear in the insured set afterwards is reported rather than
          counted.
        """
        result = SubmissionResult()
        if not rows:
            return result

        # ONE tracker read for both maps. `fetch_tracker` is uncached and paginated, so deriving
        # these from separate calls would re-download the whole account twice per run for nothing.
        tracker = self.fetch_tracker()
        terminal = _index_terminal_purchases(tracker)
        spellings = _index_tracker_spellings(tracker)

        try:
            insured = self.insured_shipments()
        except Exception as exc:  # noqa: BLE001 — deliberate: unknown state must not spend money
            log.error(
                "BFMR: could not read the insured-shipment list (%s); filing NOTHING this run. "
                "Insurance is only skipped, never duplicated, so the next run picks it up.", exc,
            )
            result.skipped.extend(
                (row.tracking_number, "insured-shipment list unavailable — not filing on a guess")
                for row in rows
            )
            return result
        filed_spellings: list[tuple[str, str]] = []
        for row in rows:
            if _has_any_spelling(insured, row.tracking_number):
                result.skipped.append((row.tracking_number, "already insured"))
                continue
            if _has_any_spelling(terminal, row.tracking_number):
                result.skipped.append((
                    row.tracking_number,
                    "BFMR's purchase is already terminal (paid/returned/cancelled)",
                ))
                continue
            if (row.total_cost or 0) < self.min_insurance_value:
                result.skipped.append((
                    row.tracking_number,
                    f"below the BFMR_MIN_INSURANCE_VALUE threshold of {self.min_insurance_value}",
                ))
                continue
            # File against the spelling BFMR actually HOLDS. Sending the ledger's bare number for a
            # package they filed under "…B" posts against a shipment they have no record of: a 2xx
            # that matches nothing, reported as success, leaving the package uninsured. Observed
            # live on 529900000009 (BFMR holds 529900000009B).
            spelling = _held_spelling(spellings, row.tracking_number)
            response = self.request(
                "POST",
                "/api/v2/insurance/file",
                mutating=True,
                data={"tracking_number": spelling},
            )
            if response is None:
                result.skipped.append((row.tracking_number, "dry run"))
            else:
                filed_spellings.append((row.tracking_number, spelling))

        if filed_spellings:
            self._confirm_filings(filed_spellings, result)
        return result

    def _confirm_filings(self, filed: list[tuple[str, str]], result: SubmissionResult) -> None:
        """Re-read the insured set and only count a filing that actually landed.

        One extra pair of GETs after a run that spent money — cheap next to a premium that bought
        nothing. A filing that does not appear is reported as needing a human rather than as a
        failure: nothing we can retry distinguishes "BFMR dropped it" from "it has not propagated",
        and retrying is the one response that could double-charge.
        """
        try:
            insured_now = self.insured_shipments()
        except Exception as exc:  # noqa: BLE001
            log.warning("BFMR: filed insurance but could not verify it (%s).", exc)
            result.submitted.extend(number for number, _ in filed)
            return

        for number, spelling in filed:
            if spelling in insured_now or _has_any_spelling(insured_now, number):
                result.submitted.append(number)
            else:
                result.needs_manual.append((
                    number,
                    f"{number}: BFMR accepted an insurance filing for {spelling} but the shipment is "
                    f"not in their insured list afterwards. IT MAY OR MAY NOT BE COVERED, AND IT IS "
                    f"NOT RETRIED — a retry is the one thing that could charge you twice. Check the "
                    f"package in BFMR's insurance list and file it by hand if it is genuinely absent.",
                ))

    def void_insurance(self, tracking_numbers: list[str]) -> SubmissionResult:
        """Undo a filing. Exists from day one so a mistaken automatic run is reversible.

        Resolves each number to the spelling BFMR holds first, for the same reason `file_insurance`
        does — voiding against a number they have no record of would silently do nothing, and a void
        that quietly fails is worse than one that errors.
        """
        result = SubmissionResult()
        # The INSURED list, not My Tracker: a void targets an insurance record, and that record is
        # where the authoritative spelling lives. Falls back to the tracker's spellings if the route
        # is unavailable — a void that misses is recoverable, unlike a duplicate filing, so this one
        # does not refuse to act on partial information.
        held = _index_tracker_spellings(self.fetch_tracker())
        try:
            # The insurance record's own spelling wins where the two differ — a void targets an
            # insurance record, so that is the authoritative place to read it from.
            held.update({number: number for number in self.insured_shipments()})
        except Exception as exc:  # noqa: BLE001
            log.warning("BFMR: insured list unavailable (%s); resolving the void from My Tracker.", exc)
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


def _index_terminal_purchases(tracker: list[dict]) -> dict[str, str]:
    """`{tracking_number: bfmr_status}` for purchases past the point of insuring.

    Keyed on BFMR's own `status`, NOT on `insurance_status`. The probe (2026-08-13) showed the latter
    flips to `not_eligible` on exactly these rows, which is what made it look like an eligibility
    signal and kept `file_insurance` from ever filing anything.
    """
    return {
        number: status
        for entry in tracker
        if (number := _tracking_of(entry))
        and (status := str(entry.get("status") or "").lower()) in _TERMINAL_BFMR_STATUSES
    }


def _index_tracker_spellings(tracker: list[dict]) -> dict[str, str]:
    """`{tracking_number: itself}` for every spelling My Tracker holds, for `_held_spelling`."""
    return {number: number for entry in tracker if (number := _tracking_of(entry))}


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


def _duplicate_tracking_hint(tracking_number: str, attempts: int = 0, *,
                             rejected: bool = False, invalid=None) -> str:
    """The message for a number BFMR would not take even with letters appended.

    Best Buy reuses one tracking number across the orders it combines into a carton; BFMR's tracker
    demands unique numbers, and their fix is to append a letter until it is accepted. BFMR automated
    that on 2026-08-13 and stopped again on 2026-08-23, so `_resubmit_with_suffix` does it. Reaching
    HERE means even the suffixed sends were refused or vanished, which a letter cannot fix — so the
    message stops guessing and hands over the two facts a human needs: what we sent, and what BFMR
    said about it.
    """
    tried = (f"tried the bare number and then {attempts} suffixed spelling(s) "
             f"(e.g. {tracking_number}B)" if attempts else "tried the bare number")
    why = (f"BFMR rejected it outright: {invalid}\n" if rejected and invalid else
           "BFMR accepted the request but recorded nothing under any spelling.\n")
    return (
        f"{tracking_number}: could not be handed to BFMR. We {tried}, and the number is in My "
        f"Tracker under NO spelling.\n"
        f"\n"
        f"{why}"
        f"\n"
        f"THIS PACKAGE IS NEITHER SUBMITTED NOR INSURED, so it needs a hand. Add the tracking to the "
        f"purchase by hand in My Tracker (the purchase already carries the order number you submitted "
        f"when you ordered, which is what lets the next run match it back), appending a letter "
        f"yourself if BFMR says the number is already in use. Raise a support ticket with the "
        f"tracking number and proof of purchase if it still won't take it.\n"
        f"\n"
        f"Background: https://support.bfmr.com/hc/en-us/articles/50968170907547\n"
        f"\n"
        f"NOTHING TO EDIT ON THE SHEET. The next run reads My Tracker, matches whichever letter ends "
        f"up there back to {tracking_number}, and fills the payout, premium and status as they arrive."
    )


def _landed_pairs(tracker: list[dict]) -> set[tuple[str, str]]:
    """`(order, tracking spelling)` for everything currently in My Tracker."""
    return {(_order_id_of(entry), _tracking_of(entry)) for entry in tracker if _tracking_of(entry)}


def _confirmed(landed: set[tuple[str, str]], order_id: str, tracking_number: str) -> bool:
    """Did THIS order's shipment actually land, under any spelling?

    Scoped to the order deliberately. In a Best Buy combined carton several orders share one base
    number, so asking only "is this number in the tracker?" would see the FIRST order's shipment and
    report the second as submitted when nothing was recorded for it — precisely the silent loss the
    post-submit re-read exists to catch, and the reason a carton would never reach the suffix retry.

    An entry carrying NO order at all still counts as a match: it cannot be attributed to anyone, and
    inventing an attribution would be worse than the pre-2026-08-23 behaviour it preserves. BFMR's
    real tracker rows always carry an order number, so this is a tolerance, not a path we rely on.
    """
    for spelling in bfmr_spellings(tracking_number):
        if (order_id, spelling) in landed or ("", spelling) in landed:
            return True
    return False


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


def _has_any_spelling(index: dict, tracking_number: str) -> bool:
    """Is this package present under ANY spelling BFMR might have used?

    Deliberately separate from `_lookup_any_spelling`: presence and value are different questions,
    and conflating them costs money here. `insured_shipments()` maps a tracking number to its
    PREMIUM, and a premium that failed to parse is `None` — so a `lookup(...) is not None` test would
    read a genuinely insured package as uninsured and file it a second time.
    """
    return any(spelling in index for spelling in bfmr_spellings(tracking_number))


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
