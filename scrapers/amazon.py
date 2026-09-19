import logging

from alerts.notifier import alert
from config.settings import settings
from scrapers import amazon_mapping
from scrapers.base import ApiLoginError, BaseRetailerScraper, LoggedOutError

log = logging.getLogger(__name__)


class AmazonScraper(BaseRetailerScraper):
    retailer_name = "Amazon"
    retailer_key = "amazon"
    # PRIMARY PATH: Amazon's own web pages read deterministically (no agent, no tokens — just a CDP
    # browser holding the logged-in cookie). Amazon has no order JSON endpoint (unlike Best Buy's
    # ss-api), so scrape() -> _scrape_via_api -> scrapers/amazon_api.py fetches HTML and
    # scrapers/amazon_mapping.py parses it; the pt page supplies each shipment's tracking number via
    # the same read_tracking_page selectors below. On ANY failure scrape() writes a failure dossier,
    # alerts with its path, and records nothing this run (see BaseRetailerScraper).

    def scrape(self):
        """Run the deterministic web-page path; on ANY failure write a failure dossier.

        A successful run that finds no orders returns [] (not an error). A non-login failure goes
        to `_on_deterministic_failure`: dossier + alert + DeterministicPathError, nothing recorded.
        """
        with self._collecting() as dossier:
            try:
                items = self._scrape_via_api()
            except ApiLoginError as exc:
                # A login failure is not a page-shape change a code fix can address — alert and
                # skip so the user re-logs in the profile.
                log.warning("Amazon [%s]: session logged out (%s); skipping.",
                            self.profile.label, exc)
                alert(
                    f"Amazon [{self.profile.label}]: session logged out — not recorded this run",
                    f"The Amazon deterministic path found a logged-out session ({exc}). Re-login the "
                    f"profile (scripts/create_profile)."
                    + self._dossier_line(dossier, exc),
                )
                raise LoggedOutError(f"Amazon:{self.profile.label}") from exc
            except Exception as exc:  # noqa: BLE001 — a NON-login failure (page shape)
                return self._on_deterministic_failure(
                    exc, dossier,
                    hint="If this persists, re-capture the page shape (scripts/amazon_capture.py) "
                         "or check whether the session is logged out.",
                )
            self._report_soft_problems(dossier)
            return items

    def _scrape_via_api(self):
        from scrapers.amazon_api import AmazonApiClient

        state = self._load_order_state()
        open_ids = {o["order_id"] for o in state.get("open_orders", [])}
        terminal_ids = set(state.get("delivered_ids", [])) | set(state.get("cancelled_ids", []))
        today, since, _ = self._date_window()

        client = AmazonApiClient(self.profile, tracking_reader=self.read_tracking_page)
        items = client.fetch_order_items(since, open_ids, terminal_ids, today=today)
        log.info("Amazon [%s]: built %d ledger row(s) from the deterministic path.",
                 self.profile.label, len(items))
        return items

    # The order-details page carries structure (how many shipments) but not the tracking number —
    # Amazon shows that only on its package-tracking ("pt") page. amazon_api reads each non-terminal
    # shipment's pt page with the selectors below (injected as `tracking_reader`), one read per
    # shipment, so a split order gets every one of its tracking pages read.

    # Stable, semantically-named selectors on Amazon's package-tracking ("pt") page. Also reusable
    # by Amazon Business, which shares the same tracking page. The carrier tracking number lives in
    # a "Delivery Info" card that Amazon renders ONLY once the shipment has shipped — so the card's
    # presence is the "has shipped" signal and the number sits in a labelled child of it. (These
    # replace an earlier deep nth-child path that pointed at an always-empty node and never read a
    # number, which is why a shipped order kept reading as 'ordered'.)
    promise_selector = "h1.pt-promise-main-slot"
    delivery_card_selector = ".pt-delivery-card-wrapper"
    tracking_number_selector = ".pt-delivery-card-trackingId"  # text reads "Tracking ID: <number>"
    # Pre-estimate state — twin of scrapers/amazon_business.py (where the dossier caught it live,
    # 2026-09-04): before Amazon has a delivery estimate the pt page has NO pt-* elements, just
    # this container reading "Order received".
    preship_promise_selector = ".promise-container-inner"
    # The same legacy layout once the package has SHIPPED: still no pt-* elements; the carrier card is a "Shipped
    # with Amazon" widget whose h4 reads "Tracking ID: <number>". Twin in the other Amazon scraper.
    legacy_tracking_number_selector = ".carrierRelatedInfo-trackingId-text"

    # Audited against the captured page by the failure dossier (see BaseRetailerScraper).
    diagnostic_selectors = {
        **amazon_mapping.SELECTORS,
        "pt_promise_headline": promise_selector,
        "pt_delivery_card": delivery_card_selector,
        "pt_tracking_number": tracking_number_selector,
        "pt_preship_promise": preship_promise_selector,
        "pt_legacy_tracking_number": legacy_tracking_number_selector,
        "signin_email": "#ap_email",
        "signin_password": "#ap_password",
        "signin_claim": "#ap-claim",
        "signin_submit": "#signInSubmit",
        "signin_link": "a[href*='/ap/signin']",
    }

    def _read_tracking_number(self, page, selector: str | None = None) -> str:
        """The carrier number from the delivery card, or "" if that element isn't present.

        The element's text reads like "Tracking ID: TBA999000000001"; strip the label and keep the
        number. Kept separate so read_tracking_page can tell "no number element" from "no number".
        `selector` defaults to the pt card's; the legacy layout passes its own.
        """
        el = page.query_selector(selector or self.tracking_number_selector)
        if el is None:
            return ""
        raw = el.inner_text().strip()
        return raw.split(":", 1)[1].strip() if ":" in raw else raw

    def _read_legacy_layout(self, page) -> dict | None:
        """The LEGACY tracking layout — none of the pt-* elements, the promise in
        `.promise-container-inner` — in the two states seen live, each gated on the evidence that
        names it. Any other layout still returns None and fails loudly (a shipped order must never
        quietly read as unshipped). Twin of the other Amazon scraper.

        SHIPPED: the
        promise reads "Estimated to arrive by <date>" and a "Shipped with Amazon" widget carries
        "Tracking ID: <number>" — the number itself is the proof it shipped. Delivered is stated by
        the promise, as on the pt layout.

        PRE-ESTIMATE: before Amazon has a delivery
        estimate the container reads "Order received" and there is no carrier card — a legitimate
        'ordered', on that exact wording only.
        """
        early = page.query_selector(self.preship_promise_selector)
        if early is None:
            return None  # unexpected layout / not the tracking page → dossier problem
        promise = early.inner_text().strip()
        tracking_number = self._read_tracking_number(page, self.legacy_tracking_number_selector)
        if tracking_number:
            status = "delivered" if "delivered" in promise.lower() else "shipped"
            return {"status": status, "tracking_number": tracking_number, "delivery_promise": promise}
        if "order received" in promise.lower():
            return {"status": "ordered", "tracking_number": "", "delivery_promise": promise}
        return None  # an estimate with no number, or a carrier card with an empty one → loud

    def read_tracking_page(self, page) -> dict | None:
        """Read status/tracking#/delivery-promise from a loaded Amazon tracking page via selectors.

        Returns None to tell the caller the page could not be read — either it doesn't look like
        a tracking page (the promise headline is missing), or the shipment HAS shipped (the Delivery
        Info card is present) but no tracking number can be pulled out of it: a selector that has
        gone stale under us. The caller records that as a dossier problem; reporting 'ordered'
        silently would keep a shipped order looking unshipped forever.

        Crucially, a shipment that simply hasn't shipped yet has NO delivery card at all (the page
        shows only an "Arriving <x>" estimate with the stepper at "Ordered"); that is a legitimate
        'ordered', not a failure, and must not be flagged — otherwise every un-shipped order would
        raise a false alarm every run.
        """
        promise_el = page.query_selector(self.promise_selector)
        if promise_el is None:
            return self._read_legacy_layout(page)
        promise = promise_el.inner_text().strip()
        lowered = promise.lower()

        card = page.query_selector(self.delivery_card_selector)
        tracking_number = self._read_tracking_number(page) if card is not None else ""

        # Delivered is stated by the promise headline and is terminal; record it with whatever number
        # is present (blank is fine — a prior run usually captured it and _merge_row preserves it).
        if "delivered" in lowered:
            return {"status": "delivered", "tracking_number": tracking_number, "delivery_promise": promise}

        if card is None:
            # Not shipped yet — no delivery card, so no number. Legitimate 'ordered', not a miss.
            return {"status": "ordered", "tracking_number": "", "delivery_promise": promise}

        if not tracking_number:
            # Shipped (card present) but the number selector came back empty → it went stale.
            return None

        return {"status": "shipped", "tracking_number": tracking_number, "delivery_promise": promise}
