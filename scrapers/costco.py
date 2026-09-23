import logging

import diagnostics
from datetime import datetime, timedelta, timezone

from alerts.notifier import alert, compose
from config.settings import settings
from scrapers.base import (
    ApiLoginError,
    BaseRetailerScraper,
    LoggedOutError,
    ScrapeUnavailableError,
)
from scrapers import costco_signin
from scrapers.costco_mapping import FIELD_SOURCES, ORDER_DETAILS_URL, PayloadShapeError, build_order_items

log = logging.getLogger(__name__)

#: Exception names meaning "the request never reached Costco" — no response was ever received.
#: Matched by NAME across the MRO rather than by importing curl_cffi, which `_scrape_via_api`
#: deliberately imports lazily so the dependency stays optional.
#:
#: `HTTPError` is pointedly absent: an HTTP status means the transport DID get through, so the fault
#: is at Costco's end — a schema/page problem worth a dossier, not a proxy fault.
#: Both spellings of each concept on purpose: curl_cffi and requests say `Timeout` / `ConnectionError`
#: while Python's builtins say `TimeoutError` / `ConnectionError`, and either can surface here.
#: Subclasses (ConnectionRefusedError, ConnectionResetError, ReadTimeout, ...) match through the MRO.
_TRANSPORT_FAILURE_NAMES = frozenset({
    "ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout", "TimeoutError",
    "ProxyError", "SSLError",
})


def _is_shared_proxy_failure(exc: Exception, proxy) -> bool:
    """Did this fail at the transport, through the configured proxy — a fault, not a shape change?

    Only when a proxy is CONFIGURED. That is the whole condition: the API path egresses through the
    profile's static ISP proxy (`CostcoApiClient(..., proxy=self.profile.proxy)`), so a transport
    error there means the request never reached Costco. Blaming a selector — or, worse, the token —
    for that would send whoever reads the alert at the wrong component; the observed cost of getting
    this wrong was a healthy session misdiagnosed as logged out (2026-08-13).

    Without a proxy the answer is no, deliberately: a direct transport failure is rarer and has no
    single obvious culprit, so it takes the generic dossier path instead of this skip.
    """
    if proxy is None or not getattr(proxy, "host", ""):
        return False
    return bool({cls.__name__ for cls in type(exc).__mro__} & _TRANSPORT_FAILURE_NAMES)


class CostcoScraper(BaseRetailerScraper):
    retailer_name = "Costco"
    retailer_key = "costco"
    # The data path has no browser, so the only page a Costco dossier can ever capture is the B2C
    # sign-in screen the token grab drives (scripts/costco_token -> costco_signin). Audit that.
    diagnostic_selectors = costco_signin.SELECTORS

    # PRIMARY PATH: Costco's private GraphQL API (deterministic, no browser) — see
    # scrape() -> _scrape_via_api. On any failure scrape() writes a failure dossier, alerts with
    # its path, and records nothing this run (see BaseRetailerScraper). The one failure it heals
    # itself is a dead refresh token, grabbed back over CDP below.

    def scrape(self):
        """Run the GraphQL API path; on ANY failure write a failure dossier.

        A successful API call that simply finds no orders returns [] (not an error). A dead refresh
        token self-heals over CDP; any other auth failure alerts and skips; everything else goes to
        `_on_deterministic_failure`: dossier + alert + DeterministicPathError, nothing recorded.
        """
        with self._collecting() as dossier:
            return self._scrape_with_dossier(dossier)

    def _scrape_with_dossier(self, dossier):
        try:
            items = self._scrape_via_api()
        except ApiLoginError as exc:
            # FIRST, TRY TO FIX IT OURSELVES. A dead refresh token is the one auth failure that is
            # recoverable without a human: the Browser-Use profile is still logged into Costco, so
            # reconnecting over CDP and capturing a fresh token from the app's own silent refresh
            # turns a run-ending failure into a few seconds of work. Proven live — a grab
            # took 12s and the retried API path scraped normally.
            #
            # This is also exactly when the grab WORKS. It captures the token endpoint's response, so
            # it needs the app to actually perform a refresh — which it only does when the cached
            # token has expired. That is precisely the state we are in here, so the opportunistic
            # weakness of `--grab` disappears at this call site.
            if self._refresh_token_via_browser():
                try:
                    return self._scrape_via_api()
                except Exception:
                    log.warning(
                        "Costco [%s]: API still failing after a token refresh.",
                        self.profile.label, exc_info=True,
                    )

            # An auth failure (dead/rotated refresh token) is not a schema change a code fix can
            # address — alert and skip so the user re-authorizes the token.
            log.warning("Costco [%s]: API auth failed (%s); skipping.",
                        self.profile.label, exc)
            alert(
                f"Costco [{self.profile.label}]: API auth failed — not recorded this run",
                compose(f"The Costco API refused the token ({exc}) and the automatic refresh did not recover it.",
                        do=f"Log the profile into costco.com again (Tools › Log a Profile In), or grab a token: "
                           f"python -m scripts.costco_token --label {self.profile.label} --grab")
                + self._dossier_line(dossier, exc),
            )
            raise LoggedOutError(f"Costco:{self.profile.label}") from exc
        except Exception as exc:  # noqa: BLE001 — a NON-auth failure (schema/network)
            reason = f"{type(exc).__name__}: {exc}"
            if _is_shared_proxy_failure(exc, self.profile.proxy):
                # A transport fault must not be reported as a logout or a shape change: nothing is
                # wrong with the account or the code, so name the proxy and skip. (In the agent era
                # this same fault once bought a paid run that misdiagnosed a healthy session as
                # logged out — that history is why the classification is kept this precise.)
                log.warning(
                    "Costco [%s]: proxy could not reach Costco (%s); skipping.",
                    self.profile.label, reason,
                )
                alert(
                    f"Costco [{self.profile.label}]: proxy unreachable — not recorded this run",
                    compose(f"The request never reached Costco: {reason}. Not a sign-in problem: the proxy "
                            f"{self.profile.proxy.host}:{self.profile.proxy.port} could not connect.",
                            do="Usually clears by the next run; if it repeats, check the proxy is up and its IP is allowlisted.")
                    + self._dossier_line(dossier, exc),
                )
                raise ScrapeUnavailableError(
                    f"Costco:{self.profile.label} proxy unreachable"
                ) from exc
            return self._on_deterministic_failure(
                exc, dossier,
                hint="If the API changed, update the queries in scrapers/costco_api.py; if it is auth, re-authorize: "
                     f"python -m scripts.costco_token --label {self.profile.label} --grab",
            )
        self._report_soft_problems(dossier)
        return items

    def _refresh_token_via_browser(self) -> bool:
        """Capture a fresh Costco refresh token over CDP and save it. True if one was stored.

        Reuses `scripts.costco_token` rather than reimplementing the capture — that module owns both
        the interception strategy and the on-disk format, and a second copy of either would drift.
        Imported lazily: it pulls in the CDP browser stack, which a normal run has no reason to load.

        Never raises. This runs on a path that is already failing, so a broken recovery must not
        replace the real diagnosis with its own — the caller alerts about the original auth failure
        either way.
        """
        try:
            from scripts.costco_token import _grab_refresh_token, _load, _save
        except Exception:
            log.warning("Costco [%s]: token-refresh helper unavailable.",
                        self.profile.label, exc_info=True)
            return False

        log.info("Costco [%s]: attempting an automatic token refresh over CDP.", self.profile.label)
        try:
            token = _grab_refresh_token(self.profile.label)
        except SystemExit:
            # _grab_refresh_token exits the process on a missing/unconfigured profile. Fine for the
            # CLI, fatal here — a scheduled run would die mid-way through its retailers.
            log.warning("Costco [%s]: profile not usable for a CDP token grab.", self.profile.label)
            return False
        except Exception:
            log.warning("Costco [%s]: automatic token refresh failed.",
                        self.profile.label, exc_info=True)
            return False

        if not token:
            # The grab captures the token endpoint's RESPONSE, so it comes up empty when no exchange
            # happened. Since 2026-08-25 it also SIGNS ITSELF IN first when the browser session has
            # lapsed (scrapers/costco_signin.py), and a sign-in performs an exchange of its own — so
            # reaching here now means either the profile has no auth['costco'] block, or the sign-in
            # itself failed and has already logged its own verdict above.
            log.warning("Costco [%s]: no refresh token captured. Either this profile has no "
                        "auth['costco'] block to sign in with, or the sign-in failed — see the "
                        "Costco sign-in verdict logged just above.", self.profile.label)
            return False

        data = _load(self.profile.label)
        data["refresh_token"] = token
        # Drop the cached id_token: it was minted from the OLD refresh token and is what failed.
        # Leaving it would have the retry present the same dead credential and fail identically.
        data.pop("id_token", None)
        _save(self.profile.label, data)
        log.info("Costco [%s]: captured a fresh refresh token (%d chars); retrying the API path.",
                 self.profile.label, len(token))
        return True

    def _api_window(self) -> tuple[str, str]:
        """(start, end) YYYY-MM-DD for getOnlineOrders. end is tomorrow so same-day orders (the API
        window looks date-inclusive) are never dropped; start is the usual lookback."""
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=self.lookback_days)).date().isoformat()
        end = (now + timedelta(days=1)).date().isoformat()
        return start, end

    def _scrape_via_api(self):
        from scrapers.costco_api import CostcoApiClient  # lazy: keeps curl_cffi/jwt optional

        state = self._load_order_state()
        open_ids = [o["order_id"] for o in state.get("open_orders", [])]
        terminal_ids = set(state.get("delivered_ids", [])) | set(state.get("cancelled_ids", []))

        start, end = self._api_window()
        # Pass the profile's proxy so API traffic egresses from the same static ISP IP as the CDP
        # browser paths (token grab, receipts) instead of the host's own IP.
        client = CostcoApiClient(self.profile.label, proxy=self.profile.proxy)
        discovered = client.list_order_numbers(start, end)
        # Re-check open orders too (even older than the window); never re-fetch terminal ones.
        order_numbers = [
            n for n in dict.fromkeys(list(discovered) + list(open_ids)) if n not in terminal_ids
        ]
        log.info(
            "Costco [%s]: %d discovered + %d open -> %d order(s) to fetch via API.",
            self.profile.label, len(discovered), len(open_ids), len(order_numbers),
        )
        if not order_numbers:
            return []
        details = client.get_order_details(order_numbers)
        # Same contract as both Amazons (scrapers/amazon_api.py): a Shop Card bought on a card with
        # an explicit Costco rate in cards.json is funding inventory and gets its own ledger row;
        # on any other card it is personal spending and the mapping drops it.
        from config.cards import boosted_last4s, load_cards
        keep_digital = boosted_last4s("costco", load_cards())
        try:
            items = build_order_items(details, self.profile.label, known_open_ids=frozenset(open_ids),
                                      keep_digital_last4s=keep_digital)
        except PayloadShapeError as exc:
            from scrapers.costco_api import CostcoApiError
            diagnostics.record_response("getOrderDetails order (shape)", 200, exc.payload)
            raise CostcoApiError(str(exc)) from exc
        # THE CAPTURE GATE (2026-09-19): a mandatory cell the payload did not yield is a dossier
        # problem with that order's getOrderDetails payload attached; the rows are still recorded.
        detail_by_id = {str((d or {}).get("orderNumber") or ""): d for d in details if isinstance(d, dict)}
        diagnostics.report_unreadable_rows(
            items, FIELD_SOURCES,
            evidence=lambda oid, msgs: diagnostics.record_response(
                f"getOrderDetails order {oid} (unreadable cells)", 200, detail_by_id.get(oid)))
        log.info("Costco [%s]: built %d ledger row(s) from the API.", self.profile.label, len(items))
        return items
