import abc
import logging
from datetime import datetime, timedelta, timezone

import diagnostics
from alerts.notifier import alert
from config.settings import settings
from models.order import OrderItem
from models.profile import ProfileConfig

log = logging.getLogger(__name__)


class DeterministicPathError(Exception):
    """The deterministic path failed and a failure dossier was written.

    Raised by `BaseRetailerScraper._on_deterministic_failure` after it has alerted with the dossier's
    path, so `main.run_scrape` only has to log and move on — nothing is recorded for this retailer
    this run, and the next scheduled run retries. Distinct from LoggedOutError (a human must
    re-authenticate) and ScrapeUnavailableError (transport; nothing to fix) because the response is
    different: this one says "open the dossier and fix the selector".
    """


class LoggedOutError(Exception):
    """The retailer session is dead and could not self-heal — a human has to log the profile in."""


class ScrapeUnavailableError(Exception):
    """This profile can't be scraped this run, and there is nothing to fix in the code.

    Distinct from LoggedOutError, which says the retailer SESSION is dead and a human has to log in
    again. This says the run couldn't reach the retailer at all — nothing is wrong with the account,
    nothing needs re-authorizing, and the next scheduled run will very likely succeed.

    It exists so a transport fault can't be reported as a logout. Observed 2026-08-13 (in the
    agent-fallback era): the profile's proxy failed one upstream CONNECT to signin.costco.com and the
    resulting alert pointed at the token, which was perfectly healthy; sixty seconds later the API
    authenticated first try. The raiser has already sent an accurate alert.
    """


class ApiLoginError(Exception):
    """The deterministic API path could not authenticate (session logged out / login or token failed).

    Kept apart from every other failure because the response differs: a login failure is not a
    page-shape change a code fix can address, so each retailer's scrape() catches this, alerts
    (naming its dossier), and skips via LoggedOutError. Any OTHER deterministic-path failure
    (page shape / parsing / network) goes through `_on_deterministic_failure`.
    """


class BaseRetailerScraper(abc.ABC):
    retailer_name: str
    retailer_key: str  # matches an entry in a profile's "retailers" list in config.json
    # Every selector / page marker the retailer's deterministic path depends on, by name. Not used to
    # scrape — the parsers keep their own literals — but audited against the captured page when the
    # path fails, so the failure dossier can say WHICH one stopped matching. tests/test_diagnostics.py
    # checks each mapping module's literals are all declared here, so the two cannot drift apart.
    diagnostic_selectors: dict[str, str] = {}

    def __init__(self, profile: ProfileConfig, lookback_days: int | None = None):
        self.profile = profile
        self.lookback_days = lookback_days if lookback_days is not None else settings.lookback_days

    @abc.abstractmethod
    def scrape(self) -> list[OrderItem]:
        """Run this retailer's deterministic path and return ledger rows.

        The contract every implementation keeps (see scrapers/amazon.py for the canonical shape):
        open a dossier with `self._collecting()`; an `ApiLoginError` alerts (naming the dossier) and
        raises LoggedOutError; any other failure goes to `_on_deterministic_failure`; a success calls
        `_report_soft_problems` before returning, so a partially-unreadable page still surfaces.
        """

    def _date_window(self) -> tuple[str, str, str]:
        """(today, earliest, phrase) as ISO date strings for the discovery window.

        `earliest` is the oldest calendar date to keep (today minus lookback_days). Explicit dates
        rather than a rolling 24h clock, so a date-only order stamp compares cleanly.
        """
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        earliest = (now - timedelta(days=self.lookback_days)).date().isoformat()
        phrase = "today or yesterday" if self.lookback_days == 1 else f"on or after {earliest}"
        return today, earliest, phrase

    # --- failure dossiers (what a failure leaves behind) ------------------------------------------
    def _dossier_secrets(self) -> set[str]:
        """Every credential-shaped string on this profile, so a dossier can never write one out."""
        secrets: set[str] = set()
        proxy = self.profile.proxy
        if proxy is not None:
            secrets.update(v for v in (proxy.username, proxy.password) if v)
        for auth in (self.profile.auth or {}).values():
            try:
                values = auth.model_dump() if hasattr(auth, "model_dump") else dict(auth)
            except Exception:  # noqa: BLE001
                continue
            secrets.update(v for k, v in values.items()
                           if isinstance(v, str) and v and k != "method")
        return secrets

    def _collecting(self):
        """The dossier context for one scrape: `with self._collecting() as dossier:`."""
        return diagnostics.collecting(self.retailer_key, self.profile.label,
                                      selectors=self.diagnostic_selectors,
                                      secrets=self._dossier_secrets())

    @staticmethod
    def _dossier_line(dossier, exc) -> str:
        """Write the dossier and return the line an alert appends so a reader can find it."""
        try:
            path = dossier.write(exc)
        except Exception:  # noqa: BLE001 — the dossier must never turn into the failure
            log.exception("Failed to write the failure dossier.")
            return "\n\n(The failure dossier could not be written; see logs/run.log.)"
        link = dossier.upload()
        if link:
            # Every hosted file, not just the report: the reader wants the page HTML and the
            # screenshot in hand from the alert alone. (Object Storage has no folder page to link.)
            files = "".join(f"\n  {name}: {url}" for name, url in dossier.hosted)
            return f"\n\nFailure dossier: {link}{files}\n(local copy: {path})"
        return f"\n\nFailure dossier: {path}"

    def _on_deterministic_failure(self, exc: Exception, dossier, *, hint: str = "") -> list[OrderItem]:
        """A NON-login failure on the deterministic path: write the dossier, alert, stop.

        Nothing is recorded for this retailer this run — recording nothing loudly beats recording
        something wrong — and the next scheduled run retries. The dossier (traceback, page HTML +
        screenshot, selector audit) is the evidence the fix is made from.
        """
        name, label = self.retailer_name, self.profile.label
        reason = f"{type(exc).__name__}: {exc}"
        where = self._dossier_line(dossier, exc)
        log.error("%s [%s]: deterministic path failed (%s); nothing recorded this run. Dossier: %s",
                  name, label, reason, dossier.path, exc_info=True)
        alert(
            f"{name} [{label}]: deterministic path failed — NOT recorded this run",
            f"The {name} deterministic path could not run, so NOTHING was recorded for {name} this "
            f"run. The next scheduled run retries.\n\nReason: {reason}\n\n{hint}{where}\n\n"
            f"The dossier holds the traceback, the page HTML + screenshot at the failure, and a "
            f"selector audit. Hand it to your coding agent to fix the selector, then re-run.",
        )
        raise DeterministicPathError(f"{name}:{label} {reason}") from exc

    def _report_soft_problems(self, dossier) -> None:
        """A scrape that SUCCEEDED but reported non-fatal problems still leaves a dossier + alert.

        The case this exists for: a tracking page whose selectors stopped matching. The rows are
        still written (with a blank number, which never overwrites a known one), so the run looks
        healthy — and a shipped order would sit at 'ordered' forever with no signal at all.
        """
        if not dossier.problems:
            return
        where = self._dossier_line(dossier, None)
        summary = "\n".join(f"- {p}" for p in dossier.problems[:10])
        log.warning("%s [%s]: scrape completed with %d problem(s); dossier: %s",
                    self.retailer_name, self.profile.label, len(dossier.problems), dossier.path)
        alert(
            f"{self.retailer_name} [{self.profile.label}]: scrape completed with "
            f"{len(dossier.problems)} problem(s) — check the dossier",
            f"The orders were recorded, but part of the page could not be read:\n\n{summary}{where}",
        )

    def _load_order_state(self) -> dict:
        """Recorded order state from the sheet for this profile (delivered ids + open orders). Fails soft.

        Terminal orders are trimmed to the discovery window: they exist only to tell the fetch step
        "skip these", and discovery never scans back past the window, so carrying every order ever
        delivered would grow the state for nothing.
        """
        try:
            from sheets.ledger_sync import load_order_state

            _, earliest, _ = self._date_window()
            # Scope to THIS retailer's rows: a profile can host several retailers (e.g. profile-alpha
            # = Best Buy + Costco + Amazon Business), and without this a re-check would pull in another
            # retailer's open orders and re-read them under the wrong retailer, corrupting the ledger
            # (the upsert key has no retailer column).
            return load_order_state(self.profile.label, since=earliest, retailer=self.retailer_name)
        except Exception:
            # load_order_state has its OWN guard for an unreadable sheet (and alerts there), so this
            # only fires for something else entirely — a bad date window, an import failure. Alerted
            # for the same reason: it silently drops every open-order re-check for this run, which
            # looks identical to a healthy run in the log.
            log.warning(
                "%s: could not load order state; OPEN-ORDER RE-CHECKS ARE SKIPPED this run — only "
                "brand-new orders in the date window will be fetched.",
                self.retailer_name, exc_info=True,
            )
            alert(
                f"{self.retailer_name} [{self.profile.label}]: order state unavailable — "
                "re-checks skipped this run",
                "The scrape could not determine which orders are still open, so it re-checked none "
                "of them and only looked for brand-new orders. Any status or tracking-number change "
                "on an open order was missed for this cycle. Check logs/run.log.",
            )
            return {"delivered_ids": [], "open_orders": []}
