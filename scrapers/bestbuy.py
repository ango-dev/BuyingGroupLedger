import logging

import diagnostics

from alerts.notifier import alert
from config.settings import settings
from scrapers.base import ApiLoginError, BaseRetailerScraper, LoggedOutError
from scrapers.bestbuy_api import DIAGNOSTIC_SELECTORS

log = logging.getLogger(__name__)

class BestBuyScraper(BaseRetailerScraper):
    retailer_name = "Best Buy"
    retailer_key = "bestbuy"
    # Audited against the captured page by the failure dossier (see BaseRetailerScraper).
    diagnostic_selectors = DIAGNOSTIC_SELECTORS
    # PRIMARY PATH: Best Buy's own private endpoints (deterministic, no agent) — see scrape() ->
    # _scrape_via_api and scrapers/bestbuy_api.py. A CDP browser holds the logged-in cookie; discovery
    # reads the purchase-history page's embedded flight data and each order's detail comes from an
    # in-page fetch of /profile/ss/api/v1/orders/<id>. On ANY failure scrape() writes a failure
    # dossier, alerts with its path, and records nothing this run (see BaseRetailerScraper).

    def scrape(self):
        """Run the deterministic ss-api path; on ANY failure write a failure dossier.

        A successful API call that finds no orders returns [] (not an error). A non-login failure
        goes to `_on_deterministic_failure`: dossier + alert + DeterministicPathError, nothing recorded.
        """
        with self._collecting() as dossier:
            try:
                items = self._scrape_via_api()
            except ApiLoginError as exc:
                # A login failure is not a page-shape change a code fix can address — alert and
                # skip so the user re-logs in the profile.
                log.warning("Best Buy [%s]: API login failed (%s); skipping.",
                            self.profile.label, exc)
                alert(
                    f"Best Buy [{self.profile.label}]: session logged out — API login failed, "
                    f"not recorded this run",
                    f"The Best Buy deterministic path could not sign in ({exc}). Re-login the profile "
                    f"(scripts/create_profile)."
                    + self._dossier_line(dossier, exc),
                )
                raise LoggedOutError(f"Best Buy:{self.profile.label}") from exc
            except Exception as exc:  # noqa: BLE001 — a NON-login failure (page shape)
                return self._on_deterministic_failure(
                    exc, dossier,
                    hint="If this persists, check the sign-in flow / page shape "
                         "(scripts/bestbuy_capture.py re-captures it).",
                )
            self._report_soft_problems(dossier)
            return items

    def _scrape_via_api(self):
        from scrapers.bestbuy_api import BestBuyApiClient, BestBuyApiError
        from scrapers.bestbuy_mapping import PayloadShapeError, build_order_items

        state = self._load_order_state()
        open_ids = {o["order_id"] for o in state.get("open_orders", [])}
        terminal_ids = set(state.get("delivered_ids", [])) | set(state.get("cancelled_ids", []))
        _, since, _ = self._date_window()

        client = BestBuyApiClient(self.profile)
        payloads = client.fetch_order_payloads(since, open_ids, terminal_ids)
        try:
            items = build_order_items(payloads, self.profile.label, known_open_ids=frozenset(open_ids))
        except PayloadShapeError as exc:
            # The browser is closed by now; attach the payload that failed so the dossier holds it.
            diagnostics.record_response("ss-api order payload (shape)", 200, exc.payload)
            raise BestBuyApiError(str(exc)) from exc
        log.info("Best Buy [%s]: built %d ledger row(s) from the ss-api.", self.profile.label, len(items))
        return items
