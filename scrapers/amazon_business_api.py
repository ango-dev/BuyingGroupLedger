"""Amazon Business order data over its own web pages — the cheap, deterministic primary path.

STANDALONE sibling of `scrapers/amazon_api.py` (NOT a subclass) — Amazon Business gets its own client so
the consumer path it was cloned from can't regress.

No AI agent: a CDP browser (scrapers.cdp.CdpBrowser) holds the profile's logged-in cookie and the reads
ride that session. Amazon Business is Path B — server-rendered HTML, NO order JSON endpoint (live
capture 2026-08-11) — so this fetches HTML and scrapers/amazon_business_mapping.py parses it.

Three reads, one logged-in session:
  1. DISCOVERY: load `/your-orders/orders`; `discover_orders` scopes to `order-details?orderID=` links.
     Business paginates CLIENT-SIDE (hash routes `#pagination/N/` + a POST `/ab/your-orders/orderHistory`
     fragment), NOT via consumer's `?timeFilter=&startIndex=` URL params — so we CLICK-THROUGH the
     pagination control (`li.a-last a`) and re-read `page.content()` each page (validated live by
     scripts/amazon_business_paginate_probe.py). The default view is "Past 3 months" (~90 days), which
     covers every normal lookback; deep (>90-day) history isn't reachable from this view and falls to
     the agent.
  2. DETAILS: for each in-window / still-open order, load its order-details page; `build_order_items`
     turns the `#orderDetails` data-component tree into rows (identical structure to consumer Amazon).
  3. TRACKING NUMBER: on the package-tracking ("pt") page (the "Track package" link is still a consumer
     `/gp/your-account/ship-track…`), read via the scraper's `read_tracking_page`, then rebuild rows so
     the `_shipped_requires_tracking` invariant promotes them to `shipped`.

A lapsed session SIGNS ITSELF BACK IN (scrapers/amazon_business_signin.py) when the profile carries
an `auth["amazon-business"]` block, answering Amazon's authenticator challenge with a code generated
on this host. That reverses the original "no deterministic re-login (Amazon has OTP/2FA)" rule, which
made this the one retailer that could not heal itself — the design notes watched it cost two consecutive
runs. Without an auth block the behaviour is unchanged: raise and let the caller alert and skip.

EITHER WAY THE PAID AGENT IS NEVER RUN FOR A LOGIN FAILURE — it cannot fix auth, so it would only
spend money rediscovering the logout. `ApiLoginError` is what carries that distinction.
"""

import logging

from config.cards import boosted_last4s, load_cards
from config.settings import settings
from scrapers.amazon_business_mapping import RETAILER, build_order_items, discover_orders, parse_shipment_targets
from scrapers.amazon_business_signin import deterministic_login, looks_logged_out
from scrapers.base import ApiLoginError
from scrapers.cdp import CdpBrowser
from models.order import TERMINAL_STATUSES

log = logging.getLogger(__name__)

# The business order-history SPA lives here; its default filter is "Past 3 months" (~90 days).
ORDER_HISTORY_URL = "https://www.amazon.com/your-orders/orders"
ORDER_DETAILS_URL = "https://www.amazon.com/gp/css/order-details?orderID={}"  # redirects to /your-orders
_MAX_PAGES = 40  # hard stop so a layout change can't loop forever (~400 orders)
#: The key an auth block is filed under — the scraper's `retailer_key`, not its display name.
_AUTH_KEY = "amazon-business"

# Things the page shows only when it wants you to sign in. Used ONLY as corroboration once discovery
# has already found zero orders -- deliberately NOT folded into looks_logged_out, because a false
# positive THERE would send a perfectly good session into a doomed self-login. Here the run is
# failing either way and the only open question is whether to spend money on the agent, so the safe
# direction is to assume logged out.
_SIGNIN_CTA_SELECTORS = ("#ap_email", "#ap_password", "#ap-claim", "a[href*='/ap/signin']")


class AmazonBusinessApiError(Exception):
    """The deterministic path could not run (logged out, page shape changed, pagination stuck,
    network) — the caller should fall back to the agent."""


def _signin_affordances(page) -> list[str]:
    """Which sign-in CTAs the page is showing, if any.

    A silently-expired session and a genuine markup change look IDENTICAL at the point discovery
    comes back empty, and they have opposite correct responses: a logout must skip for free, while a
    shape change is exactly what the paid agent is for. Best Buy proved the cost of not telling them
    apart: the logged-out page tripped neither the URL check nor the form selectors,
    the agent ran, and $0.02 bought the conclusion "logged out" that was already available for free.
    """
    found = []
    for selector in _SIGNIN_CTA_SELECTORS:
        try:
            if page.locator(selector).count() > 0:
                found.append(selector)
        except Exception:  # noqa: BLE001 — a selector engine hiccup must not mask the real failure
            continue
    return found


class AmazonBusinessApiClient:
    def __init__(self, profile, tracking_reader=None):
        # tracking_reader: callable(page) -> {status, tracking_number, ...} | None. In production this
        # is AmazonBusinessScraper.read_tracking_page (the validated .pt-delivery-card-trackingId
        # selectors); kept injectable so the client needs no scraper import and stays easy to test.
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
                raise AmazonBusinessApiError("No orders found on the order-history page (shape changed?).")

            to_fetch = [
                oid for oid, date in dates.items()
                if oid not in terminal_ids and (oid in open_ids or not date or date >= since_date)
            ]
            for oid in open_ids:
                if oid not in to_fetch and oid not in terminal_ids:
                    to_fetch.append(oid)

            log.info("Amazon Business [%s]: %d order(s) in window, fetching %d order-details page(s).",
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
                    log.warning("Amazon Business [%s]: failed to load order-details for %s (skipped).",
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
        paid agent is never run — it cannot fix an auth failure. What differs is the MESSAGE, and
        that matters more than it looks: a missing auth block, a rejected password, an unanswerable
        SMS challenge and a network-layer rejection need four different responses from whoever reads
        the alert, and they are indistinguishable without being told.
        """
        auth = (self.profile.auth or {}).get(_AUTH_KEY)
        if auth is None:
            raise ApiLoginError(
                "Amazon Business session is logged out and this profile has no auth block, so there "
                "is nothing to sign in with. Either add auth['amazon-business'] (method/username/"
                "password/totp_secret) to config.json, or re-login by hand with "
                "`python -m scripts.create_profile`. The agent is NOT run for a login failure — it "
                "cannot fix auth."
            )

        log.info("Amazon Business [%s]: session logged out; attempting deterministic self-login.",
                 self.profile.label)
        outcome = deterministic_login(page, auth)
        if not outcome.ok:
            log.warning("Amazon Business [%s]: deterministic self-login did not succeed.",
                        self.profile.label)
            # Lead with the classified reason when the page told us one — an SMS challenge, a
            # rejected password and an anti-bot reset all end here, and the alert is useless unless
            # it says WHICH.
            raise ApiLoginError(
                outcome.reason
                or ("Amazon Business session is logged out and deterministic login did not succeed; "
                    "the auth requests died at the NETWORK layer (anti-bot/transport, not the page "
                    "flow — changing egress does not help)."
                    if outcome.transport_failed else
                    "Amazon Business session is logged out and deterministic login did not succeed.")
            )
        log.info("Amazon Business [%s]: deterministic self-login succeeded.", self.profile.label)
        page.goto(ORDER_HISTORY_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

    def _discover(self, page, since_date: str, today: str) -> dict[str, str]:
        """{order_id: order_date} for every order back to `since_date`, CLICK-THROUGH paginating the
        business order-history SPA.

        Business is newest-first and paginates client-side, so we read a page, then click the "next"
        control (`li.a-last a`) and re-read, stopping once a full page is entirely older than
        `since_date` (everything past it is older too) or a page adds no new ids. Signs itself back
        in if the very first page is logged out."""
        page.goto(ORDER_HISTORY_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        attempted_login = False
        if looks_logged_out(page):
            self._sign_in_here(page)
            attempted_login = True

        dates = self._read_paginated_history(page, since_date)

        # DISCOVERY CAME BACK EMPTY. Two causes, opposite responses, and the parse alone cannot tell
        # them apart. A logout must never reach the agent (it cannot fix auth, and costs real money
        # to confirm what is already known); a real markup change is precisely what the agent is for.
        if not dates and (affordances := _signin_affordances(page)):
            log.info("Amazon Business [%s]: no orders parsed and the page is offering to sign in "
                     "(%s) — treating as a lapsed session, not a shape change.",
                     self.profile.label, ", ".join(affordances))
            if not attempted_login:
                # The session expired without tripping looks_logged_out. Recovering it here turns a
                # lost run into a normal one.
                self._sign_in_here(page)
                attempted_login = True
                dates = self._read_paginated_history(page, since_date)
            if not dates:
                raise ApiLoginError(
                    "Amazon Business order history is empty and still showing a sign-in prompt; the "
                    "session is logged out. Not running the agent — it cannot fix an auth failure."
                )

        log.info("Amazon Business [%s]: discovered %d order(s) across paginated history.",
                 self.profile.label, len(dates))
        return dates

    def _read_paginated_history(self, page, since_date: str) -> dict[str, str]:
        """Walk the click-through history from the page currently loaded.

        Split out of `_discover` so it can be run a SECOND time after a late-detected logout is
        healed — otherwise recovering the session would still return the empty result parsed before
        signing in.
        """
        dates: dict[str, str] = {}
        for _ in range(_MAX_PAGES):
            page_dates = discover_orders(page.content())
            if not page_dates:
                break
            new_ids = [oid for oid in page_dates if oid not in dates]
            dates.update(page_dates)
            # Stop once a whole page is older than the window (dates are newest-first); a page with no
            # new ids (all already seen) also means we've caught up / the list didn't advance.
            page_all_old = bool(page_dates) and all(
                d and d < since_date for d in page_dates.values()
            )
            if page_all_old or not new_ids:
                break
            if not self._go_next_page(page):
                break  # last page
        return dates

    def _go_next_page(self, page) -> bool:
        """Advance the order-history SPA to the next page by clicking its pagination control.

        Returns True if it advanced, False if there is no next page (we're on the last page — the
        `li.a-last` becomes `a-disabled` with no anchor). Raises AmazonBusinessApiError if a next
        control exists but the rendered order list never swaps — better to fall back to the agent
        (which re-scans everything) than to silently miss the orders on pages we couldn't reach."""
        try:
            nxt = page.locator("li.a-last:not(.a-disabled) a")
            if nxt.count() == 0:
                return False  # last page
            before_first = next(iter(discover_orders(page.content())), None)
            nxt.first.click(timeout=10000)
        except Exception as exc:
            raise AmazonBusinessApiError(f"order-history pagination click failed: {exc}") from exc

        for _ in range(30):  # up to ~15s for the client-side list swap to land
            page.wait_for_timeout(500)
            now_first = next(iter(discover_orders(page.content())), None)
            if now_first and now_first != before_first:
                page.wait_for_timeout(1000)  # let the rest of the page settle
                return True
        raise AmazonBusinessApiError("order-history pagination did not advance (list never changed).")

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
                    log.warning("Amazon Business [%s]: tracking read failed for %s / %s.",
                                self.profile.label, oid, target["shipment"], exc_info=True)
                    info = None
                if info and info.get("tracking_number"):
                    result.setdefault(oid, {})[target["shipment"]] = info["tracking_number"]
                    log.info("Amazon Business [%s]: read tracking %s for %s / %s.",
                             self.profile.label, info["tracking_number"], oid, target["shipment"])
        return result
