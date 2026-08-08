import abc
import logging
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID

from browser_use_sdk import BrowserUse as BrowserUseV2
from browser_use_sdk.v4 import BrowserUse, CustomProxy, RunBrowserSettings

from alerts.notifier import alert
from config.settings import settings
from models.order import OrderExtractionResult, OrderItem
from models.profile import ProfileConfig

log = logging.getLogger(__name__)


class LoggedOutError(Exception):
    """Raised when Browser-Use reports the retailer session is logged out."""


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

    def __init__(self, profile: ProfileConfig, lookback_days: int | None = None):
        self.profile = profile
        self.lookback_days = lookback_days if lookback_days is not None else settings.lookback_days

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

        # Agent skips every known order in its new-order scan; only re-checks CDP misses.
        skip_ids = list(order_state.get("delivered_ids", [])) + list(open_ids)

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
        """Re-check undelivered orders cheaply via CDP + selectors (no LLM). Returns
        (recheck_items, fallback_orders) — fallback_orders are ones CDP couldn't read, to be
        handed to the agent. If this retailer doesn't implement read_tracking_page or CDP setup
        fails, everything falls back to the agent.
        """
        if (
            not open_orders
            or not self.cdp_recheck_enabled
            or type(self).read_tracking_page is BaseRetailerScraper.read_tracking_page
        ):
            return [], open_orders

        from scrapers.cdp import CdpBrowser

        items: list[OrderItem] = []
        fallback: list[dict] = []
        processed: set[str] = set()
        try:
            with CdpBrowser(self.profile) as page:
                for o in open_orders:
                    processed.add(o["order_id"])
                    shipments = o.get("shipments") or []
                    # Only a single shipment that is already 'shipped' is safe for the cheap CDP
                    # delivery-watch. Send everything else to the agent (which re-reads the whole
                    # order-details page): multi-shipment orders need a per-shipment status, and a
                    # still-'ordered' order can still SPLIT into several shipments when it ships —
                    # CDP polling one tracking page would silently miss those new shipments.
                    if len(shipments) > 1 or o.get("status") != "shipped":
                        fallback.append(o)
                        continue
                    # Stamp the row with its known shipment label so the upsert updates in place
                    # ("Shipment 1" for a labeled single order; "" for legacy pre-Shipment-column rows).
                    shipment_label = shipments[0] if len(shipments) == 1 else ""
                    url = o.get("tracking_url") or o.get("order_url")
                    names = o.get("item_names") or []
                    if not url or not names:
                        fallback.append(o)
                        continue
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=90000)
                        page.wait_for_timeout(3000)
                        info = self.read_tracking_page(page)
                    except Exception:
                        log.warning("CDP re-check failed for order %s; will fall back to agent.", o["order_id"], exc_info=True)
                        info = None
                    if info is None:
                        fallback.append(o)
                        continue
                    for name in names:
                        items.append(
                            OrderItem(
                                retailer=self.retailer_name,
                                profile_label=self.profile.label,
                                order_id=o["order_id"],
                                order_date=o.get("order_date", ""),
                                status=info["status"],
                                tracking_number=info["tracking_number"],
                                tracking_url=url,
                                delivery_date=info.get("delivery_promise", ""),
                                item_name=name,
                                shipment=shipment_label,
                            )
                        )
                    log.info("CDP re-check %s -> %s", o["order_id"], info["status"])
        except Exception:
            log.warning("CDP browser unavailable; falling back to agent for open orders.", exc_info=True)
            # anything not yet processed goes to the agent
            return items, [o for o in open_orders if o["order_id"] not in processed] + fallback
        return items, fallback

    def _load_order_state(self) -> dict:
        """Recorded order state from the sheet for this profile (delivered ids + open orders). Fails soft."""
        try:
            from sheets.ledger_sync import load_order_state

            return load_order_state(self.profile.label)
        except Exception:
            log.warning("Could not load order state; treating all orders as new.", exc_info=True)
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
