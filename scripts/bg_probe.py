"""Read-only recon against the buying-group APIs. WRITES NOTHING, SUBMITS NOTHING, SPENDS NOTHING.

This is the decision gate, in the same spirit as scripts/bestbuy_capture.py and the Amazon capture:
both adapters were written from published documentation, and on this project documentation has been
wrong before in ways that cost real money to discover. The Best Buy GraphQL design was overturned by
a capture; Amazon's order-history pagination silently dropped orders until a live run exposed it. So
the rule is: confirm against a real account BEFORE the first write, not after the first bad one.

FOUR QUESTIONS IT EXISTS TO ANSWER:

1. **BFMR's join key.** `GET /api/v2/my-tracker` documents NEITHER `order_no` NOR `tracking_number`
   in its response schema — checked against every GET in the spec. If that is real rather than a gap
   in a hand-maintained Swagger file, there is no documented way to match a payout row back to a
   ledger row, and `buying_groups/bfmr.py:_tracking_of` / `_index_purchases_by_order` are both built
   on the assumption that the fields are simply undocumented. This confirms or refutes that.

2. **Does BFMR support multiple shipments per purchase?** Nothing in the docs says it does. The
   per-record `shipment_id` and "null means create" semantics imply yes, and a buying group would be
   unusable otherwise — but the entire split design rests on it, so it gets checked rather than
   assumed. The signal is several tracker rows sharing one `purchase_id`.

3. **MOD's payout column.** Its CSV carries both `COMMISSION` and `EXT TOTAL` and the docs describe
   neither. Picking wrong writes a plausible but incorrect number into Payout Amount, which feeds
   Total Profit — a wrong figure that nothing downstream would ever flag.

4. **BFMR's rate limit.** Every endpoint documents a 429; none states the actual limit.

COST: BFMR calls appear to be unmetered (no published limit). The MOD read spends **one of ten**
daily receipt searches, so it is opt-in via --mod rather than run by default.

    python -m scripts.bg_probe                  # BFMR only (free)
    python -m scripts.bg_probe --mod            # also spend 1 of MOD's 10 daily receipt searches
    python -m scripts.bg_probe --tracking 1Z... # also check one known number against BFMR
"""

import argparse
import json
import logging
from pathlib import Path

from buying_groups.base import BuyingGroupError
from buying_groups.bfmr import BFMRClient
from buying_groups.maxoutdeals import MaxOutDealsClient, parse_received_items_csv

log = logging.getLogger("bg_probe")

#: Real account data (order numbers, addresses, payouts) — gitignored, like the retailer captures.
CAPTURE_DIR = Path(__file__).resolve().parent.parent / ".bg_capture"

#: Header-ish fields worth redacting before anything is written to disk.
_REDACT = {"api-key", "api-secret", "authorization"}


def _save(name: str, payload) -> Path:
    CAPTURE_DIR.mkdir(exist_ok=True)
    path = CAPTURE_DIR / name
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=1, default=str)
    path.write_text(text, encoding="utf-8")
    return path


def probe_bfmr(tracking_number: str | None) -> None:
    client = BFMRClient(dry_run=True)  # dry_run gates mutations; every call below is a GET

    print("\n=== BFMR ===")
    tracker = client.fetch_tracker()
    print(f"my-tracker returned {len(tracker)} row(s) -> {_save('bfmr_my_tracker.json', tracker)}")

    if not tracker:
        print("  (no tracker rows — reserve or purchase something on BFMR and re-run)")
    else:
        keys = sorted({k for row in tracker for k in row})
        print(f"  fields present: {keys}")

        # Q1 — the join key.
        for field in ("order_no", "tracking_number", "tracking_no", "tracking"):
            present = sum(1 for row in tracker if row.get(field))
            verdict = "PRESENT" if present else "ABSENT"
            print(f"  Q1 {field:<16} {verdict:<8} ({present}/{len(tracker)} rows)")
        if not any(row.get("order_no") for row in tracker):
            print("  !! No order_no anywhere. buying_groups/bfmr.py cannot match a ledger order to "
                  "a BFMR purchase — try GET /api/v2/my-tracker?search=<order id> instead.")

        # Q2 — several shipments under one purchase?
        by_purchase: dict[str, int] = {}
        for row in tracker:
            pid = str(row.get("purchase_id") or "")
            if pid and row.get("shipment_id"):
                by_purchase[pid] = by_purchase.get(pid, 0) + 1
        multi = {p: n for p, n in by_purchase.items() if n > 1}
        print(f"  Q2 purchases with >1 shipment: {len(multi)} "
              f"{'-> SPLITS ARE SUPPORTED' if multi else '-> inconclusive (no split observed yet)'}")

        # Q3 (BFMR half) — the status vocabularies neither documented anywhere.
        print(f"  status values:          {sorted({str(r.get('status')) for r in tracker})}")
        print(f"  tracking_status values: {sorted({str(r.get('tracking_status')) for r in tracker})}")
        print(f"  insurance_status values:{sorted({str(r.get('insurance_status')) for r in tracker})}")

    raw = client.get_json("/api/v2/deal/reservations/active")
    reservations = client.active_reservations()
    print(f"active reservations: {len(reservations)} -> {_save('bfmr_reservations.json', raw)}")
    if reservations:
        item = reservations[0].get("item") or {}
        print(f"  reservation fields: {sorted(reservations[0])}")
        print(f"  item fields:        {sorted(item) if isinstance(item, dict) else type(item).__name__}")
    else:
        print("  (none — so the empty-reservation matcher has no live shape to be written against;"
              " re-run this once a reservation exists)")

    _probe_insurance_routes(client, tracker)

    if tracking_number:
        try:
            status = client.get_json(
                "/api/v2/shipments/status", params={"tracking_number": tracking_number}
            )
            print(f"shipments/status for {tracking_number} -> "
                  f"{_save('bfmr_shipment_status.json', status)}")
        except BuyingGroupError as exc:
            # A 404 here is a real answer — it is how already_submitted() reads "not known".
            print(f"shipments/status for {tracking_number}: {exc}")


def _probe_insurance_routes(client, tracker: list[dict]) -> None:
    """Are BFMR's two documented insurance READS deployed yet? Today: no.

    THE DISTINCTION THAT MATTERS is which KIND of 404 comes back, because they mean opposite things:

      - Laravel's "The route ... could not be found"  -> the endpoint DOES NOT EXIST. Documented,
        never deployed. Nothing to read, for any shipment.
      - the spec's own documented 404, "Insurance not found" -> the route works; THIS shipment just
        isn't insured.

    Both render as "404" to a caller that only reads the status code, and conflating them is how
    "BFMR has no insurance data at all" gets mistaken for "this package isn't insured". While the
    routes are absent, the premium comes from the negative FEE row on My Tracker instead (see
    `buying_groups/bfmr.py:_is_insurance_fee_row`) — the only source that actually works.

    Re-run this after any BFMR API update: the day the route appears, `/insurance/shipments` gives
    an authoritative `cost_of_insurance`, plus certificate numbers and links the fee row cannot.
    """
    insured = next(
        (r for r in tracker if r.get("insurance_status") == "insured" and r.get("tracking_number")),
        None,
    )
    sample = insured["tracking_number"] if insured else "TEST"
    print(f"  insurance routes (probing with {sample!r}, insurance_status="
          f"{insured.get('insurance_status') if insured else 'n/a'}):")

    for path, params in (
        (f"/api/v2/shipment/insured/{sample}", None),
        ("/api/v2/insurance/shipments", {"tracking_numbers": sample, "per_page": 50}),
    ):
        try:
            payload = client.get_json(path, params=params)
            print(f"    DEPLOYED   {path} -> {_save('bfmr_insurance.json', payload)}")
            print("      !! The route now exists — switch the premium source off the fee row.")
        except BuyingGroupError as exc:
            message = str(exc)
            if "could not be found" in message:
                print(f"    NOT-BUILT  {path}  (Laravel route missing — documented, not deployed)")
            elif "404" in message:
                print(f"    NO-RECORD  {path}  (route works; this shipment simply isn't insured)")
            else:
                print(f"    ERROR      {path}: {message[:120]}")


def probe_mod(tracking_numbers: list[str]) -> None:
    print("\n=== MaxOutDeals (spends 1 of today's 10 receipt searches) ===")
    client = MaxOutDealsClient(dry_run=True)
    client.pull_budget.spend()
    response = client.request(
        "POST",
        "/p/it@api@received-items/cmd/search",
        mutating=False,
        json_body=client._body(**({"trackings": tracking_numbers} if tracking_numbers else {})),
    )
    body = response.text
    print(f"raw response -> {_save('mod_received_items.csv', body)}")

    header = body.strip().splitlines()[0] if body.strip() else ""
    print(f"  header: {header}")
    try:
        records = parse_received_items_csv(body)
    except BuyingGroupError as exc:
        print(f"  !! {exc}")
        return
    print(f"  parsed {len(records)} payout record(s)")
    for record in records[:5]:
        print(f"    {record.tracking_number:<24} amount={record.payout_amount} "
              f"date={record.payout_date!r}")
    print("  Q3: compare the amounts above against what MOD's dashboard says it PAID you. If they "
          "don't match, the payout is EXT TOTAL rather than COMMISSION — change _PAYOUT_COLUMN in "
          "buying_groups/maxoutdeals.py.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--mod", action="store_true",
                        help="Also probe MaxOutDeals (spends 1 of its 10 daily receipt searches)")
    parser.add_argument("--tracking", nargs="*", default=[],
                        help="Known tracking number(s) to look up")
    parser.add_argument("--skip-bfmr", action="store_true", help="Don't probe BFMR")
    args = parser.parse_args()

    print("READ-ONLY probe. Nothing is submitted, written, or insured.")
    if not args.skip_bfmr:
        try:
            probe_bfmr(args.tracking[0] if args.tracking else None)
        except BuyingGroupError as exc:
            print(f"\nBFMR: {exc}")
    if args.mod:
        try:
            probe_mod(args.tracking)
        except BuyingGroupError as exc:
            print(f"\nMaxOutDeals: {exc}")

    print(f"\nCaptures written to {CAPTURE_DIR} (gitignored — they hold real account data).")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    main()
