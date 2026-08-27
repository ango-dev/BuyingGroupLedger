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
from datetime import date

from config.cards import boosted_last4s, load_cards
from config.settings import settings
from scrapers.amazon_mapping import RETAILER, build_order_items, discover_orders, parse_shipment_targets
from scrapers.amazon_signin import deterministic_login, looks_logged_out
from scrapers.base import ApiLoginError
from scrapers.cdp import CdpBrowser
from models.order import TERMINAL_STATUSES

log = logging.getLogger(__name__)

# Order history PAGINATES (10 orders/page) via ?timeFilter=<tf>&startIndex=<n> — it does NOT
# infinite-scroll, so reading only the first page silently MISSES every older in-window order (a
# heavy buyer can have 18 orders in 30 days across 2+ pages). `timeFilter` buckets: last30 / months-3
# (default, ~90d) / year-YYYY. We page through the smallest bucket(s) covering `since`.
ORDER_HISTORY_PAGE = "https://www.amazon.com/your-orders/orders?timeFilter={tf}&startIndex={start}"
_PAGE_SIZE = 10
_MAX_PAGES_PER_FILTER = 40  # hard stop so a layout change can't loop forever (~400 orders)
ORDER_DETAILS_URL = "https://www.amazon.com/gp/css/order-details?orderID={}"
_SIGNIN_MARKERS = ("/ap/signin", "/ap/mfa", "/ap/cvf", "signin")
#: The key an auth block is filed under -- the scraper's `retailer_key`, not its display name.
_AUTH_KEY = "amazon"

# Things the page shows only when it wants you to sign in. Used ONLY as corroboration once discovery
# has already found zero orders -- deliberately NOT folded into _looks_logged_out, because a false
# positive THERE would send a perfectly good session into a doomed self-login. Here the run is
# failing either way and the only open question is whether to spend money on the agent, so the safe
# direction is to assume logged out.
_SIGNIN_CTA_SELECTORS = ("#ap_email", "#ap_email_login", "#ap_password", "#ap-claim",
                         "a[href*='/ap/signin']")


def _signin_affordances(page) -> list[str]:
    """Which sign-in CTAs the page is showing, if any.

    A silently-expired session and a genuine markup change look IDENTICAL at the point discovery
    comes back empty, and they have opposite correct responses: a logout must skip for free, while a
    shape change is exactly what the paid agent is for.
    """
    found = []
    for selector in _SIGNIN_CTA_SELECTORS:
        try:
            if page.locator(selector).count() > 0:
                found.append(selector)
        except Exception:  # noqa: BLE001 -- a selector engine hiccup must not mask the real failure
            continue
    return found


def _time_filters_for(since_date: str, today: str) -> list[str]:
    """The order-history `timeFilter` bucket(s), newest-first, that cover [since_date, today].

    `months-3` covers ~90 days (the default view) and handles every normal lookback in one bucket;
    only a window older than 90 days falls back to per-year buckets."""
    try:
        since = date.fromisoformat(since_date)
        end = date.fromisoformat(today)
    except (ValueError, TypeError):
        return ["months-3"]
    if (end - since).days <= 90:
        return ["months-3"]
    return [f"year-{y}" for y in range(end.year, since.year - 1, -1)]


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
            dates = self._discover(page, since_date, today)
            if not dates:
                raise AmazonApiError("No orders found on the order-history page (shape changed?).")

            to_fetch = [
                oid for oid, date in dates.items()
                if oid not in terminal_ids and (oid in open_ids or not date or date >= since_date)
            ]
            for oid in open_ids:
                if oid not in to_fetch and oid not in terminal_ids:
                    to_fetch.append(oid)

            log.info("Amazon [%s]: %d order(s) in window, fetching %d order-details page(s).",
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
        # A gift card bought on a card with an explicit Amazon rate in cards.json is funding
        # inventory, so its purchase has to land on the ledger; on any other card it is personal
        # spending and the mapping drops it. See scrapers/*_mapping._skip_digital.
        keep_digital = boosted_last4s(RETAILER, load_cards())
        rows = []
        for oid, html in details_html.items():
            rows.extend(build_order_items(
                html, self.profile.label, known_open_ids=frozenset(open_ids),
                tracking_by_shipment=tracking_by_order.get(oid), today=today,
                net_gift_cards=settings.amazon_gift_card_netting_enabled,
                keep_digital_last4s=keep_digital,
            ))

        # Final keep filter using the authoritative per-order date already parsed into the rows.
        kept = []
        for r in rows:
            if r.order_id in open_ids or not r.order_date or r.order_date >= since_date:
                kept.append(r)
        return kept

    def _sign_in_here(self, page) -> None:
        """Self-heal a lapsed session, raising the right error if it can't.

        Every failure route out of here is `ApiLoginError`, so the caller alerts and SKIPS and the
        paid agent is never run -- it cannot fix an auth failure. What differs is the MESSAGE: a
        missing auth block, a rejected password, an unanswerable SMS challenge and a network-layer
        rejection need four different responses from whoever reads the alert.

        **This profile must never sign into the BUSINESS account.** CLAUDE.md keeps the two Amazon
        accounts on separate profiles precisely to avoid linking them, and Amazon's "Switch accounts"
        page is the one place they can bleed together -- it offers remembered accounts by display
        name, and this user's two accounts share one. `amazon_signin.handle_account_switcher` matches
        on the configured username and refuses on ambiguity, which is what keeps that guarantee.
        """
        auth = (self.profile.auth or {}).get(_AUTH_KEY)
        if auth is None:
            raise ApiLoginError(
                "Amazon session is logged out and this profile has no auth block, so there is "
                "nothing to sign in with. Either add auth['amazon'] (method/username/password/"
                "totp_secret) to config.json, or re-login by hand with "
                "`python -m scripts.create_profile`. The agent is NOT run for a login failure -- it "
                "cannot fix auth."
            )

        log.info("Amazon [%s]: session logged out; attempting deterministic self-login.",
                 self.profile.label)
        # Pass our own auth key: the sign-in module is shared by both Amazon accounts, and its
        # alerts tell a human WHICH config block to edit. Naming the wrong one sends the fix
        # to the wrong profile.
        outcome = deterministic_login(page, auth, _AUTH_KEY)
        if not outcome.ok:
            log.warning("Amazon [%s]: deterministic self-login did not succeed.", self.profile.label)
            raise ApiLoginError(
                outcome.reason
                or ("Amazon session is logged out and deterministic login did not succeed; the auth "
                    "requests died at the NETWORK layer (anti-bot/transport, not the page flow -- "
                    "changing egress does not help)."
                    if outcome.transport_failed else
                    "Amazon session is logged out and deterministic login did not succeed.")
            )
        log.info("Amazon [%s]: deterministic self-login succeeded.", self.profile.label)

    def _discover(self, page, since_date: str, today: str) -> dict[str, str]:
        """{order_id: order_date} for every order back to `since_date`, PAGINATING order history.

        Amazon paginates 10 orders/page (newest-first) — not infinite scroll — so we walk
        `startIndex=0,10,20,…` within each timeFilter bucket, stopping a bucket once a full page is
        entirely older than `since_date` (everything past it is older too). Raises if the very first
        page is logged out."""
        dates: dict[str, str] = {}
        first = True
        for tf in _time_filters_for(since_date, today):
            for pageno in range(_MAX_PAGES_PER_FILTER):
                url = ORDER_HISTORY_PAGE.format(tf=tf, start=pageno * _PAGE_SIZE)
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                if first:
                    first = False
                    if _looks_logged_out(page):
                        self._sign_in_here(page)
                        # Re-load THIS page: the goto above landed on a sign-in form, so its content
                        # is the login page, not orders. Without this the first bucket parses zero
                        # and the run silently under-fetches.
                        page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(3000)
                page_dates = discover_orders(page.content())
                if not page_dates:
                    break  # past the last page of this bucket
                new_ids = [oid for oid in page_dates if oid not in dates]
                dates.update(page_dates)
                # Stop this bucket once a whole page is older than the window (dates are newest-first);
                # a page with no new ids (all already seen) also means we've caught up.
                page_all_old = bool(page_dates) and all(
                    d and d < since_date for d in page_dates.values()
                )
                if page_all_old or not new_ids:
                    break
        # DISCOVERY CAME BACK EMPTY. A logout that never tripped _looks_logged_out and a real markup
        # change are indistinguishable from the parse alone, and they have opposite responses: the
        # first must skip for free, the second is what the paid agent is for.
        if not dates and (affordances := _signin_affordances(page)):
            log.info("Amazon [%s]: no orders parsed and the page is offering to sign in (%s) -- "
                     "treating as a lapsed session, not a shape change.",
                     self.profile.label, ", ".join(affordances))
            raise ApiLoginError(
                "Amazon order history is empty and still showing a sign-in prompt; the session is "
                "logged out. Not running the agent -- it cannot fix an auth failure."
            )

        log.info("Amazon [%s]: discovered %d order(s) across paginated history.",
                 self.profile.label, len(dates))
        return dates

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
