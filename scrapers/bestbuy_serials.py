"""Pull the units' SERIAL NUMBERS for specific Best Buy orders, on demand.

Serials are NOT part of the normal scrape and there is no ledger column for them: they are only ever needed when BFMR receives a combined Best Buy box and emails
asking for them, so respond_bfmr.py calls this at that moment — pull up the box's order ids,
read the serials off the site, reply. Only Best Buy has the combined-package issue.

The hunt is two-surface (PROVEN LIVE: the shipped MacBook order surfaced all 3 of
its serials, where 5 pre-fulfillment August captures had shown none — serials appear once the
units ship):

  1. The ss-api order JSON, walked for any key matching /serial/i — the structured source,
     and the only one that can ATTRIBUTE a serial to its package (each item names its own
     trackingNumber; see serials_by_tracking).
  2. The rendered order-details page, scanned for "Serial Number"-labelled text — a fallback
     that can't attribute, so its finds are only usable on a single-package order.

Both extractors are PURE and offline-tested (tests/test_bestbuy_serials.py); only the thin
CDP driver needs a live session. FINDING NOTHING IS A RESULT, NOT AN ERROR: the caller
(respond_bfmr) turns an empty answer into an ACTION NEEDED alert naming the order, and the
first live run's capture is what pins the real location (scripts/bestbuy_serial_probe.py is
the richer recon for that). Never guess a serial.

Rides the deterministic Best Buy session machinery (scrapers/bestbuy_api.py): CDP attach,
self-healing password login, in-page fetch with the session cookie. Spends one CDP-browser fee
per call.
"""

import json
import logging
import re

from scrapers.base import ApiLoginError
from scrapers.bestbuy_api import (
    ORDER_DETAIL_PATH,
    _deterministic_login,
    _looks_logged_out,
)
from scrapers.cdp import CdpBrowser

log = logging.getLogger(__name__)

ORDER_DETAILS_PAGE_URL = "https://www.bestbuy.com/profile/ss/orders/order-details/{}/view"

#: A serial as labelled text on the page: the word, an optional "Number"/"#", a separator, then
#: the value — 6+ chars, letters/digits/dashes, at least one digit (rules out "Serial Number"
#: itself and prose). Case-insensitive on the label, tolerant of markup collapsing to spaces.
_LABELLED_SERIAL = re.compile(
    r"serial(?:\s*(?:number|no\.?|#))?\s*[:#]?\s*([A-Z0-9][A-Z0-9-]{5,29})",
    re.IGNORECASE,
)

#: Payload keys that mean "this value is a serial": serialNumber, serial_number, itemSerial, ...
_SERIAL_KEY = re.compile(r"serial", re.IGNORECASE)


def serials_from_payload(payload) -> list[str]:
    """Every string value sitting under a /serial/i key anywhere in the JSON, in document order,
    deduplicated. Pure."""
    found: dict[str, None] = {}

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if _SERIAL_KEY.search(str(key)):
                    for v in (value if isinstance(value, list) else [value]):
                        if isinstance(v, str) and v.strip():
                            found.setdefault(v.strip(), None)
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return list(found)


#: The key under which serials attributed to no particular package are returned. A combined box
#: often holds only PART of an order (BBY01-809900000010 shipped 2+1 under two numbers), so an
#: unattributed serial is only usable when the order has a single tracking number.
UNATTRIBUTED = ""


def _tracking_numbers_in(node) -> list[str]:
    """Every trackingNumber value nested anywhere under `node`, in document order."""
    found: dict[str, None] = {}

    def walk(n):
        if isinstance(n, dict):
            for key, value in n.items():
                if key == "trackingNumber" and isinstance(value, str) and value.strip():
                    found.setdefault(value.strip(), None)
                walk(value)
        elif isinstance(n, list):
            for value in n:
                walk(value)

    walk(node)
    return list(found)


def serials_by_tracking(payload) -> dict[str, list[str]]:
    """{tracking number: [serials]} from one ss-api order payload, plus UNATTRIBUTED ("") for
    serials the payload doesn't tie to a package. Pure.

    THE CORRELATION IS STRUCTURAL, never positional: an `order.items[]` entry carries both its
    serials and its own `fulfillment.tracking.trackingNumber`, so a serial found INSIDE an item
    that names exactly one tracking number belongs to that package. An item naming zero or
    several tracking numbers contributes to UNATTRIBUTED instead — guessing would send wrong
    serials to BFMR. Serials outside any item (order level) are UNATTRIBUTED too.
    """
    out: dict[str, dict[str, None]] = {}

    def add(tracking: str, serials: list[str]) -> None:
        bucket = out.setdefault(tracking, {})
        for s in serials:
            bucket.setdefault(s, None)

    orders = payload if isinstance(payload, list) else [payload]
    claimed: set[str] = set()
    for entry in orders:
        order = entry.get("order") if isinstance(entry, dict) else None
        items = (order or {}).get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            serials = serials_from_payload(item)
            if not serials:
                continue
            trackings = _tracking_numbers_in(item)
            add(trackings[0] if len(trackings) == 1 else UNATTRIBUTED, serials)
            claimed.update(serials)

    leftovers = [s for s in serials_from_payload(payload) if s not in claimed]
    if leftovers:
        add(UNATTRIBUTED, leftovers)
    return {tracking: list(serials) for tracking, serials in out.items()}


def serials_from_html(html: str) -> list[str]:
    """Serial values the page labels as such, in document order, deduplicated. Pure.

    Works on visible text: tags are stripped first so "Serial Number:</span> <span>XXX" still
    reads as one labelled pair. The label requirement is what keeps this from harvesting SKUs
    and order ids — an unlabelled token is never trusted as a serial.
    """
    text = re.sub(r"<[^>]+>", " ", html)
    found: dict[str, None] = {}
    for match in _LABELLED_SERIAL.finditer(text):
        value = match.group(1)
        if any(ch.isdigit() for ch in value):
            found.setdefault(value, None)
    return list(found)


def fetch_serials(profile, order_ids: list[str]) -> dict[str, dict[str, list[str]]]:
    """{order_id: {tracking number: [serials], UNATTRIBUTED: [...]}} from one CDP session.

    PER PACKAGE, because one order routinely ships as several packages and a combined box holds
    only some of them — the ss-api ties each item to its own trackingNumber, so
    the correlation is structural (serials_by_tracking). Page-scanned serials can't be tied to
    a package and land under UNATTRIBUTED; the caller decides when those are safe to use (only
    when the order has a single tracking number).

    An order the hunt finds nothing for maps to {} — the caller decides how loud to be.
    Raises ApiLoginError when the session can't be established (same contract as the scraper).
    """
    results: dict[str, dict[str, list[str]]] = {oid: {} for oid in order_ids}
    if not order_ids:
        return results

    with CdpBrowser(profile) as page:
        page.goto(ORDER_DETAILS_PAGE_URL.format(order_ids[0]),
                  wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        if _looks_logged_out(page):
            auth = profile.auth.get("bestbuy")
            outcome = _deterministic_login(page, auth)
            if not outcome.ok:
                raise ApiLoginError(outcome.reason or
                                    "Best Buy session is logged out and deterministic login "
                                    "did not succeed.")
            page.goto(ORDER_DETAILS_PAGE_URL.format(order_ids[0]),
                      wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

        for oid in order_ids:
            # Surface 1: the ss-api payload, in-page so it rides the session cookie. This is
            # the surface that can ATTRIBUTE a serial to its package.
            try:
                raw = page.evaluate(
                    """async (p) => { const r = await fetch(p, {credentials: 'include',
                         headers: {accept: 'application/json'}}); return await r.text(); }""",
                    ORDER_DETAIL_PATH.format(oid))
                payload = json.loads(raw)
            except Exception:
                log.warning("ss-api fetch for %s failed while hunting serials", oid, exc_info=True)
                payload = None
            per_tracking = serials_by_tracking(payload) if payload is not None else {}
            claimed = {s for serials in per_tracking.values() for s in serials}

            # Surface 2: the rendered order-details page. The DOM can't tie a serial to a
            # package, so anything new lands under UNATTRIBUTED.
            try:
                if not page.url.startswith(ORDER_DETAILS_PAGE_URL.format(oid).split("?")[0]):
                    page.goto(ORDER_DETAILS_PAGE_URL.format(oid),
                              wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(3000)
                for _ in range(3):  # lazy-rendered sections
                    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(1200)
                loose = [s for s in serials_from_html(page.content()) if s not in claimed]
                if loose:
                    per_tracking.setdefault(UNATTRIBUTED, [])
                    per_tracking[UNATTRIBUTED].extend(
                        s for s in loose if s not in per_tracking[UNATTRIBUTED])
            except Exception:
                log.warning("order-details page scan for %s failed", oid, exc_info=True)

            results[oid] = per_tracking
            total = sum(len(v) for v in per_tracking.values())
            log.info("Best Buy serial hunt for %s: %d found (%d package(s) attributed).",
                     oid, total, sum(1 for k in per_tracking if k != UNATTRIBUTED))

    return results
