"""
Probe a delivery-tracking API's coverage of real tracking numbers BEFORE any integration code exists.

Why this exists: the tracking-API tier would replace the CDP delivery-watch,
but it only pays off if the provider actually resolves the numbers we hold. Non-Amazon carriers
(UPS/FedEx/USPS  -  most Best Buy shipments) are near-certain to work; the open risk is Amazon
Logistics `TBA...` numbers, which are Amazon's own network and are often only partially covered by
third-party trackers. This script answers that for ~$0 against a provider's free quota, so we don't
build the integration on an assumption.

Provider-neutral by intent, but a probe still has to speak SOME provider's API, so this one targets
17TRACK  -  the first candidate (best economics found so far: 1 quota per number, once). If you
evaluate a different provider (TrackingMore, etc.), swap the three provider-specific pieces: API_BASE,
the auth header in _post(), and the response field paths in _summarize(). The register/poll/verdict
flow is generic.

What it does:
  1. REGISTER each number (POST /register)  -  the provider then starts fetching it asynchronously.
  2. POLL each number (POST /gettrackinfo), retrying a few times because a freshly-registered
     number is usually still "pending" for the first ~30-60s.
  3. Print a per-number verdict: which carrier was detected, the latest status/milestone, and how
     many tracking events came back  -  i.e. whether the number is actually COVERED.

WARNING:  QUOTA: each *new* registration consumes 1 of 17TRACK's 200 free lifetime registrations.
    Re-registering a number you already registered does NOT cost more (17TRACK returns an "already
    registered" rejection, which this script treats as fine). Use --poll-only to re-query for free.

Auth: set SEVENTEENTRACK_API_KEY in the environment (or pass --api-key). This is the "17token" from
your 17TRACK API dashboard.

Usage (run from the project root):
    # Register + poll a couple of real TBA numbers plus a known-good UPS number as a control:
    .venv\\Scripts\\python -m scripts.probe_tracking_api TBA303111222333 1Z999AA10123456784

    # Force a carrier code (e.g. if auto-detect fails for TBA  -  find the code in 17TRACK's
    # carrier list; "Amazon Logistics US" is the one to confirm):
    .venv\\Scripts\\python -m scripts.probe_tracking_api TBA303111222333 --carrier 100365

    # Numbers from a file (one per line, blank lines / #comments ignored):
    .venv\\Scripts\\python -m scripts.probe_tracking_api --file numbers.txt

    # Re-query numbers already registered earlier, spending ZERO quota:
    .venv\\Scripts\\python -m scripts.probe_tracking_api TBA303111222333 --poll-only
"""

import argparse
import json
import os
import sys
import time

import requests

API_BASE = "https://api.17track.net/track/v2.2"
# /register and /gettrackinfo both take a JSON array of up to 40 {"number", "carrier"?} objects.
MAX_BATCH = 40
# A just-registered number needs a moment before 17TRACK has fetched anything for it.
POLL_ATTEMPTS = 4
POLL_INTERVAL_SEC = 20


def _load_numbers(args: argparse.Namespace) -> list[str]:
    numbers = list(args.numbers)
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line:
                    numbers.append(line)
    # De-dupe, preserve order.
    seen: set[str] = set()
    unique = []
    for n in numbers:
        if n not in seen:
            seen.add(n)
            unique.append(n)
    return unique


def _post(path: str, api_key: str, payload: list[dict]) -> dict:
    resp = requests.post(
        f"{API_BASE}{path}",
        headers={"17token": api_key, "Content-Type": "application/json"},
        data=json.dumps(payload),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _entry(number: str, carrier: int | None) -> dict:
    entry: dict = {"number": number}
    if carrier is not None:
        entry["carrier"] = carrier  # omit -> 17TRACK auto-detects the carrier
    return entry


def register(numbers: list[str], api_key: str, carrier: int | None) -> None:
    print(f"\n=== REGISTER ({len(numbers)} number(s)) ===")
    print("(each NEW number consumes 1 of your free registration quota)")
    payload = [_entry(n, carrier) for n in numbers]
    data = _post("/register", api_key, payload).get("data", {})
    for acc in data.get("accepted", []):
        print(f"  registered:  {acc.get('number')}  (carrier={acc.get('carrier')})")
    for rej in data.get("rejected", []):
        err = rej.get("error", {})
        code = err.get("code")
        msg = err.get("message", "")
        # -18019902 = already registered: harmless, no extra quota spent.
        note = "  (already registered  -  no quota spent)" if code == -18019902 else ""
        print(f"  rejected:    {rej.get('number')}  code={code} {msg}{note}")


def _summarize(number: str, track_info: dict) -> str:
    """One-line verdict for a single number from its track_info block."""
    latest = (track_info or {}).get("latest_status", {}) or {}
    status = latest.get("status") or "?"
    sub = latest.get("sub_status") or ""
    providers = ((track_info or {}).get("tracking", {}) or {}).get("providers", []) or []
    carrier_name = "?"
    event_count = 0
    if providers:
        prov = providers[0].get("provider", {}) or {}
        carrier_name = prov.get("name") or prov.get("key") or "?"
        event_count = sum(len(p.get("events", []) or []) for p in providers)

    # No events and no resolved status = 17TRACK has nothing for this number (the TBA risk).
    if event_count == 0 and status in ("?", "NotFound", "InfoReceived"):
        verdict = "NOT COVERED / no data yet"
    else:
        verdict = "COVERED"
    return (
        f"    carrier={carrier_name!r}  status={status}"
        f"{('/' + sub) if sub else ''}  events={event_count}  ->  {verdict}"
    )


def poll(numbers: list[str], api_key: str, dump: bool) -> None:
    print(f"\n=== POLL ({len(numbers)} number(s)) ===")
    pending = set(numbers)
    for attempt in range(1, POLL_ATTEMPTS + 1):
        payload = [{"number": n} for n in sorted(pending)]
        data = _post("/gettrackinfo", api_key, payload).get("data", {})

        for acc in data.get("accepted", []):
            number = acc.get("number")
            track_info = acc.get("track_info", {}) or {}
            latest = (track_info.get("latest_status") or {}).get("status")
            # Keep re-polling numbers 17TRACK is still fetching (no status resolved yet).
            if latest and latest not in ("NotFound", "InfoReceived"):
                pending.discard(number)
            print(f"\n  {number}")
            print(_summarize(number, track_info))
            if dump:
                print("    --- raw track_info ---")
                print(json.dumps(track_info, indent=2, ensure_ascii=False))

        for rej in data.get("rejected", []):
            number = rej.get("number")
            pending.discard(number)
            err = rej.get("error", {})
            print(f"\n  {number}")
            print(f"    rejected: code={err.get('code')} {err.get('message', '')}")

        if not pending or attempt == POLL_ATTEMPTS:
            break
        print(
            f"\n  {len(pending)} number(s) still being fetched by 17TRACK; "
            f"waiting {POLL_INTERVAL_SEC}s (attempt {attempt}/{POLL_ATTEMPTS - 1})..."
        )
        time.sleep(POLL_INTERVAL_SEC)

    if pending:
        print(
            f"\n  Still pending after {POLL_ATTEMPTS} attempts: {', '.join(sorted(pending))}\n"
            "  Re-run with --poll-only in a minute (costs no quota) to check again."
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("numbers", nargs="*", help="Tracking numbers to probe")
    parser.add_argument("--file", help="File with one tracking number per line (# comments allowed)")
    parser.add_argument(
        "--carrier",
        type=int,
        default=None,
        help="Force a 17TRACK carrier code instead of auto-detect (e.g. Amazon Logistics US)",
    )
    parser.add_argument(
        "--poll-only",
        action="store_true",
        help="Skip registration; only query numbers already registered (spends 0 quota)",
    )
    parser.add_argument(
        "--dump",
        action="store_true",
        help="Print the full raw track_info JSON for each number, not just the one-line verdict",
    )
    parser.add_argument("--api-key", default=None, help="17TRACK API token (else SEVENTEENTRACK_API_KEY)")
    args = parser.parse_args()

    api_key = args.api_key or os.getenv("SEVENTEENTRACK_API_KEY", "")
    if not api_key:
        sys.exit("No API key. Set SEVENTEENTRACK_API_KEY or pass --api-key.")

    numbers = _load_numbers(args)
    if not numbers:
        sys.exit("No tracking numbers given. Pass them as arguments or via --file.")

    if len(numbers) > MAX_BATCH:
        sys.exit(f"{len(numbers)} numbers exceeds 17TRACK's per-call max of {MAX_BATCH}.")

    try:
        if not args.poll_only:
            register(numbers, api_key, args.carrier)
            # Give 17TRACK a beat to start fetching before the first poll.
            time.sleep(5)
        poll(numbers, api_key, args.dump)
    except requests.HTTPError as exc:
        body = exc.response.text[:500] if exc.response is not None else ""
        sys.exit(f"17TRACK HTTP error: {exc}\n{body}")

    print(
        "\nDone. Read the verdicts above: if the TBA... numbers show COVERED with real events, the "
        "API tier can handle Amazon Logistics. If they're NOT COVERED while UPS/FedEx/USPS controls "
        "are COVERED, plan on API-for-real-carriers + CDP-for-TBA."
    )


if __name__ == "__main__":
    main()
