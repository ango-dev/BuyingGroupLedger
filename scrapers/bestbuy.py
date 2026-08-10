from scrapers.base import BaseRetailerScraper

# In-browser TOTP generator for the password+2FA fallback. The task prompt is built ONCE at run
# start, but the agent doesn't reach the 2FA field until a minute or two later — a code generated in
# Python here would already be expired. So instead the agent runs this in the page at the moment the
# code is asked for, always yielding a fresh RFC 6238 code (SHA-1, 6 digits, 30s step) via Web Crypto.
# __SECRET__ is replaced with the base32 authenticator seed. The whole thing is a body you can pass to
# `new Function` / an async IIFE. Verified against RFC 6238 test vectors in tests/test_bestbuy_totp_js.py.
_TOTP_JS = (
    "async function bbTotp(secret){"
    "const A='ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';"
    "secret=secret.replace(/=+$/,'').replace(/\\s/g,'').toUpperCase();"
    "let bits='';for(const c of secret){const v=A.indexOf(c);if(v<0)continue;bits+=v.toString(2).padStart(5,'0');}"
    "const bytes=[];for(let i=0;i+8<=bits.length;i+=8)bytes.push(parseInt(bits.substr(i,8),2));"
    "const buf=new ArrayBuffer(8);const dv=new DataView(buf);dv.setUint32(4,Math.floor(Date.now()/30000));"
    "const k=await crypto.subtle.importKey('raw',new Uint8Array(bytes),{name:'HMAC',hash:'SHA-1'},false,['sign']);"
    "const s=new Uint8Array(await crypto.subtle.sign('HMAC',k,buf));const o=s[19]&15;"
    "const n=((s[o]&127)<<24)|((s[o+1]&255)<<16)|((s[o+2]&255)<<8)|(s[o+3]&255);"
    "return (n%1000000).toString().padStart(6,'0');}"
    "return await bbTotp('__SECRET__');"
)


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

    def _signin_block(self) -> str:
        """Sign-in instructions injected into JOB 1 when this profile has auto-auth for Best Buy.

        Empty when no `auth` is configured (then the agent reports logged_out instead of logging in —
        the pre-auto-auth behavior). Two methods:
          - "google": ride the profile's long-lived Google session — one "Continue with Google" click,
            no Best Buy password or TOTP stored.
          - "password": username + password, and (if `totp_secret` is set) authenticator-app 2FA
            computed in-browser at the moment the code is asked for (see _TOTP_JS).
        See models.profile.RetailerAuth.
        """
        auth = self.profile.auth.get(self.retailer_key)
        if auth is None:
            return ""
        if auth.method == "google":
            return self._google_signin_block(auth)
        if auth.method == "password":
            return self._password_signin_block(auth)
        return ""

    def _google_signin_block(self, auth) -> str:
        pick = (
            f'the "{auth.google_email}" account'
            if auth.google_email
            else "your Google account (there should be only one signed in)"
        )
        # Keep this FAST: every browser step re-sends the whole page (~28K tokens), and a lapsed
        # session is hit on essentially every scheduled run, so wandering on the sign-in page is the
        # single biggest avoidable cost. The instructions below pin the shortest click path and ban
        # page reads / dead-end flows (email/password, guest) that burn steps.
        return (
            "\nIf you land on a sign-in / login page, the Best Buy web session has lapsed — LOG BACK "
            "IN via Google, as FEW steps as possible. Do this and nothing else on these pages:\n"
            '  1. Find and click the "Sign in with Google" / "Continue with Google" button. If you do '
            'not see it immediately, click "See all ways to sign in" / "Other ways to sign in" ONCE '
            "to reveal it, then click it. Do NOT type an email or password on Best Buy, and do NOT "
            "use guest checkout.\n"
            f"  2. Google opens (redirect or popup). If an account chooser appears, click {pick} "
            "once; click through a single \"Continue\"/consent screen if shown.\n"
            f"  3. You are now back on Best Buy, signed in — go straight to {self.order_history_url} "
            "and carry on with the jobs below.\n"
            "Do NOT read, scroll, or run page-content evaluations on the sign-in or Google pages — "
            "just click the buttons above; extracting page text there wastes steps. The Google "
            "session is long-lived, so this normally needs no password. ONLY if Google itself now "
            "asks for a password or a verification/2FA code (its session has also expired, which is "
            'rare) do NOT attempt it — report the logged-out result ({"logged_out": true, "items": '
            "[]}) and stop.\n"
        )

    def _password_signin_block(self, auth) -> str:
        # Best Buy's password login is a 3-screen flow the agent otherwise burns ~40 steps + expensive
        # screenshots on, because the "Use password" choice is the LAST radio on the method chooser,
        # below the fold (it flails with coordinate clicks trying to find it). These exact steps +
        # ids were confirmed by CDP inspection of the live logged-out sign-in page (2026-08-09);
        # naming them keeps the login cheap and reliable. Do NOT let it screenshot/scan pages here.
        if auth.totp_secret:
            js = _TOTP_JS.replace("__SECRET__", auth.totp_secret)
            totp = (
                "  4. If Best Buy then asks for a 2-step verification / authenticator code, get a FRESH "
                "code by running EXACTLY this JavaScript in the page at that moment (it returns the "
                "current 6-digit time-based code — the code changes every 30 seconds, so run it right "
                "when the code field is shown, do NOT reuse an earlier value), then type the 6 digits "
                f"into the code field and submit:\n(async () => {{ {js} }})()\n"
            )
        else:
            totp = (
                "  4. If Best Buy asks for a 2-step verification code (SMS/email/authenticator), you "
                "cannot complete it — report the logged-out result and stop.\n"
            )
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
            + totp +
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
