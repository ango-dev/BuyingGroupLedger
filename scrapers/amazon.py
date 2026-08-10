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

    # Stable, semantically-named selectors on Amazon's package-tracking ("pt") page. Also reusable
    # by Amazon Business, which shares the same tracking page. The carrier tracking number lives in
    # a "Delivery Info" card that Amazon renders ONLY once the shipment has shipped — so the card's
    # presence is the "has shipped" signal and the number sits in a labelled child of it. (These
    # replace an earlier deep nth-child path that pointed at an always-empty node and never read a
    # number, which is why a shipped order kept reading as 'ordered'.)
    promise_selector = "h1.pt-promise-main-slot"
    delivery_card_selector = ".pt-delivery-card-wrapper"
    tracking_number_selector = ".pt-delivery-card-trackingId"  # text reads "Tracking ID: <number>"

    def _read_tracking_number(self, page) -> str:
        """The carrier number from the delivery card, or "" if that element isn't present.

        The element's text reads like "Tracking ID: TBA999000000001"; strip the label and keep the
        number. Kept separate so read_tracking_page can tell "no number element" from "no number".
        """
        el = page.query_selector(self.tracking_number_selector)
        if el is None:
            return ""
        raw = el.inner_text().strip()
        return raw.split(":", 1)[1].strip() if ":" in raw else raw

    def read_tracking_page(self, page) -> dict | None:
        """Read status/tracking#/delivery-promise from a loaded Amazon tracking page via selectors.

        Returns None to tell the caller to fall back to the agent. That happens when the page
        doesn't look like a tracking page (the promise headline is missing), OR when the shipment
        HAS shipped (the Delivery Info card is present) but no tracking number can be pulled out of
        it — a selector that has gone stale under us. Escalating rather than silently reporting
        'ordered' keeps a shipped order from looking unshipped forever.

        Crucially, a shipment that simply hasn't shipped yet has NO delivery card at all (the page
        shows only an "Arriving <x>" estimate with the stepper at "Ordered"); that is a legitimate
        'ordered', not a failure, and must not escalate — otherwise every un-shipped order would
        wastefully hit the agent every run.
        """
        promise_el = page.query_selector(self.promise_selector)
        if promise_el is None:
            return None  # unexpected layout / not the tracking page → agent fallback
        promise = promise_el.inner_text().strip()
        lowered = promise.lower()

        card = page.query_selector(self.delivery_card_selector)
        tracking_number = self._read_tracking_number(page) if card is not None else ""

        # Delivered is stated by the promise headline and is terminal; record it with whatever number
        # is present (blank is fine — a prior run usually captured it and _merge_row preserves it).
        if "delivered" in lowered:
            return {"status": "delivered", "tracking_number": tracking_number, "delivery_promise": promise}

        if card is None:
            # Not shipped yet — no delivery card, so no number. Legitimate 'ordered', not a miss.
            return {"status": "ordered", "tracking_number": "", "delivery_promise": promise}

        if not tracking_number:
            # Shipped (card present) but the number selector came back empty → it went stale → agent.
            return None

        return {"status": "shipped", "tracking_number": tracking_number, "delivery_promise": promise}

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
                "the order-details page — do NOT open any tracking page. Output one entry per "
                "(shipment x distinct physical item), filling ALL fields for each shipment just like a "
                "new order — including THIS shipment's own quantity, cost_per_item, shipping, "
                "card_last4, delivery_address, order_url and tracking_url. An order can SPLIT after it "
                "was recorded (a single shipment of quantity N becomes N shipments of smaller "
                "quantity): each split-off shipment is a brand-new row that needs its own full data, "
                "and the original shipment's quantity drops accordingly — so re-read every shipment's "
                "quantity/cost fresh, do NOT leave them blank. The ONE exception is tracking_number: "
                'leave it "" — Amazon\'s number lives on the tracking page and is read separately by a '
                "cheaper process, so filling it here would clobber that. Ignore digital items. These "
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
do not attempt to log in. Today is {today}. Record EVERY order placed on or after {earliest}
({window_phrase}) — there may be several. Amazon lists orders newest-first, so work down from the top
and check EACH order's date; if the page shows a time-period filter, make sure it covers back to
{earliest}, and page/scroll through all orders in the window. Only stop once you reach an order dated
before {earliest} (everything below it is older). Do NOT stop early after the first order.
CANCELLED orders: if an order shows as Cancelled, SKIP it — do not record it — but KEEP scanning older
orders; a cancelled order is NOT a stopping point.{new_scan_skip}
For each qualifying (non-cancelled) order, open its order-details page and extract it per the SHIPMENT
RULES below.

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
    * "cancelled" — the whole order (or this shipment) shows Cancelled. Only use this on a re-check of
      an order already recorded; a brand-new cancelled order is skipped in JOB 1, not recorded.
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
- total_cost: leave "" — it is computed as quantity x cost_per_item for this line. Fill it only if
  you cannot determine cost_per_item but can read this line's own subtotal.
- card_last4: last 4 digits of the payment card, else "". This is ORDER-LEVEL — Amazon shows one
  payment method for the whole order, so it is the SAME on every shipment. Read it once and put it on
  EVERY shipment entry of the order, never blank on the 2nd+ shipment. (shipping is likewise
  order-level — the same value on every shipment entry.)

For JOB 2 re-check entries, use the SAME full shape as JOB 1 — read the order-details page WITHOUT
opening any tracking page and fill EVERY field for each physical shipment (its own status, tracking_url,
delivery_date, delivery_address, quantity, cost_per_item, shipping, card_last4, order_url). A
single-shipment order is "Shipment 1". Two exceptions: (1) leave tracking_number "" — Amazon's number is
read separately by a cheaper process, and filling it here would clobber that; (2) Copy order_date and item_name
EXACTLY as already recorded; they identify the existing row, so re-wording an item name creates a
duplicate instead of updating it.

Respond with ONLY a single raw JSON object (no markdown code fences, no commentary). Every entry (JOB 1
and JOB 2) uses this full shape (JOB 2 entries leave tracking_number ""):

{{
  "logged_out": false,
  "items": [
    {{
      "retailer": "Amazon",
      "order_id": "...",
      "order_date": "YYYY-MM-DD",
      "shipment": "Shipment 1",
      "status": "ordered | shipped | delivered | cancelled",
      "order_url": "...",
      "tracking_number": "",
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
