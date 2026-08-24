import logging

from alerts.notifier import alert
from config.settings import settings
from scrapers.base import ApiLoginError, BaseRetailerScraper, LoggedOutError

log = logging.getLogger(__name__)

class BestBuyScraper(BaseRetailerScraper):
    retailer_name = "Best Buy"
    retailer_key = "bestbuy"
    # Purchase history is always here; order details live at
    # https://www.bestbuy.com/profile/ss/orders/order-details/<order-id>/view
    order_history_url = "https://www.bestbuy.com/purchasehistory/purchases"

    # PRIMARY PATH: Best Buy's own private endpoints (deterministic, no agent) — see scrape() ->
    # _scrape_via_api and scrapers/bestbuy_api.py. A CDP browser holds the logged-in cookie; discovery
    # reads the purchase-history page's embedded flight data and each order's detail comes from an
    # in-page fetch of /profile/ss/api/v1/orders/<id>. FALLBACK: on ANY failure (login, page shape,
    # network) scrape() alerts and defers to the Browser-Use agent (the base implementation, driven by
    # task_prompt below), which has its own sign-in handling (_signin_block). No CDP re-check either
    # way (read_tracking_page is not overridden), so agent-fallback re-checks route straight to the agent.

    def scrape(self):
        """Try the deterministic ss-api path first; fall back to the Browser-Use agent on ANY failure.

        A successful API call that finds no orders returns [] (not an error), so the agent — which
        costs money — only runs when the API path genuinely can't.
        """
        try:
            return self._scrape_via_api()
        except ApiLoginError as exc:
            # Login failure is NOT a DOM change the agent can fix — do NOT run the (paid) agent; alert
            # and skip so the user re-logs in the profile.
            log.warning("Best Buy [%s]: API login failed (%s); NOT running the agent.",
                        self.profile.label, exc)
            alert(
                f"Best Buy [{self.profile.label}]: session logged out — API login failed, agent NOT run",
                f"The Best Buy deterministic path could not sign in ({exc}). Re-login the profile "
                f"(scripts/create_profile). The agent was deliberately not run — a login failure is not "
                f"something the agent can fix.",
            )
            raise LoggedOutError(f"Best Buy:{self.profile.label}") from exc
        except Exception as exc:  # noqa: BLE001 — a NON-login failure (page shape) degrades to the agent
            reason = f"{type(exc).__name__}: {exc}"
            log.warning("Best Buy [%s]: API path failed (%s); falling back to the agent.",
                        self.profile.label, reason, exc_info=True)
            alert(
                f"Best Buy [{self.profile.label}]: API path failed — using agent fallback",
                f"The Best Buy deterministic path could not run, so this run used the Browser-Use "
                f"agent instead.\n\nReason: {reason}\n\nIf this persists, check the sign-in flow / "
                f"page shape (scripts/bestbuy_capture.py re-captures it).",
            )
            return super().scrape()

    def _scrape_via_api(self):
        from scrapers.bestbuy_api import BestBuyApiClient
        from scrapers.bestbuy_mapping import build_order_items

        # Test/ops hook: force the agent-fallback path (e.g. to validate it) without breaking anything.
        if settings.bestbuy_force_agent:
            raise RuntimeError("BESTBUY_FORCE_AGENT is set — forcing the agent fallback.")

        state = self._load_order_state()
        open_ids = {o["order_id"] for o in state.get("open_orders", [])}
        terminal_ids = set(state.get("delivered_ids", [])) | set(state.get("cancelled_ids", []))
        _, since, _ = self._date_window()

        client = BestBuyApiClient(self.profile)
        payloads = client.fetch_order_payloads(since, open_ids, terminal_ids)
        items = build_order_items(payloads, self.profile.label, known_open_ids=frozenset(open_ids))
        log.info("Best Buy [%s]: built %d ledger row(s) from the ss-api.", self.profile.label, len(items))
        return items

    # NOTE: no CDP re-check for Best Buy on the AGENT FALLBACK. Amazon has a stable in-site tracking
    # page we can read with fixed selectors; Best Buy's carrier tracking hands off to the carrier's own
    # site (UPS/FedEx/etc.), which has no stable per-account selector. So agent-fallback re-checks stay
    # on the agent (base read_tracking_page returns None -> _recheck_via_cdp routes open orders to the
    # agent). The PRIMARY ss-api path above reads tracking numbers directly, so this only matters when
    # the API path is down.

    def _signin_block(self) -> str:
        """Sign-in instructions injected into JOB 1 when this profile has auto-auth for Best Buy.

        Empty when no `auth` is configured (then the agent reports logged_out instead of logging in —
        the pre-auto-auth behavior).

        USERNAME + PASSWORD IS THE ONLY METHOD. Google SSO, Apple and in-browser
        TOTP were removed; 2-step verification must be OFF on the account, because nothing here can
        answer a challenge. See models.profile.RetailerAuth.
        """
        auth = self.profile.auth.get(self.retailer_key)
        if auth is None:
            return ""
        return self._password_signin_block(auth)

    def _password_signin_block(self, auth) -> str:
        # Best Buy's password login is a 3-screen flow the agent otherwise burns ~40 steps + expensive
        # screenshots on, because the "Use password" choice is the LAST radio on the method chooser,
        # below the fold (it flails with coordinate clicks trying to find it). These exact steps +
        # ids were confirmed by CDP inspection of the live logged-out sign-in page (2026-08-09);
        # naming them keeps the login cheap and reliable. Do NOT let it screenshot/scan pages here.
        return (
            "\nIf you land on a sign-in / login page, the Best Buy web session has lapsed — LOG BACK "
            "IN with your password. Follow these EXACT steps; do NOT take screenshots or dump page "
            "text (that is what wastes dozens of expensive steps):\n"
            f'  1. Type "{auth.username}" into the "Email Address" field (id "fld-e"; actually type it '
            'into the field, do not just set the value), leave "Keep me signed in" checked, and click '
            'the blue "Continue" button — NOT "Sign in with a Passkey" / "Apple" / "Google".\n'
            '  2. The next screen, "Choose a sign-in method", is a radio list: Text a code / Send a '
            "code to my email / Email a sign-in link / Use your Google account / Use password. Select "
            'the LAST one, "Use password" (its radio has id "password-radio") — SCROLL DOWN if it is '
            "below the fold. Do NOT pick SMS, email code, email link, Google, or passkey.\n"
            f'  3. A password field then appears — type "{auth.password}" into it and submit ("Sign In" '
            "/ Continue).\n"
            "  4. If Best Buy asks for a 2-step verification code (SMS / email / authenticator), you "
            "cannot complete it — 2-step verification is supposed to be OFF on this account. Do not "
            'attempt it: report the logged-out result ({"logged_out": true, "items": []}) and stop, '
            "so the alert says to turn it off rather than the run hanging on a challenge screen.\n"
            f"  5. Once signed in, go to {self.order_history_url} and carry on with the jobs below.\n"
            "If the credentials are rejected, the account is locked, or you otherwise cannot get in, do "
            'NOT keep retrying — report the logged-out result ({"logged_out": true, "items": []}) and '
            "stop.\n"
        )

    def task_prompt(self, skip_order_ids: list[str], recheck_orders: list[dict]) -> str:
        # Cost is driven by number of steps (browser-use sends the whole page each step), not prompt
        # length. The agent (1) scans for NEW orders and (2) re-checks open orders (Best Buy has no
        # cheap CDP path yet, so all open-order re-checks arrive here as recheck_orders).
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
                link = o.get("order_url") or (
                    f"https://www.bestbuy.com/profile/ss/orders/order-details/{o['order_id']}/view"
                )
                lines.append(
                    f"  - order_id {o['order_id']} (ordered {o.get('order_date','?')}), currently "
                    f"'{o.get('status','?')}': go to {link}"
                )
            recheck_block = (
                "\nRE-CHECK these already-recorded, not-yet-delivered orders — do NOT re-scan the "
                "purchase history for them. For each, go straight to its order-details link below and "
                "re-read EVERY physical shipment on that page (see the shipment rules below). Output "
                "one entry per (shipment x distinct physical item), filling ALL fields for each shipment "
                "just like a new order — including THIS shipment's own quantity, cost_per_item, shipping, "
                "card_last4, delivery_address and order_url. An order can SPLIT after it was recorded (a "
                "single shipment of quantity N becomes N shipments of smaller quantity): each split-off "
                "shipment is a brand-new row that needs its own full data, and the original shipment's "
                "quantity drops accordingly — so re-read every shipment's quantity/cost fresh, do NOT "
                "leave them blank. Ignore digital items. These may be older than the window above; "
                "re-check anyway:\n"
                + "\n".join(lines)
                + "\n"
            )

        new_scan_skip = ""
        if skip_order_ids:
            new_scan_skip = (
                " Skip any order whose ID is in the already-recorded list above; only fully process "
                "orders NOT in that list."
            )

        # When this profile has Google auto-auth for Best Buy, the agent logs itself back in on a
        # lapsed session; otherwise it reports logged-out and stops (do not attempt to log in).
        signin_instructions = self._signin_block() or (
            "\nIf you land on a sign-in / login page, the session is logged out: report that (see "
            "output format) and stop immediately; do not attempt to log in.\n"
        )

        return f"""You have TWO jobs on Best Buy. Do both, then return one combined JSON result.

WORK EFFICIENTLY — READ THIS FIRST. Input tokens dominate cost and every step re-sends the whole page,
so keep the number of browser actions small and never dump the full page repeatedly:
- Each extraction should run ONE JavaScript evaluate that returns COMPACT JSON (only the fields you
  need), not document.body.innerText of the whole page. Read the page in structured pieces, not blobs.
- The purchase-history list LAZY-LOADS: only the newest order or two render at first. Scroll to the
  bottom in a loop (e.g. window.scrollTo(0, document.body.scrollHeight) a few times, pausing briefly)
  until the oldest visible order is dated before {earliest} or the list stops growing — THEN read the
  whole list in one pass. Do not read-scroll-read-scroll one order at a time.
- Get the list of order_id + date + top-level status for every in-window order in that single read.
  Then for each qualifying order, navigate DIRECTLY to its details URL
  (https://www.bestbuy.com/profile/ss/orders/order-details/<order-id>/view) — do NOT click through the
  UI — and read that order's shipments in ONE evaluate. Target roughly one read per page; dozens of
  browser actions for a handful of orders means you are exploring too much.

JOB 1 — find NEW orders. Go to {self.order_history_url} and wait for it to load.{signin_instructions}Today is {today}. Record EVERY order placed on or after {earliest}
({window_phrase}) — there may be several. Best Buy lists purchases newest-first; after loading the full
list (see the lazy-load note above), take every order dated on or after {earliest} and ignore those
dated before it. Do NOT stop early after the first order.
CANCELLED orders: if an order shows as Cancelled, SKIP it — do not record it — but KEEP scanning the
other in-window orders; a cancelled order is NOT a stopping point.{new_scan_skip}
For each qualifying (non-cancelled) order, go straight to its order-details page
(https://www.bestbuy.com/profile/ss/orders/order-details/<order-id>/view) and extract it per the
shipment rules below.
{skip_line}{recheck_block}
JOB 2 is the re-check list above (empty if none). Combine JOB 1 and JOB 2 entries into "items".

SHIPMENT RULES (apply to every order-details page):
- Best Buy shows each UNIT as its OWN block on the order-details page: a product bought in quantity 7
  appears as SEVEN separate blocks, each with the same SKU and each labeled "Quantity: 1". Do NOT
  output one row per block — group the blocks into shipments and COUNT them (see below).
- A physical SHIPMENT is the set of blocks that share the SAME tracking number (for a not-yet-shipped
  order with no tracking yet, the blocks Best Buy groups under one "Arriving …"/fulfillment group).
  Blocks with the SAME tracking number are ONE shipment; blocks with DIFFERENT tracking numbers are
  DIFFERENT shipments. Whatever heading Best Buy prints above a group (if any),
  ignore it and use the numbering rule below instead. (This is why an order "splits": 5 units in one
  group become 5 shipments once each unit ships under its own tracking number.)
- IGNORE digital items entirely — do NOT output any entry for them. A group is digital if it is labeled
  "Digital Item ..." or shows "Digital Delivery" / "Ready to Redeem" / a redemption key/code, or the
  line shows $0.00 as a digital delivery. Digital items are never resold, so skip them completely.
- Output ONE entry per (shipment x distinct product). Within one shipment, if the SAME product (same
  SKU) appears in more than one block, do NOT create duplicate rows — output a single entry for it with
  quantity = the total count of that product's blocks in that shipment (e.g. 7 identical ASUS blocks
  under ONE tracking number = one entry with quantity 7, NOT seven rows and NOT quantity 1). If a
  shipment contains SEVERAL DIFFERENT products, output a separate entry for EACH product, each with its
  own quantity in that shipment. Two shipments each containing the same product are still TWO separate
  entries (one per shipment). Read quantity FRESH from the page every time (including on re-checks) —
  never default it to 1 when a shipment holds several blocks of the same product.
- Label EVERY physical shipment "Shipment 1", "Shipment 2", "Shipment 3", ... in top-to-bottom order
  (by where each shipment's first block appears), INCLUDING a single-shipment order (its one shipment
  is "Shipment 1"). Do NOT copy Best Buy's own wording — always use this numbering, so the same
  shipment gets the same label on every re-check and updates its existing row instead of creating a
  duplicate. Skipped digital groups do not consume a number: number only the physical shipments you
  actually output.
- Every physical entry for the order gets the tracking_number / tracking_url / delivery_date / status
  of ITS shipment.

Fields for each entry:
- retailer: "Best Buy"
- order_id: the Best Buy order number (e.g. "BBY01-806123456789")
- order_date: the date the order was placed, formatted exactly as YYYY-MM-DD
- shipment: "Shipment 1" / "Shipment 2" / ... — this shipment's number in top-to-bottom order (a
  single-shipment order is "Shipment 1"). Never Best Buy's own wording.
- status: judged from THIS shipment's status text on the order-details page:
    * "delivered" — the shipment says "Delivered" / "Delivered <date>"
    * "shipped"   — a tracking number IS shown and it says "Shipped" / "Out for delivery" / "Arriving
      <date>" but not yet delivered. An arrival estimate with NO tracking number is still "ordered".
    * "ordered"   — not shipped yet: no tracking number (e.g. "Preparing", "Order received", an arrival
      estimate only, or an in-store-pickup order that is merely ready for pickup)
    * "cancelled" — the whole order (or this shipment) shows Cancelled. Only use this on a re-check of
      an order already recorded; a brand-new cancelled order is skipped in JOB 1, not recorded.
- order_url: the full URL of this order's details page (address bar URL while viewing it) — same for
  every shipment of the order
- tracking_number: the carrier tracking number shown for THIS shipment; "" if not shipped yet
- tracking_url: the full URL of the carrier tracking / "Track Package" link for this shipment. Capture
  it EVEN IF not shipped yet when a link exists; leave "" if there is genuinely none.
- delivery_date: estimated arrival date if "shipped", actual delivery date if "delivered" (YYYY-MM-DD);
  "" if only ordered
- delivery_address: the shipping address for THIS shipment (for in-store pickup, the store address)
- item_name: the product name/title of this line item
- quantity: integer quantity of this product IN THIS SHIPMENT (preserve the count; see shipment rules)
- cost_per_item: price per unit, a number (no currency symbol)
- shipping: order shipping cost, a number (0 if free)
- total_cost: leave "" — it is computed as quantity x cost_per_item for this line. Fill it only if
  you cannot determine cost_per_item but can read this line's own subtotal.
- card_last4: last 4 digits of the payment card, else "". This is ORDER-LEVEL — Best Buy shows one
  payment card for the whole order (in the order summary), so it is the SAME on every shipment. Read
  it once and put it on EVERY shipment entry of the order, never blank on the 2nd+ shipment. (shipping
  is likewise order-level — the same value on every shipment entry.)

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
      "retailer": "Best Buy",
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
