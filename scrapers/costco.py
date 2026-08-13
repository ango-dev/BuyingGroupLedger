import logging
import os
from datetime import datetime, timedelta, timezone

from alerts.notifier import alert
from scrapers.base import ApiLoginError, BaseRetailerScraper, LoggedOutError
from scrapers.costco_mapping import ORDER_DETAILS_URL, build_order_items

log = logging.getLogger(__name__)


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
        try:
            return self._scrape_via_api()
        except ApiLoginError as exc:
            # Auth failure (dead/rotated refresh token) is NOT a schema change the agent can fix — do
            # NOT run the (paid) agent; alert and skip so the user re-authorizes the token.
            log.warning("Costco [%s]: API auth failed (%s); NOT running the agent.",
                        self.profile.label, exc)
            alert(
                f"Costco [{self.profile.label}]: API auth failed — agent NOT run",
                f"The Costco API could not authenticate ({exc}). Re-authorize with "
                f"`python -m scripts.costco_token --label {self.profile.label} ...`. The agent was "
                f"deliberately not run — an auth failure is not something the agent can fix.",
            )
            raise LoggedOutError(f"Costco:{self.profile.label}") from exc
        except Exception as exc:  # noqa: BLE001 — a NON-auth failure (schema/network) degrades to the agent
            reason = f"{type(exc).__name__}: {exc}"
            log.warning("Costco [%s]: API path failed (%s); falling back to the agent.",
                        self.profile.label, reason, exc_info=True)
            alert(
                f"Costco [{self.profile.label}]: API path failed — using agent fallback",
                f"The Costco GraphQL path could not run, so this run used the Browser-Use agent "
                f"instead.\n\nReason: {reason}\n\nIf this persists, re-authorize the API with "
                f"`python -m scripts.costco_token --label {self.profile.label} ...`.",
            )
            return super().scrape()

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
        if os.getenv("COSTCO_FORCE_AGENT"):
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
  separate boxes get separate tracking numbers. Each shipment has its OWN status, tracking number,
  tracking link, and estimated/actual delivery date. Whatever heading Costco prints above a group (if
  any), ignore it and use the numbering rule below instead.
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
- Every physical entry for the order gets the tracking_number / tracking_url / delivery_date / status
  of ITS shipment.

Fields for each entry:
- retailer: "Costco"
- order_id: the Costco order number
- order_date: the date the order was placed, formatted exactly as YYYY-MM-DD
- shipment: "Shipment 1" / "Shipment 2" / ... — this shipment's number in top-to-bottom order (a
  single-shipment order is "Shipment 1"). Never Costco's own wording.
- status: judged from THIS shipment's status text on the order-details page:
    * "delivered" — the shipment says "Delivered" / "Delivered <date>"
    * "shipped"   — a tracking number IS shown and it says "Shipped" / "Out for delivery" / "Arriving
      <date>" but not yet delivered. An arrival estimate with NO tracking number is still "ordered".
    * "ordered"   — not shipped yet: no tracking number (e.g. "Preparing", "Order received", an arrival
      estimate only)
    * "cancelled" — the whole order (or this shipment) shows Cancelled. Only use this on a re-check of
      an order already recorded; a brand-new cancelled order is skipped in JOB 1, not recorded.
- order_url: the full URL of this order's details page (address bar URL while viewing it) — same for
  every shipment of the order
- tracking_number: the carrier tracking number shown for THIS shipment; "" if not shipped yet. Costco
  shows it on the order-details / shipment view; if the number only appears behind a "Track" link,
  follow that link once to read it, then come back.
- tracking_url: the full URL of the carrier tracking / "Track" link for this shipment. Capture it EVEN
  IF not shipped yet when a link exists; leave "" if there is genuinely none.
- delivery_date: estimated arrival date if "shipped", actual delivery date if "delivered" (YYYY-MM-DD);
  "" if only ordered
- delivery_address: the shipping address for THIS shipment
- item_name: the product name/title of this line item
- quantity: integer quantity of this product IN THIS SHIPMENT (preserve the count; see shipment rules)
- cost_per_item: price per unit, a number (no currency symbol)
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
