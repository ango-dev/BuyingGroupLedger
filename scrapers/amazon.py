from scrapers.base import BaseRetailerScraper


class AmazonScraper(BaseRetailerScraper):
    retailer_name = "Amazon"
    retailer_key = "amazon"
    order_history_url = "https://www.amazon.com/gp/css/order-history"

    # Work is split by cost: the AGENT reads the order-details page (structure — how many shipments
    # there are, which can change when an order splits at ship time), and CDP + selectors read each
    # shipment's own tracking page (the tracking number, which Amazon doesn't show on order details).
    # The reader runs per SHIPMENT, so a split order gets every one of its tracking pages read; the
    # agent keeps re-reading the order until every shipment has a tracking number, which is the point
    # after which no further split can appear.
    cdp_recheck_enabled = True

    # Stable selectors on Amazon's package-tracking ("pt") page. Also reusable by Amazon Business,
    # which shares the same tracking page.
    tracking_number_selector = (
        "#pt-page-container-inner > div.a-row.pt-main-container > div.pt-map-outer-container"
        ".pt-map-type-static > div.pt-floating-map-card > section > div > div:nth-child(1) > div"
    )
    promise_selector = "h1.pt-promise-main-slot"

    def read_tracking_page(self, page) -> dict | None:
        """Read status/tracking#/delivery-promise from a loaded Amazon tracking page via selectors.

        Returns None if the page doesn't look like a tracking page (missing the promise headline),
        signalling the caller to fall back to the agent rather than assume "not shipped".
        """
        promise_el = page.query_selector(self.promise_selector)
        if promise_el is None:
            return None  # unexpected layout / not the tracking page → agent fallback
        promise = promise_el.inner_text().strip()

        tn_el = page.query_selector(self.tracking_number_selector)
        tracking_number = tn_el.inner_text().strip() if tn_el else ""

        lowered = promise.lower()
        if "delivered" in lowered:
            status = "delivered"
        elif tracking_number:
            status = "shipped"
        else:
            status = "ordered"

        return {"status": status, "tracking_number": tracking_number, "delivery_promise": promise}

    def task_prompt(self, skip_order_ids: list[str], recheck_orders: list[dict]) -> str:
        # Cost is driven by number of steps (browser-use sends the whole page each step), not prompt
        # length. Cheap re-checks of single-shipment open orders normally happen via CDP+selectors
        # (no agent); the agent only (1) scans for NEW orders and (2) re-checks the orders CDP
        # couldn't read OR that are multi-shipment (per-shipment status can't be read via one page).
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
                    f"https://www.amazon.com/gp/css/order-details?orderID={o['order_id']}"
                )
                lines.append(
                    f"  - order_id {o['order_id']} (ordered {o.get('order_date','?')}), currently "
                    f"'{o.get('status','?')}': go to {link}"
                )
            recheck_block = (
                "\nRE-CHECK these already-recorded, not-yet-delivered orders — do NOT re-scan the "
                "order history for them. For each, go straight to its order-details link below and "
                "re-read EVERY shipment listed on that page (see the shipment rules below). Stay on "
                "the order-details page — do NOT open any tracking page. You are checking whether the "
                "order has split into more shipments and what each one's status and tracking link is; "
                "the tracking numbers are read separately by a cheaper process. Output one entry per "
                "(shipment x distinct physical item), filling ONLY shipment, status, tracking_url and "
                'delivery_date, and leaving every other field empty (""). Ignore digital items. These '
                "may be older than the window above; re-check anyway:\n"
                + "\n".join(lines)
                + "\n"
            )

        new_scan_skip = ""
        if skip_order_ids:
            new_scan_skip = (
                " Skip any order whose ID is in the already-recorded list above; only fully process "
                "orders NOT in that list."
            )

        return f"""You have TWO jobs on Amazon. Do both, then return one combined JSON result.

JOB 1 — find NEW orders. Go to {self.order_history_url} and wait for it to load. If you land on a
sign-in / login page, the session is logged out: report that (see output format) and stop immediately;
do not attempt to log in. Today is {today}. Record ONLY orders placed on or after {earliest}
({window_phrase}). Amazon lists orders newest-first, so check from the top; the moment you reach an
order dated before {earliest}, stop scanning — everything below it is older.{new_scan_skip}
For each qualifying order, open its order-details page and extract it per the SHIPMENT RULES below.

IGNORE digital items entirely — do NOT output any entry for them. Digital items are things with no
shipment: gift cards, Kindle eBooks, digital downloads/software, Prime Video / digital rentals,
memberships and subscriptions, and credits. Only physical, shippable products are recorded.
{skip_line}{recheck_block}
JOB 2 is the re-check list above (empty if none). Combine JOB 1 and JOB 2 entries into "items".

SHIPMENT RULES (apply to every order-details page):
- An order can split into multiple shipments. On the order-details page each shipment is its own block
  with its OWN status header ("Delivered <date>", "Arriving <date>", "Out for delivery", "Now
  arriving", etc.) and its OWN "Track package" button that opens that shipment's tracking page.
- Label EVERY shipment "Shipment 1", "Shipment 2", "Shipment 3", ... in top-to-bottom order,
  INCLUDING a single-shipment order (its one shipment is "Shipment 1"). Amazon often starts an order
  as one shipment and splits it into several when it ships, so always number them — that way the
  original row updates in place and any newly-split shipments are added as new rows.
- Output ONE entry per (shipment x distinct product). If the SAME product appears more than once inside
  ONE shipment, do NOT create duplicate rows — output a single entry with quantity = the total count in
  that shipment. The same product appearing in TWO shipments is still TWO entries (one per shipment),
  each with its own shipment label and tracking.
- Each physical entry gets ITS shipment's status, tracking_url and delivery_date.
- Read everything from the order-details page. Do NOT open "Track package" pages — capture each
  shipment's track link as a URL and move on.

Fields for each entry:
- retailer: "Amazon"
- order_id: the Amazon order number (e.g. "111-2223334-5556667")
- order_date: the date the order was placed, formatted exactly as YYYY-MM-DD
- shipment: "Shipment 1" / "Shipment 2" / ... — this shipment's label (a single-shipment order is "Shipment 1")
- status: judged from THIS shipment's status text on the order-details page:
    * "delivered" — the shipment says "Delivered" / "Delivered <date>"
    * "shipped"   — it says "Arriving <date>" / "Out for delivery" / "Shipped" but not yet delivered
    * "ordered"   — not shipped yet (e.g. "Preparing for shipment", "Not yet shipped")
- order_url: the full URL of this order's details page (address bar URL while viewing it) — same for
  every shipment of the order
- tracking_number: leave "" — the tracking number lives on the tracking page and is read separately
  by a cheaper process. Only fill it if the order-details page itself shows it outright.
- tracking_url: THIS shipment's "Track package" link target. Read the link's URL from the page —
  do NOT open it. Opening every shipment's tracking page is the single most expensive thing you can
  do here, and nothing on it is needed: the link alone is enough. Only if the URL cannot be
  determined without opening it, open it once, capture the URL, and go back. Capture the link EVEN
  IF the shipment hasn't shipped yet — that page is where its tracking number appears later. Leave
  "" only if there is genuinely no "Track package" link.
- delivery_date: estimated arrival date if "shipped", actual delivery date if "delivered" (YYYY-MM-DD);
  "" if only ordered
- delivery_address: the shipping address for THIS shipment
- item_name: the product name/title of this line item
- quantity: integer quantity of this product IN THIS SHIPMENT (preserve the count; see shipment rules)
- cost_per_item: price per unit, a number (no currency symbol)
- shipping: order shipping cost, a number (0 if free)
- total_cost: order grand total, a number
- card_last4: last 4 digits of the payment card, else ""

For JOB 2 re-check entries ONLY, go to the order's details page, re-read EVERY shipment WITHOUT opening
any tracking page, and output one entry per (shipment x distinct physical item) with just retailer,
order_id, order_date, item_name, shipment, status, tracking_url and delivery_date filled — leave the
rest empty ("") and do not re-read address/costs. A single-shipment order is "Shipment 1". Copy order_date and item_name
EXACTLY as already recorded; they identify the existing row, so re-wording an item name creates a
duplicate instead of updating it. A JOB 2 entry looks like this (note the empty fields — this shape, not
the full one below):

{{
  "retailer": "Amazon",
  "order_id": "...",
  "order_date": "YYYY-MM-DD",
  "shipment": "Shipment 1",
  "status": "ordered | shipped | delivered",
  "order_url": "",
  "tracking_number": "",
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
      "retailer": "Amazon",
      "order_id": "...",
      "order_date": "YYYY-MM-DD",
      "shipment": "Shipment 1",
      "status": "ordered | shipped | delivered",
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
