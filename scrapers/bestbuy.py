from scrapers.base import BaseRetailerScraper


class BestBuyScraper(BaseRetailerScraper):
    retailer_name = "Best Buy"
    retailer_key = "bestbuy"
    # Purchase history is always here; order details live at
    # https://www.bestbuy.com/profile/ss/orders/order-details/<order-id>/view
    order_history_url = "https://www.bestbuy.com/purchasehistory/purchases"

    # NOTE: no CDP re-check yet for Best Buy. Amazon has a stable in-site tracking page we can read
    # with fixed selectors; Best Buy's carrier tracking usually hands off to the carrier's own site
    # (UPS/FedEx/etc.), which has no stable per-account selector we trust. So Best Buy re-checks stay
    # on the agent (base read_tracking_page returns None -> _recheck_via_cdp routes open orders to the
    # agent without opening a CDP session). To add cheap re-checks later, mirror amazon.py: define the
    # tracking-page selectors + override read_tracking_page() once a live order lets us confirm them.

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
                "one entry per (shipment x distinct physical item), filling ONLY shipment, status, "
                "tracking_number, tracking_url and delivery_date, and leaving every other field empty "
                '(""). Ignore digital items. These may be older than the window above; re-check anyway:\n'
                + "\n".join(lines)
                + "\n"
            )

        new_scan_skip = ""
        if skip_order_ids:
            new_scan_skip = (
                " Skip any order whose ID is in the already-recorded list above; only fully process "
                "orders NOT in that list."
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

JOB 1 — find NEW orders. Go to {self.order_history_url} and wait for it to load. If you land on a
sign-in / login page, the session is logged out: report that (see output format) and stop immediately;
do not attempt to log in. Today is {today}. Record EVERY order placed on or after {earliest}
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
- An order can split into multiple fulfillment groups. Each physical shipment has its OWN status,
  tracking number, tracking link, and estimated/actual delivery date. Whatever heading Best Buy prints
  above a group (if any), ignore it and use the numbering rule below instead.
- IGNORE digital items entirely — do NOT output any entry for them. A group is digital if it is labeled
  "Digital Item ..." or shows "Digital Delivery" / "Ready to Redeem" / a redemption key/code, or the
  line shows $0.00 as a digital delivery. Digital items are never resold, so skip them completely.
- Output ONE entry per (physical shipment x distinct product) within the order. If the SAME product
  appears more than once inside ONE shipment, do NOT create duplicate rows — output a single entry for
  it with quantity = the total count in that shipment. Two shipments each containing the same product
  are still TWO separate entries (one per shipment), each with its own shipment label and tracking.
- Label EVERY physical shipment "Shipment 1", "Shipment 2", "Shipment 3", ... in top-to-bottom order,
  INCLUDING a single-shipment order (its one shipment is "Shipment 1"). Do NOT copy Best Buy's own
  wording — always use this numbering, so the same shipment gets the same label on every re-check and
  updates its existing row instead of creating a duplicate. Skipped digital groups do not consume a
  number: number only the physical shipments you actually output.
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
- card_last4: last 4 digits of the payment card, else ""

For JOB 2 re-check entries ONLY, fill just retailer, order_id, order_date, item_name, shipment, status,
tracking_number, tracking_url, delivery_date and leave the rest empty ("") — do not re-read
address/costs. Copy order_date and item_name EXACTLY as already recorded; they identify the existing
row, so re-wording an item name creates a duplicate instead of updating it. A JOB 2 entry looks like
this (note the empty fields — this shape, not the full one below):

{{
  "retailer": "Best Buy",
  "order_id": "...",
  "order_date": "YYYY-MM-DD",
  "shipment": "Shipment 1",
  "status": "ordered | shipped | delivered | cancelled",
  "order_url": "",
  "tracking_number": "...",
  "tracking_url": "...",
  "delivery_date": "YYYY-MM-DD",
  "delivery_address": "",
  "item_name": "...",
  "quantity": null,
  "cost_per_item": null,
  "shipping": null,
  "total_cost": null,
  "card_last4": ""
}}

Respond with ONLY a single raw JSON object (no markdown code fences, no commentary). JOB 1 entries use
the full shape below; JOB 2 entries use the trimmed shape above:

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
