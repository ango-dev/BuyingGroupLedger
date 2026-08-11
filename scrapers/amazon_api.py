"""Amazon order data over its own web pages — the cheap, deterministic primary path.

No AI agent: a CDP browser (scrapers.cdp.CdpBrowser) holds the profile's logged-in cookie, and the
reads ride that session. Amazon has NO order/shipment/cost JSON endpoint (confirmed by live capture
2026-08-10, scripts/amazon_capture.py) — its order pages are server-rendered HTML — so this fetches
HTML and scrapers/amazon_mapping.py parses it (kept pure + unit-tested offline).

Three reads, one logged-in session:
  1. DISCOVERY: load `order-history`; `discover_orders` scopes to `order-details?orderID=` links (a
     bare id regex catches junk ids from "Buy again" widgets) -> {order_id: order_date}.
  2. DETAILS: for each in-window / still-open order, load its order-details page; `build_order_items`
     turns the `#orderDetails` data-component tree into rows (per-shipment status, items, qty, cost,
     card, address, "Track package" hrefs).
  3. TRACKING NUMBER: Amazon puts the number on the package-tracking ("pt") page, not order-details.
     For each non-terminal shipment with a track link, navigate there and read the number with the
     scraper's already-validated `read_tracking_page` (`.pt-delivery-card-trackingId`), then rebuild
     the rows so the `_shipped_requires_tracking` invariant can promote them to `shipped`.

Unlike Best Buy, Amazon sessions are long-lived, so there is NO deterministic re-login here: Amazon
login has OTP/2FA. If the session has lapsed we raise `AmazonApiError`, and scrapers/amazon.py falls
back to the Browser-Use agent (which detects logged-out, alerts, and skips) — never an auto-login.
"""

import logging

from scrapers.amazon_mapping import build_order_items, discover_orders, parse_shipment_targets
from scrapers.cdp import CdpBrowser
from models.order import TERMINAL_STATUSES

log = logging.getLogger(__name__)

ORDER_HISTORY_URL = "https://www.amazon.com/gp/css/order-history"
ORDER_DETAILS_URL = "https://www.amazon.com/gp/css/order-details?orderID={}"
_SIGNIN_MARKERS = ("/ap/signin", "/ap/mfa", "/ap/cvf", "signin")


class AmazonApiError(Exception):
    """The deterministic path could not run (logged out, page shape changed, network) — the caller
    should fall back to the agent."""


def _looks_logged_out(page) -> bool:
    url = (page.url or "").lower()
    if any(m in url for m in _SIGNIN_MARKERS):
        return True
    try:
        return page.locator("#ap_email, #ap_password, input[name='email']").count() > 0
    except Exception:
        return False


class AmazonApiClient:
    def __init__(self, profile, tracking_reader=None):
        # tracking_reader: callable(page) -> {status, tracking_number, ...} | None. In production this
        # is AmazonScraper.read_tracking_page (the validated .pt-delivery-card-trackingId selectors);
        # kept injectable so the client needs no scraper import and stays easy to test.
        self.profile = profile
        self.tracking_reader = tracking_reader

    def fetch_order_items(self, since_date: str, open_ids, terminal_ids, today: str | None = None):
        """Build ledger rows for every order placed on/after `since_date` plus every still-open order,
        minus terminal ones — discovery + details + tracking numbers in one logged-in CDP session."""
        open_ids = set(open_ids or [])
        terminal_ids = set(terminal_ids or [])

        with CdpBrowser(self.profile) as page:
            page.goto(ORDER_HISTORY_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
            if _looks_logged_out(page):
                raise AmazonApiError(
                    "Amazon session is logged out. Amazon login has OTP/2FA, so the deterministic path "
                    "does not auto-login; falling back to the agent."
                )

            # Lazy-load the history so older in-window orders render into the card list.
            for _ in range(5):
                try:
                    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    pass
                page.wait_for_timeout(1200)

            dates = discover_orders(page.content())
            if not dates:
                raise AmazonApiError("No orders found on the order-history page (shape changed?).")

            to_fetch = [
                oid for oid, date in dates.items()
                if oid not in terminal_ids and (oid in open_ids or not date or date >= since_date)
            ]
            for oid in open_ids:
                if oid not in to_fetch and oid not in terminal_ids:
                    to_fetch.append(oid)

            log.info("Amazon [%s]: %d order(s) on page, fetching %d order-details page(s).",
                     self.profile.label, len(dates), len(to_fetch))
            if not to_fetch:
                return []

            # Fetch each order-details page's HTML.
            details_html: dict[str, str] = {}
            for oid in to_fetch:
                try:
                    page.goto(ORDER_DETAILS_URL.format(oid), wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(3000)
                    details_html[oid] = page.content()
                except Exception:
                    log.warning("Amazon [%s]: failed to load order-details for %s (skipped).",
                                self.profile.label, oid, exc_info=True)

            # TRACKING NUMBERS: visit each non-terminal shipment's pt page and read the number.
            tracking_by_order = self._read_tracking_numbers(page, details_html)

        # Build rows (pure) with the tracking numbers injected so the invariant promotes to 'shipped'.
        rows = []
        for oid, html in details_html.items():
            rows.extend(build_order_items(
                html, self.profile.label, known_open_ids=frozenset(open_ids),
                tracking_by_shipment=tracking_by_order.get(oid), today=today,
            ))

        # Final keep filter using the authoritative per-order date already parsed into the rows.
        kept = []
        for r in rows:
            if r.order_id in open_ids or not r.order_date or r.order_date >= since_date:
                kept.append(r)
        return kept

    def _read_tracking_numbers(self, page, details_html: dict[str, str]) -> dict[str, dict]:
        """{order_id: {shipment_label: tracking_number}} read from each non-terminal shipment's pt page.

        Skips terminal shipments (delivered/cancelled — number already recorded or irrelevant) and any
        shipment with no track link. No-ops entirely when no tracking_reader is wired in."""
        result: dict[str, dict] = {}
        if not self.tracking_reader:
            return result
        for oid, html in details_html.items():
            for target in parse_shipment_targets(html):
                if target["status"] in TERMINAL_STATUSES or not target["tracking_url"]:
                    continue
                try:
                    page.goto(target["tracking_url"], wait_until="domcontentloaded", timeout=90000)
                    page.wait_for_timeout(3000)
                    info = self.tracking_reader(page)
                except Exception:
                    log.warning("Amazon [%s]: tracking read failed for %s / %s.",
                                self.profile.label, oid, target["shipment"], exc_info=True)
                    info = None
                if info and info.get("tracking_number"):
                    result.setdefault(oid, {})[target["shipment"]] = info["tracking_number"]
                    log.info("Amazon [%s]: read tracking %s for %s / %s.",
                             self.profile.label, info["tracking_number"], oid, target["shipment"])
        return result
