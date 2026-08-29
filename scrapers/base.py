import abc
import logging
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID

from browser_use_sdk import BrowserUse as BrowserUseV2
from browser_use_sdk.v4 import BrowserUse, CustomProxy, RunBrowserSettings

import diagnostics
from alerts.notifier import alert
from config.settings import settings
from models.order import TERMINAL_STATUSES, OrderExtractionResult, OrderItem
from models.profile import ProfileConfig

log = logging.getLogger(__name__)


class DeterministicPathError(Exception):
    """The deterministic path failed, the agent fallback is OFF, and a failure dossier was written.

    Raised by `BaseRetailerScraper._on_deterministic_failure` after it has alerted with the dossier's
    path, so `main.run_scrape` only has to log and move on — nothing is recorded for this retailer
    this run, and the next scheduled run retries. Distinct from LoggedOutError (a human must
    re-authenticate) and ScrapeUnavailableError (transport; nothing to fix) because the response is
    different: this one says "open the dossier and fix the selector".
    """


class LoggedOutError(Exception):
    """Raised when Browser-Use reports the retailer session is logged out."""


class ScrapeUnavailableError(Exception):
    """This profile can't be scraped this run, and THE AGENT MUST NOT BE TRIED as a substitute.

    Distinct from LoggedOutError, which says the retailer SESSION is dead and a human has to log in
    again. This says the run couldn't reach the retailer at all — nothing is wrong with the account,
    nothing needs re-authorizing, and the next scheduled run will very likely succeed.

    It exists so a transport fault can't be reported as a logout. Observed 2026-08-13: the profile's
    proxy failed one upstream CONNECT to signin.costco.com, the API path fell back to the agent, and
    the agent — which egresses through THAT SAME PROXY — couldn't load the page and concluded the
    session was logged out. That alert pointed at the token, which was perfectly healthy; sixty
    seconds later the API authenticated first try. The raiser has already sent an accurate alert.
    """


class ApiLoginError(Exception):
    """The deterministic API path could not authenticate (session logged out / login or token failed).

    This is an EXCEPTION to the agent fallback: a login failure is not a DOM-shape change the agent can
    fix — running the agent would just burn a paid session or hit the same wall — so each retailer's
    scrape() catches this, alerts, and skips (via LoggedOutError) instead of falling back to the agent.
    Any OTHER deterministic-path failure (page shape / parsing / network) still falls back to the agent.
    """


def _extract_json_object(text: str) -> str:
    """Best-effort extraction of a JSON object from an LLM's raw text output.

    v4 has no structured-output/schema parameter, so the agent is instructed to
    return JSON directly in the prompt, but it may still wrap it in markdown
    fences or add surrounding prose — this strips that off before parsing.
    """
    text = text.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


class BaseRetailerScraper(abc.ABC):
    retailer_name: str
    order_history_url: str
    retailer_key: str  # matches an entry in a profile's "retailers" list in profiles.json
    # Cheap CDP+selector re-check path. A retailer opts in by overriding read_tracking_page AND
    # leaving this True. Retailers whose orders can change shape after they already look shipped —
    # e.g. Amazon splits an order into multiple shipments (each with its own delivery date) at ship
    # time, sometimes revealing one late — set this False and re-check via the agent, which re-reads
    # the whole order-details page so no shipment is ever missed.
    cdp_recheck_enabled: bool = True
    # Every selector / page marker the retailer's deterministic path depends on, by name. Not used to
    # scrape — the parsers keep their own literals — but audited against the captured page when the
    # path fails, so the failure dossier can say WHICH one stopped matching. tests/test_diagnostics.py
    # checks each mapping module's literals are all declared here, so the two cannot drift apart.
    diagnostic_selectors: dict[str, str] = {}

    def __init__(self, profile: ProfileConfig, lookback_days: int | None = None):
        self.profile = profile
        self.lookback_days = lookback_days if lookback_days is not None else settings.lookback_days

    # --- failure dossiers (the replacement for the paid agent fallback) ---------------------------
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
            return f"\n\nFailure dossier: {link}\n(local copy: {path})"
        return f"\n\nFailure dossier: {path}"

    def _on_deterministic_failure(self, exc: Exception, dossier, *, force_agent: bool = False,
                                  hint: str = "") -> list[OrderItem]:
        """A NON-login failure on the deterministic path: dossier first, then agent or stop.

        The dossier is written either way — even when the agent runs, the point is to know what
        broke. Then: if the agent fallback is enabled (or this retailer's *_FORCE_AGENT hook is on,
        which is an explicit request to spend), alert and run it; otherwise alert with the dossier's
        path and raise DeterministicPathError so nothing is recorded and the next run retries.
        """
        name, label = self.retailer_name, self.profile.label
        reason = f"{type(exc).__name__}: {exc}"
        where = self._dossier_line(dossier, exc)
        if settings.agent_fallback_enabled or force_agent:
            log.warning("%s [%s]: deterministic path failed (%s); falling back to the agent.",
                        name, label, reason, exc_info=True)
            alert(
                f"{name} [{label}]: deterministic path failed — using agent fallback",
                f"The {name} deterministic path could not run, so this run used the Browser-Use "
                f"agent instead.\n\nReason: {reason}\n\n{hint}{where}",
            )
            return BaseRetailerScraper.scrape(self)
        log.error("%s [%s]: deterministic path failed (%s); agent fallback is OFF — nothing "
                  "recorded this run. Dossier: %s", name, label, reason, dossier.path, exc_info=True)
        alert(
            f"{name} [{label}]: deterministic path failed — NOT recorded this run",
            f"The {name} deterministic path could not run and the agent fallback is off "
            f"(AGENT_FALLBACK_ENABLED), so NOTHING was recorded for {name} this run. The next "
            f"scheduled run retries.\n\nReason: {reason}\n\n{hint}{where}\n\n"
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

    def _date_window(self) -> tuple[str, str, str]:
        """(today, earliest, phrase) as ISO date strings for the discovery window.

        `earliest` is the oldest calendar date to keep (today minus lookback_days). Handing the
        agent explicit dates avoids it having to reason about a rolling 24h clock.
        """
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        earliest = (now - timedelta(days=self.lookback_days)).date().isoformat()
        phrase = "today or yesterday" if self.lookback_days == 1 else f"on or after {earliest}"
        return today, earliest, phrase

    @abc.abstractmethod
    def task_prompt(self, skip_order_ids: list[str], recheck_orders: list[dict]) -> str:
        """The task handed to the agent.

        `skip_order_ids`: order IDs already recorded (delivered or already re-checked via CDP) —
        the agent skips these in its new-order scan and only fully processes genuinely new orders.
        `recheck_orders`: open orders the cheap CDP reader could NOT read (selector miss / no
        tracking link) — the agent falls back to re-checking these directly via their link.
        """

    def read_tracking_page(self, page):  # optional; overridden by retailers that support CDP re-checks
        """Return {status, tracking_number, delivery_promise} from a loaded tracking page, or None."""
        return None

    def scrape(self) -> list[OrderItem]:
        order_state = self._load_order_state()
        open_orders = order_state.get("open_orders", [])
        open_ids = {o["order_id"] for o in open_orders}

        # Cheap path first: re-check open orders via CDP + selectors (near-free, deterministic).
        recheck_items, fallback_orders = self._recheck_via_cdp(open_orders)

        # Agent skips every known order in its new-order scan (terminal + still-open); it only
        # re-checks CDP misses. Cancelled orders are terminal, so they stay skipped for good.
        skip_ids = (
            list(order_state.get("delivered_ids", []))
            + list(order_state.get("cancelled_ids", []))
            + list(open_ids)
        )

        client = BrowserUse()
        agent_session_id: UUID | None = None
        try:
            browser_settings = self._build_browser_settings()

            created = client.runs.create(
                self.task_prompt(skip_ids, fallback_orders),
                model=settings.browser_use_llm,
                browser_settings=browser_settings,
                max_cost_usd=settings.browser_use_max_cost_usd,
            )
            agent_session_id = created.session_id
            run = client.runs.wait_for_completion(created.id)

            log.info(
                "%s [%s]: run %s finished (%s) — cost=$%s, tokens in=%s/out=%s",
                self.retailer_name,
                self.profile.label,
                run.id,
                run.status.value,
                run.total_cost_usd,
                run.total_input_tokens,
                run.total_output_tokens,
            )

            if run.status.value != "completed":
                raise RuntimeError(
                    f"{self.retailer_name} [{self.profile.label}]: run ended with status "
                    f"'{run.status.value}' (error={run.error})"
                )

            if not run.result:
                raise RuntimeError(f"{self.retailer_name} [{self.profile.label}]: run completed with no result")

            try:
                parsed = OrderExtractionResult.model_validate_json(_extract_json_object(run.result))
            except Exception as exc:
                raise RuntimeError(
                    f"{self.retailer_name} [{self.profile.label}]: could not parse agent output as JSON "
                    f"({exc}). Raw output: {run.result[:500]!r}"
                ) from exc

            if parsed.logged_out:
                alert(
                    f"{self.retailer_name} [{self.profile.label}]: session logged out",
                    f"Browser-Use found a logged-out session while checking {self.retailer_name} "
                    f"order history on profile '{self.profile.label}'. Log in again on that profile "
                    "(cloud.browser-use.com) and re-run.",
                )
                raise LoggedOutError(f"{self.retailer_name}:{self.profile.label}")

            # Date-based window (retailers expose only an order date). Keep any order dated on or
            # after `lookback_days` calendar days ago (default 1 = today and yesterday); midnight so
            # a date-only order isn't dropped for sorting before an hour-precise cutoff.
            cutoff = (datetime.now(timezone.utc) - timedelta(days=self.lookback_days)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            agent_items = []
            for item in parsed.items:
                parsed_date = self._parse_order_date(item.order_date)
                # Keep new orders within the window, re-checked open orders of ANY age, and
                # unparseable dates (rather than crash).
                if parsed_date is None or parsed_date >= cutoff or item.order_id in open_ids:
                    item.profile_label = self.profile.label
                    agent_items.append(item)
            # CDP re-check items + agent (new orders + fallback re-checks)
            return recheck_items + agent_items
        finally:
            client.close()
            if agent_session_id:
                self._stop_underlying_browser(agent_session_id)

    def _recheck_via_cdp(self, open_orders: list[dict]) -> tuple[list[OrderItem], list[dict]]:
        """Read every open shipment's tracking page via CDP + selectors (no LLM).

        Returns (items, agent_orders). `items` are per-shipment status/tracking rows.
        `agent_orders` are orders the agent must still re-read: ones whose structure can change
        (needs_agent — some shipment hasn't shipped, so the order can still split), plus any
        order where a selector came back empty.

        Iterates SHIPMENTS, not orders: an order that split has one tracking page per shipment,
        each with its own status and delivery date. The previous version had a single URL per
        order and so had to skip multi-shipment orders entirely.
        """
        agent_orders = [o for o in open_orders if o.get("needs_agent")]
        agent_ids = {o["order_id"] for o in agent_orders}

        def to_agent(order: dict) -> None:
            if order["order_id"] not in agent_ids:
                agent_orders.append(order)
                agent_ids.add(order["order_id"])

        # Retailers with no cheap reader (no read_tracking_page override, or opted out) re-check
        # entirely through the agent.
        if (
            not open_orders
            or not self.cdp_recheck_enabled
            or type(self).read_tracking_page is BaseRetailerScraper.read_tracking_page
        ):
            return [], list(open_orders)

        targets = [
            (o, s)
            for o in open_orders
            for s in o.get("shipments", [])
            if s["status"] not in TERMINAL_STATUSES and s.get("tracking_url")
        ]
        if not targets:
            return [], agent_orders

        from scrapers.cdp import CdpBrowser

        items: list[OrderItem] = []
        done: set[tuple[str, str]] = set()
        try:
            with CdpBrowser(self.profile) as page:
                for o, s in targets:
                    done.add((o["order_id"], s["shipment"]))
                    try:
                        page.goto(s["tracking_url"], wait_until="domcontentloaded", timeout=90000)
                        page.wait_for_timeout(3000)
                        info = self.read_tracking_page(page)
                    except Exception:
                        log.warning(
                            "CDP read failed for %s / %s; falling back to agent.",
                            o["order_id"], s["shipment"], exc_info=True,
                        )
                        info = None
                    if info is None:
                        to_agent(o)
                        continue
                    for name in s["item_names"]:
                        items.append(
                            OrderItem(
                                retailer=self.retailer_name,
                                profile_label=self.profile.label,
                                order_id=o["order_id"],
                                order_date=o.get("order_date", ""),
                                status=info["status"],
                                tracking_number=info["tracking_number"],
                                tracking_url=s["tracking_url"],
                                # Deliberately blank: the tracking page states a promise in prose
                                # ("Arriving Monday"), not a date. Writing that here would clobber
                                # the agent's YYYY-MM-DD with un-parseable text; blank preserves it
                                # (see _merge_row).
                                delivery_date="",
                                item_name=name,
                                shipment=s["shipment"],
                            )
                        )
                    log.info("CDP read %s / %s -> %s", o["order_id"], s["shipment"], info["status"])
        except Exception:
            log.warning("CDP browser unavailable; falling back to agent for open orders.", exc_info=True)
            for o, s in targets:
                if (o["order_id"], s["shipment"]) not in done:
                    to_agent(o)
            return items, agent_orders
        return items, agent_orders

    def _load_order_state(self) -> dict:
        """Recorded order state from the sheet for this profile (delivered ids + open orders). Fails soft.

        Delivered orders are trimmed to the discovery window: they exist only to tell the agent
        "skip these", the agent never scans back past the window, and the skip list is re-sent on
        every agent step — so carrying every order ever delivered would grow the per-step prompt
        without bound.
        """
        try:
            from sheets.ledger_sync import load_order_state

            _, earliest, _ = self._date_window()
            # Scope to THIS retailer's rows: a profile can host several retailers (e.g. profile-alpha
            # = Best Buy + Costco + Amazon Business), and without this a re-check would pull in another
            # retailer's open orders and re-read them under the wrong retailer (the agent fallback would
            # open their order_url and mislabel them, corrupting the ledger).
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

    @staticmethod
    def _stop_underlying_browser(agent_session_id: UUID) -> None:
        """A v4 run/session shows 'completed' immediately, but the browser behind it stays
        'active' — v4 keeps it alive in case a follow-up reuses it — until its multi-hour
        timeout. We never send follow-ups, so leaving it running just holds a reservation
        against the account balance and a concurrent-session slot for nothing. v4 exposes no
        way to stop it, but the underlying browser is a shared resource reachable via v2's
        /browsers endpoint, keyed back to this run's session via agent_session_id.
        """
        v2_client = BrowserUseV2()
        try:
            # Use raw HTTP, not the typed browsers.list()/stop(): the SDK's BrowserSessionItemView
            # puts a strict regex on proxy_cost/browser_cost that rejects tiny values returned in
            # scientific notation (e.g. '1.16e-7'), which crashes parsing even though the calls
            # themselves succeed. Raw dicts sidestep that SDK bug.
            data = v2_client._http.request("GET", "/browsers", params={"pageSize": 50})
            target_id = None
            for item in (data or {}).get("items", []):
                if str(item.get("agentSessionId")) == str(agent_session_id) and item.get("status") == "active":
                    target_id = item.get("id")
                    break
            if target_id:
                v2_client._http.request("PATCH", f"/browsers/{target_id}", json={"action": "stop"})
                log.info("Stopped underlying browser %s for session %s", target_id, agent_session_id)
        except Exception:
            log.warning("Failed to stop underlying browser for session %s", agent_session_id, exc_info=True)
        finally:
            v2_client.close()

    def _build_browser_settings(self) -> RunBrowserSettings | None:
        """Pin the run to this profile's Browser-Use profile/proxy, if either is configured."""
        proxy = self.profile.proxy
        custom_proxy = (
            CustomProxy(
                host=proxy.host,
                port=proxy.port,
                username=proxy.username or None,
                password=proxy.password or None,
            )
            if proxy and proxy.host
            else None
        )

        if not self.profile.profile_id and custom_proxy is None:
            return None

        # RunBrowserSettings forbids extra fields and only accepts its camelCase aliases
        # (profileId/customProxy/proxyCountryCode), not the snake_case Python attribute names.
        return RunBrowserSettings(
            profileId=self.profile.profile_id or None,
            customProxy=custom_proxy,
            proxyCountryCode=None,
        )

    @staticmethod
    def _parse_order_date(value: str) -> datetime | None:
        try:
            return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None
