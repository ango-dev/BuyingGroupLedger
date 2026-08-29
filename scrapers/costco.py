import logging
from datetime import datetime, timedelta, timezone

from alerts.notifier import alert
from config.settings import settings
from scrapers.base import (
    ApiLoginError,
    BaseRetailerScraper,
    LoggedOutError,
    ScrapeUnavailableError,
)
from scrapers.costco_mapping import ORDER_DETAILS_URL, build_order_items

log = logging.getLogger(__name__)

#: Exception names meaning "the request never reached Costco" — no response was ever received.
#: Matched by NAME across the MRO rather than by importing curl_cffi, which `_scrape_via_api`
#: deliberately imports lazily so the dependency stays optional.
#:
#: `HTTPError` is pointedly absent: an HTTP status means the transport DID get through, so the fault
#: is at Costco's end and the agent is worth a try.
#: Both spellings of each concept on purpose: curl_cffi and requests say `Timeout` / `ConnectionError`
#: while Python's builtins say `TimeoutError` / `ConnectionError`, and either can surface here.
#: Subclasses (ConnectionRefusedError, ConnectionResetError, ReadTimeout, ...) match through the MRO.
_TRANSPORT_FAILURE_NAMES = frozenset({
    "ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout", "TimeoutError",
    "ProxyError", "SSLError",
})


def _is_shared_proxy_failure(exc: Exception, proxy) -> bool:
    """Did this fail in a way the agent fallback would hit identically?

    Only when a proxy is CONFIGURED. That is the whole condition: the API path and the browser paths
    egress through the same static ISP proxy (`CostcoApiClient(..., proxy=self.profile.proxy)` here,
    `if proxy and proxy.host` there), so a transport that can't get out through it won't get out for
    the agent either. Spending a paid agent session on that is guaranteed waste — and worse, the
    agent interprets a page it cannot load as a logged-out session, which is a false alarm aimed at
    the wrong component.

    Without a proxy the answer is no, deliberately: the agent then uses a completely different
    transport (a remote browser, not this host's curl), so it really might succeed where curl didn't.
    """
    if proxy is None or not getattr(proxy, "host", ""):
        return False
    return bool({cls.__name__ for cls in type(exc).__mro__} & _TRANSPORT_FAILURE_NAMES)


class CostcoScraper(BaseRetailerScraper):
    retailer_name = "Costco"
    retailer_key = "costco"
    # Costco account "Orders & Purchases" — only used by the AGENT FALLBACK below. The primary path
    # is Costco's GraphQL API (scrapers/costco_api.py), which needs no browser.
    order_history_url = "https://www.costco.com/OrderStatusCmd"
    # Direct order-details deep link (shared with the GraphQL mapping, which fills order_url from it).
    # The UUID is Costco's web-app client id (same for every account), so the agent can jump straight
    # to an order and the link is derivable from the order number alone.
    order_details_url = ORDER_DETAILS_URL

    # PRIMARY PATH: Costco's private GraphQL API (deterministic, no browser, no agent) — see
    # scrape() -> _scrape_via_api. FALLBACK: if the API errors (auth dead, schema changed, network),
    # scrape() alerts and defers to the Browser-Use agent via the base implementation, which uses the
    # task_prompt below. No CDP re-check either way (read_tracking_page is not overridden, so
    # base._recheck_via_cdp routes agent-fallback re-checks straight to the agent).

    def scrape(self):
        """Try the GraphQL API first; fall back to the Browser-Use agent on ANY failure.

        A successful API call that simply finds no orders returns [] (not an error), so the agent —
        which costs money — only runs when the API path genuinely can't. The agent path is the base
        class's scrape(), which drives task_prompt() below.
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

            # Auth failure (dead/rotated refresh token) is NOT a schema change the agent can fix — do
            # NOT run the (paid) agent; alert and skip so the user re-authorizes the token.
            log.warning("Costco [%s]: API auth failed (%s); NOT running the agent.",
                        self.profile.label, exc)
            alert(
                f"Costco [{self.profile.label}]: API auth failed — agent NOT run",
                f"The Costco API could not authenticate ({exc}), and the automatic token refresh "
                f"over CDP did not recover it — which usually means the Browser-Use profile's own "
                f"Costco session is logged out, since that session is what the refresh reads from.\n\n"
                f"Log the profile back into costco.com:\n"
                f"  python -m scripts.create_profile --label {self.profile.label}\n"
                f"then either re-run, or grab a token directly:\n"
                f"  python -m scripts.costco_token --label {self.profile.label} --grab\n\n"
                f"The agent was deliberately not run — an auth failure is not something it can fix."
                + self._dossier_line(dossier, exc),
            )
            raise LoggedOutError(f"Costco:{self.profile.label}") from exc
        except Exception as exc:  # noqa: BLE001 — a NON-auth failure (schema/network) degrades to the agent
            reason = f"{type(exc).__name__}: {exc}"
            if _is_shared_proxy_failure(exc, self.profile.proxy):
                # Same reasoning as ApiLoginError above — "or hit the same wall" — for the case where
                # the wall is the proxy both paths share. Skip, and say so accurately: the last time
                # this fell through to the agent it cost a paid run and produced a "session is logged
                # out" alert about a session that was fine.
                log.warning(
                    "Costco [%s]: proxy could not reach Costco (%s); NOT running the agent.",
                    self.profile.label, reason,
                )
                alert(
                    f"Costco [{self.profile.label}]: proxy unreachable — agent NOT run",
                    f"The request never reached Costco, so nothing was scraped this run.\n\n"
                    f"Reason: {reason}\n\n"
                    f"THIS IS NOT A LOGIN PROBLEM — do not re-authorize the token. The profile's "
                    f"proxy ({self.profile.proxy.host}:{self.profile.proxy.port}) failed to get a "
                    f"connection out. The agent was deliberately not run because it egresses through "
                    f"that same proxy, so it would fail identically while costing a paid session.\n\n"
                    f"Usually transient — the next scheduled run picks the orders up. If it repeats, "
                    f"check the proxy is alive and that its IP is still allowlisted."
                    + self._dossier_line(dossier, exc),
                )
                raise ScrapeUnavailableError(
                    f"Costco:{self.profile.label} proxy unreachable"
                ) from exc
            return self._on_deterministic_failure(
                exc, dossier, force_agent=settings.costco_force_agent,
                hint="The dossier holds the GraphQL request/response that failed. If the API schema "
                     "changed, update scrapers/costco_api.py's queries; if auth is the problem, "
                     f"re-authorize with `python -m scripts.costco_token --label {self.profile.label} ...`.",
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

        # Test/ops hook: force the agent-fallback path (e.g. to validate it) without breaking the token.
        if settings.costco_force_agent:
            raise RuntimeError("COSTCO_FORCE_AGENT is set — forcing the agent fallback.")

        state = self._load_order_state()
        open_ids = [o["order_id"] for o in state.get("open_orders", [])]
        terminal_ids = set(state.get("delivered_ids", [])) | set(state.get("cancelled_ids", []))

        start, end = self._api_window()
        # Pass the profile's proxy so API traffic egresses from the same static ISP IP as the browser
        # paths (agent fallback / CDP) instead of the host's own IP.
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
        items = build_order_items(details, self.profile.label, known_open_ids=frozenset(open_ids))
        log.info("Costco [%s]: built %d ledger row(s) from the API.", self.profile.label, len(items))
        return items

    def task_prompt(self, skip_order_ids: list[str], recheck_orders: list[dict]) -> str:
        # Cost is driven by number of steps (browser-use sends the whole page each step), not prompt
        # length. The agent (1) scans for NEW orders and (2) re-checks open orders (Costco has no
        # cheap CDP path, so all open-order re-checks arrive here as recheck_orders).
        today, earliest, window_phrase = self._date_window()

        skip_line = ""
        if skip_order_ids:
            skip_line = (
                "\nAlready recorded — do NOT process these in the new-order scan (handled separately): "
                f"{', '.join(sorted(skip_order_ids))}.\n"
            )

        recheck_block = ""
        if recheck_orders:
            lines = []
            for o in recheck_orders:
                where = o.get("order_url") or self.order_details_url.format(o["order_id"])
                lines.append(
                    f"  - order_id {o['order_id']} (ordered {o.get('order_date','?')}), currently "
                    f"'{o.get('status','?')}': go to {where}"
                )
            recheck_block = (
                "\nRE-CHECK these already-recorded, not-yet-delivered orders — do NOT re-scan the "
                "whole order history for them. For each, open its order-details view and re-read "
                "EVERY physical shipment on that page (see the shipment rules below). Output one entry "
                "per (shipment x distinct physical item), filling ALL fields for each shipment just "
                "like a new order — including THIS shipment's own quantity, cost_per_item, shipping, "
                "card_last4, delivery_address and order_url. An order can SPLIT after it was recorded "
                "(a single shipment of quantity N becomes N shipments of smaller quantity): each "
                "split-off shipment is a brand-new row that needs its own full data, and the original "
                "shipment's quantity drops accordingly — so re-read every shipment's quantity/cost "
                "fresh, do NOT leave them blank. Ignore digital items. These may be older than the "
                "window above; re-check anyway:\n"
                + "\n".join(lines)
                + "\n"
            )

        new_scan_skip = ""
        if skip_order_ids:
            new_scan_skip = (
                " Skip any order whose ID is in the already-recorded list above; only fully process "
                "orders NOT in that list."
            )

        return f"""You have TWO jobs on Costco. Do both, then return one combined JSON result.

WORK EFFICIENTLY — READ THIS FIRST. Input tokens dominate cost and every step re-sends the whole page,
so keep the number of browser actions small and never dump the full page repeatedly:
- Each extraction should run ONE JavaScript evaluate that returns COMPACT JSON (only the fields you
  need), not document.body.innerText of the whole page. Read the page in structured pieces, not blobs.
- The orders list may LAZY-LOAD or paginate. Load more (scroll to the bottom in a loop, or page
  forward) until the oldest visible order is dated before {earliest} or the list stops growing — THEN
  read the whole list in one pass. Do not read-scroll-read-scroll one order at a time.
- Get order_id + date + top-level status for every in-window order in that single read. Then open each
  qualifying order's details view once and read its shipments in ONE evaluate. Target roughly one read
  per page; dozens of browser actions for a handful of orders means you are exploring too much.
- If that one read finds NO tracking number and NO shipment/tracking section at all, that almost always
  means the order hasn't shipped yet — record it as "ordered" with tracking_number "" and MOVE ON
  immediately. Do NOT try a second or third selector to double-check, do NOT delegate a sub-agent task
  to re-read the same output, and do NOT take a screenshot to visually confirm the absence. One clean
  read that shows no tracking is itself the answer, not a sign to dig deeper — an order that's still
  "Preparing"/"Order received" simply has nothing there to find yet.

SCOPE — ONLINE SHIPPED ORDERS ONLY. Costco's Orders & Purchases page mixes several kinds of purchase.
Record ONLY online orders that ship to an address. IGNORE (do not output any entry for):
- In-warehouse / in-store purchases and warehouse pickup / curbside-pickup orders (no carrier shipment),
- Costco Same-Day / grocery deliveries fulfilled by Instacart (no standard carrier tracking),
- Travel, membership, gift cards, and other digital/non-shippable items (see digital rule below).

JOB 1 — find NEW orders. Go to {self.order_history_url} and wait for it to load. If you land on a
sign-in / login page, the session is logged out: report that (see output format) and stop immediately;
do not attempt to log in. Today is {today}. Record EVERY qualifying online order placed on or after
{earliest} ({window_phrase}) — there may be several. Costco lists orders newest-first; after loading
the full list (see the lazy-load note above), take every in-scope order dated on or after {earliest}
and ignore those dated before it. Do NOT stop early after the first order.
CANCELLED orders: if an order shows as Cancelled, SKIP it — do not record it — but KEEP scanning the
other in-window orders; a cancelled order is NOT a stopping point.{new_scan_skip}
For each qualifying (non-cancelled, in-scope) order, open its order-details view and extract it per the
shipment rules below. You can jump straight to any order's details at
https://www.costco.com/myaccount/#/app/4900eb1f-0c10-4bd9-99c3-c59e6c1ecebf/orderdetails/<order-id>
instead of clicking through the list.
{skip_line}{recheck_block}
JOB 2 is the re-check list above (empty if none). Combine JOB 1 and JOB 2 entries into "items".

SHIPMENT RULES (apply to every order-details page):
- An order can split into multiple shipments. A physical SHIPMENT is the set of items that share the
  SAME tracking number: items under the same tracking number are ONE shipment; items under DIFFERENT
  tracking numbers are DIFFERENT shipments. That is what "splitting" means — units that ship in
  separate boxes get separate tracking numbers. Whatever heading Costco prints above a group (if
  any), ignore it and use the numbering rule below instead.
- **ONE "Track" LINK CAN COVER SEVERAL BOXES, AND THIS IS THE EASIEST THING TO GET WRONG HERE.** The
  tracking link belongs to the ORDER LINE, not to the box: when you buy quantity 2 of one product and
  Costco ships them separately, the order page still shows ONE product row with ONE "Track" link, and
  the tracking page it opens lists BOTH packages — each with its own carrier tracking number, usually
  labelled like "Package 1 of 2" / "Package 2 of 2".
  So when you open a tracking page, read EVERY package listed on it, not just the first. Each package
  is a separate shipment and needs its own entry, even though they all came from one product row and
  one link. Two shipments therefore SHARE a tracking_url while having DIFFERENT tracking numbers —
  that is normal and expected, so an identical tracking_url is never a reason to doubt your read.
  If a tracking page lists N packages, that product row produced N shipments. Do not report fewer.
- IGNORE digital items entirely — do NOT output any entry for them. A group is digital if it is a gift
  card, membership, digital download/eBook, redemption code, or any non-shippable line (a Same-Day/
  Instacart grocery order also does not belong here — see SCOPE above). Digital items are never resold,
  so skip them completely.
- Output ONE entry per (physical shipment x distinct product) within the order. If the SAME product
  appears more than once inside ONE shipment, do NOT create duplicate rows — output a single entry for
  it with quantity = the total count of that product in that shipment. Two shipments each containing
  the same product are still TWO separate entries (one per shipment), each with its own shipment label
  and tracking. Read quantity FRESH from the page every time, including on re-checks — never default
  it to 1.
- Label EVERY physical shipment "Shipment 1", "Shipment 2", "Shipment 3", ... in top-to-bottom order,
  INCLUDING a single-shipment order (its one shipment is "Shipment 1"). Do NOT copy Costco's own
  wording — always use this numbering, so the same shipment gets the same label on every re-check and
  updates its existing row instead of creating a duplicate. Skipped digital groups do not consume a
  number: number only the physical shipments you actually output.
- Every physical entry for the order gets the tracking_number / delivery_date / status of ITS
  shipment. tracking_url may legitimately be shared with a sibling shipment (see the "Track" link
  rule above).
- EVERY physical shipment in a multi-box order has its OWN, DIFFERENT tracking number from every
  other shipment in that same order — that is what makes it a separate shipment. If you find yourself
  about to report the SAME tracking number for two different shipments, STOP: that is a read error,
  not a real duplicate. Almost always it means two boxes shared one "Track" link and you read only the
  first package on that tracking page — go back to that page and read the OTHER package's number
  (do not reuse a number you already recorded for a different shipment) before reporting it.
  If after re-reading you still cannot find a second distinct number, report that shipment with
  tracking_number "" and its real status. Never duplicate a number to fill the gap: a blank is
  corrected on the next run, whereas a duplicate invents a box that does not exist and permanently
  corrupts the ledger.

Fields for each entry:
- retailer: "Costco"
- order_id: the Costco order number
- order_date: the date the order was placed, formatted exactly as YYYY-MM-DD
- shipment: "Shipment 1" / "Shipment 2" / ... — this shipment's number in top-to-bottom order (a
  single-shipment order is "Shipment 1"). Never Costco's own wording.
- status: judged from THIS shipment's status text on the order-details page:
    * "delivered" — the shipment says "Delivered" / "Delivered <date>"
    * "shipped"   — the page says "Shipped" / "Out for delivery" / "Arriving <date>" and it is not yet
      delivered. Judge this from the SHIPMENT'S OWN STATUS TEXT, not from whether you managed to read
      a tracking number.
    * "ordered"   — POSITIVE evidence it has not shipped: the page itself says "Preparing" / "Order
      received", or there is no shipment section at all.
      NEVER use "ordered" merely because you could not find a tracking number. "I could not read it"
      and "it has not shipped" are different facts, and reporting the second when you mean the first
      walks a delivered order backwards on the ledger. If the status text says shipped or delivered
      but you cannot read the number, report THAT status with tracking_number "" — a missing number
      is recoverable on the next run, a wrong status is not.
    * "cancelled" — the whole order (or this shipment) shows Cancelled. Only use this on a re-check of
      an order already recorded; a brand-new cancelled order is skipped in JOB 1, not recorded.
- order_url: the full URL of this order's details page (address bar URL while viewing it) — same for
  every shipment of the order
- tracking_number: the carrier tracking number shown for THIS shipment; "" if not shipped yet, and ""
  also if it HAS shipped but you could not read its number. Costco shows it on the order-details /
  shipment view; if the number only appears behind a "Track" link, follow that link and read EVERY
  package listed on the page it opens (one link can cover several boxes — see the SHIPMENT RULES),
  then come back. Leaving it "" is safe — the recorded number is kept and the next run fills it in.
  Reporting another shipment's number is NOT safe: it invents a box that does not exist.
- tracking_url: the full URL of the carrier tracking / "Track" link for this shipment. Capture it EVEN
  IF not shipped yet when a link exists; leave "" if there is genuinely none.
- delivery_date: estimated arrival date if "shipped", actual delivery date if "delivered" (YYYY-MM-DD);
  "" if only ordered
- delivery_address: the shipping address for THIS shipment
- item_name: the product name/title of this line item
- quantity: integer quantity of this product IN THIS SHIPMENT (preserve the count; see shipment rules)
- cost_per_item: NET price actually paid per unit, a number (no currency symbol). Costco often
  discounts an item (promo code, instant savings, bundle deal) and shows the discount as its own line
  ("You Saved $X" / "Instant Savings -$X") rather than reducing the item's listed price. PREFER a
  per-item figure if Costco shows one directly under this item: subtract it (divided by this
  shipment's quantity) from the listed per-unit price. If Costco instead shows only ONE discount for
  the WHOLE ORDER, that lump figure typically also covers making every digital/non-shippable item you
  are skipping (gift cards, e-delivery software, memberships) fully free — Costco prices those at a
  token amount (often $0.01 each) and the order discount includes rebating them to $0. Before applying
  the order discount to a physical item, SUBTRACT the listed price of every skipped digital item in
  the order from the order discount figure; apply only the remainder to the physical item(s) (split
  proportionally by extended price — price x quantity — if there is more than one physical SKU). Do
  NOT subtract the full order-level discount from the physical item alone — that overcounts by exactly
  the digital items' price.
- shipping: order shipping cost, a number (0 if free)
- total_cost: leave "" — it is computed as quantity x cost_per_item for this line. Fill it only if
  you cannot determine cost_per_item but can read this line's own subtotal.
- card_last4: last 4 digits of the payment card, else "". This is ORDER-LEVEL — Costco shows one
  payment card for the whole order, so it is the SAME on every shipment. Read it once and put it on
  EVERY shipment entry of the order, never blank on the 2nd+ shipment. (shipping is likewise
  order-level — the same value on every shipment entry.)

For JOB 2 re-check entries, use the SAME full shape as JOB 1 — read the order-details page and fill
EVERY field for each physical shipment (its own status, tracking_number, tracking_url, delivery_date,
delivery_address, quantity, cost_per_item, shipping, card_last4, order_url). The only exception:
Copy order_date and item_name EXACTLY as already recorded; they identify the existing row, so
re-wording an item name creates a duplicate instead of updating it.

Respond with ONLY a single raw JSON object (no markdown code fences, no commentary). Every entry (JOB 1
and JOB 2) uses this full shape:

{{
  "logged_out": false,
  "items": [
    {{
      "retailer": "Costco",
      "order_id": "...",
      "order_date": "YYYY-MM-DD",
      "shipment": "Shipment 1",
      "status": "ordered | shipped | delivered | cancelled",
      "order_url": "...",
      "tracking_number": "...",
      "tracking_url": "...",
      "delivery_date": "YYYY-MM-DD",
      "delivery_address": "...",
      "item_name": "...",
      "quantity": 1,
      "cost_per_item": 0.00,
      "shipping": 0.00,
      "total_cost": 0.00,
      "card_last4": "..."
    }}
  ]
}}

If the session was logged out, respond with {{"logged_out": true, "items": []}} and nothing else.
If there are no new qualifying orders and no re-check orders, respond with
{{"logged_out": false, "items": []}}.
"""
