"""Where each retailer's receipt lives, and where its captured copy is stored.

Pure string work — no network, no browser, no boto3 — so every rule here is provable offline. The
two functions answer the two questions the capture step asks per order: which page do I render, and
under what object name does it go.

WHY A TABLE AND NOT A METHOD ON EACH SCRAPER. A receipt page is not part of any scraper's data path
— Best Buy reads JSON and never opens an order page at all, and Costco has no browser whatsoever.
Putting the URLs here keeps receipts entirely outside the deterministic clients, so a change to one
can never regress the other, and it keeps the whole table readable in one screen.
"""

from __future__ import annotations

# Amazon publishes a genuine PRINT INVOICE — a stripped, print-styled page with the items, per-unit
# prices, tax, totals, payment method and both addresses, and none of the recommendation carousels
# that clutter order-details. It is the closest thing any of these four retailers has to a document
# you would attach to a support ticket, which is exactly what a BFMR suffixed-tracking ticket needs.
#Found during the 2026-08-10 capture; scripts/amazon_capture.py has been loading
# it as a fixture ever since.
_AMAZON_PRINT_INVOICE = "https://www.amazon.com/gp/css/summary/print.html?orderID={}"

# Best Buy and Costco have no print view we know of, so their receipt is the order-details page each
# retailer's mapping module ALREADY builds into every row's Order Link. Kept spelled out here rather
# than imported from the mappings: importing scrapers/ would drag Playwright and curl_cffi into a
# module whose whole point is to stay dependency-free and offline-testable. tests/test_receipt_
# sources.py asserts these stay identical to the mappings' own constants, so the duplication cannot
# drift silently.
_BESTBUY_ORDER_DETAILS = "https://www.bestbuy.com/profile/ss/orders/order-details/{}/view"
_COSTCO_ORDER_DETAILS = (
    "https://www.costco.com/myaccount/#/app/4900eb1f-0c10-4bd9-99c3-c59e6c1ecebf/orderdetails/{}"
)

RECEIPT_URLS = {
    "amazon": _AMAZON_PRINT_INVOICE,
    # Amazon Business runs on amazon.com and shares the consumer order-details and package-tracking
    # pages verbatim, so the print invoice is expected to work unchanged. UNVERIFIED
    # until scripts/receipt_probe.py is run against the business account — if it 404s or redirects,
    # this is the one line that changes (to the /gp/css/order-details form).
    "amazon-business": _AMAZON_PRINT_INVOICE,
    "bestbuy": _BESTBUY_ORDER_DETAILS,
    "costco": _COSTCO_ORDER_DETAILS,
}

# What has to be on the page before it is worth rendering. These are WAITED ON, not slept through:
# a fixed sleep is either too short (we capture a spinner) or too long (we pay for it on every
# order). Costco's is the one that matters — its order page is a hash-route SPA, so
# `domcontentloaded` fires while the document is still an empty shell.
READY_SELECTORS = {
    # LIVE-VERIFIED 2026-08-15 (scripts/receipt_probe.py). The print invoice is NOT the old
    # table-layout document — it renders the same `#orderDetails` data-component tree that
    # scrapers/amazon_mapping.py already parses on order-details, so this is the same anchor the
    # deterministic path trusts. (An earlier guess of "table" never matched: the page contains zero
    # <table> elements.)
    "amazon": "#orderDetails",
    # Amazon BUSINESS never reaches a selector wait — its print-invoice URL 302s to Amazon's own
    # invoice PDF, which is downloaded rather than rendered (see looks_like_pdf). Kept in step with
    # consumer Amazon for the day that redirect stops happening.
    "amazon-business": "#orderDetails",
    # LIVE-VERIFIED 2026-08-15: matched, and the page carries its own `@media print` rules plus
    # print-only containers (that "Print Receipt" button just calls window.print()). Page.printToPDF
    # renders in PRINT media, so the stored PDF is the clean receipt — 3 pages of Best Buy's own
    # HumanBBYDigital font — not the navigation-and-footer page a screenshot would catch.
    "bestbuy": "main, [class*='order-details']",
    # LIVE-VERIFIED 2026-08-21 against the rendered SPA. Costco's order page is a hash-route SPA, so
    # `domcontentloaded` fires while the document is still an empty shell — this is the one retailer
    # where waiting on the right node genuinely matters.
    #
    # NOTE these are `automation-id` ATTRIBUTES, not ids: the page renders
    # `<div automation-id="orderNumber">Order Number 1399000015</div>`, so `#orderNumber` matches
    # nothing. (The earlier guess of `[class*='order-details']` matched nothing either — Costco's
    # classes are hashed MUI names like `css-1pzz4na` — and cost a 30s timeout on every capture.)
    # `#detail-costcoOrder` is a genuine id and is kept as a second chance.
    "costco": '[automation-id="orderNumber"], #detail-costcoOrder',
}

# Sections a retailer COLLAPSES by default and that a receipt is incomplete without. Clicked before
# rendering, best-effort — a selector that matches nothing is not an error.
#
# Costco hides the whole Order Summary (payment method, subtotal, shipping, tax, grand total) behind
# a "Show Details" toggle, so a straight render produces a receipt with the ITEM but none of the
# MONEY — found 2026-08-21 by reading the captured PDFs' text rather than trusting that the page
# looked complete. Keep these SURGICAL: the same page has ~29 other `aria-expanded="false"` nodes,
# every one of them site chrome (footer accordions, nav dropdowns, tooltips) that would only add
# noise to the document.
EXPAND_SELECTORS = {
    "costco": ('[automation-id="HideorExpandOrderSummary"]',),
    # Best Buy hides the payment method behind a "Payment Details" disclosure. NOTE the toggle is
    # itself marked `hidden-print`, so Best Buy may deliberately keep payment off a printed receipt
    # — clicking it costs nothing either way, and the shipping address and totals are already in the
    # render. Whether the revealed panel survives print media is recorded in the design notes.
    "bestbuy": ('[data-test="order-details-summary__toggle"]',),
}

# Only ever clicks a section that is genuinely CLOSED. The toggle is a toggle: firing it blindly on
# an already-open accordion would COLLAPSE the very thing we came to reveal, so a run where Costco
# ships it open by default would silently start producing worse receipts than one where it doesn't.
EXPAND_JS = """
(selectors) => {
  let clicked = 0;
  for (const sel of selectors) {
    for (const el of document.querySelectorAll(sel)) {
      const header = el.closest('[aria-expanded]');
      if (!header || header.getAttribute('aria-expanded') === 'false') { el.click(); clicked++; }
    }
  }
  return clicked;
}
"""


def expand_selectors(retailer_key: str) -> tuple:
    """Collapsed sections to open before rendering. Empty for retailers that hide nothing."""
    return EXPAND_SELECTORS.get(retailer_key, ())


# Text that means the DOCUMENT ITSELF says the order has not shipped yet, so it is not the final
# invoice and must not be stored.
#
# WHY CHECK THE DOCUMENT AND NOT THE STATUS. A receipt is stored once and never refreshed, and these
# receipts substantiate COGS at tax time — a pre-shipment invoice can diverge from what was actually
# paid (partial shipment, price adjustment, outright cancellation), and Amazon generally charges AT
# shipment, so an unshipped order may not be a booked expense at all. Status is a claim ABOUT the
# document; this is the document. Live the two disagreed: the ledger said `shipped` (it
# had a tracking number — Amazon Logistics assigns `TBA…` at LABEL CREATION, not dispatch) while
# Amazon's own invoice said `Not Yet Shipped`. The invoice was right.
#
# Only Amazon Business has a marker, and deliberately so: its receipt is Amazon's OWN generated PDF,
# which is explicitly titled `Final Details for Order #…` once shipped and `Details for Order #…`
# before. Consumer Amazon's rendered print.html carries NO finality wording in either direction
# (verified: `Order Summary` / `Grand Total` / `Payment`, never `Final Details` or `Not Yet
# Shipped`), so inventing a marker for it would only produce false rejections. Best Buy and Costco
# likewise. This table is the extension point if that ever changes.
NOT_FINAL_MARKERS = {
    "amazon-business": ("Not Yet Shipped",),
}


def not_final_reason(retailer_key: str, text: str) -> str | None:
    """The marker proving this document is pre-shipment, or None if it looks final.

    Positive-match only: a retailer with no markers, or text we could not extract, returns None and
    the receipt is stored. See _reject_if_not_final in receipts/capture.py for why this fails open.
    """
    if not text:
        return None
    for marker in NOT_FINAL_MARKERS.get(retailer_key, ()):
        if marker.lower() in text.lower():
            return marker
    return None


# Substrings that mean the page we landed on is a sign-in wall or a bot check rather than a receipt.
# WITHOUT this the capture succeeds, uploads a perfectly rendered PDF of a login form, and marks the
# order done forever — the exact silent failure this project keeps engineering against. Matched
# against the FINAL url after any redirect.
_LOGGED_OUT_URL_MARKERS = (
    "/ap/signin", "/ap/mfa", "/ap/cvf", "/errors/validatecaptcha",
    "/identity/signin", "/identity/global/signin",
    "signin.costco.com", "/logon", "/login",
)


class UnknownRetailerError(KeyError):
    """No receipt source is configured for that retailer key."""


def receipt_url(retailer_key: str, order_id: str) -> str:
    """The page to render as `order_id`'s receipt.

    Raises rather than returning "" for an unknown retailer: a blank URL would navigate nowhere,
    render the previous page, and store THAT as the receipt — a wrong document is worse than none.
    """
    try:
        template = RECEIPT_URLS[retailer_key]
    except KeyError:
        raise UnknownRetailerError(
            f"No receipt source configured for retailer {retailer_key!r}. Add one to "
            f"receipts/sources.py:RECEIPT_URLS (known: {', '.join(sorted(RECEIPT_URLS))})."
        ) from None
    if not order_id:
        raise ValueError("order_id is required to build a receipt URL")
    return template.format(order_id)


def ready_selector(retailer_key: str) -> str:
    """A CSS selector to wait for before rendering. Falls back to <body> for an unlisted retailer."""
    return READY_SELECTORS.get(retailer_key, "body")


def looks_logged_out(url: str) -> bool:
    """Did we land on a sign-in / captcha page instead of the receipt?"""
    lowered = (url or "").lower()
    return any(marker in lowered for marker in _LOGGED_OUT_URL_MARKERS)


def looks_like_pdf(url: str) -> bool:
    """Did the receipt URL redirect to an actual PDF document?

    LIVE-FOUND 2026-08-15. Amazon BUSINESS answers the print-invoice URL with a 302 to
    `/documents/download/<uuid>/order-document.pdf` — Amazon's own official invoice, the best
    possible receipt. Chrome then displays it in its built-in PDF viewer.

    This matters because rendering that with Page.printToPDF captures THE VIEWER, not the document:
    the probe's output came back with Roboto and SegoeFluentIcons (Chrome's UI fonts) and 22 text
    operators, versus AmazonEmber and 823 for the consumer HTML page. So a PDF landing must be
    DOWNLOADED, never re-rendered. Consumer Amazon still serves HTML and takes the render path; the
    check is on the landed URL rather than the retailer, so either behaviour is handled wherever it
    shows up.
    """
    path = (url or "").split("?", 1)[0].split("#", 1)[0].lower()
    return path.endswith(".pdf")


def object_key(retailer_key: str, order_id: str, order_date: str, ext: str) -> str:
    """The object name a receipt is stored under.

        receipts/amazon/2026-08/113-9990003-1234567.pdf

    Keyed on ORDER ID, not on the ledger's row key: one order is one document, however many line
    items or shipments it becomes. That is also what makes the capture idempotent — a re-check run
    re-derives the identical key, finds the object already there, and opens no browser.

    The YYYY-MM folder is for humans browsing the bucket; a missing or malformed order_date falls
    back to `unknown` rather than raising, since a receipt filed under an odd prefix is still a
    receipt and losing it over a date string would be the wrong trade.
    """
    if not order_id:
        raise ValueError("order_id is required to build an object key")
    month = order_date[:7] if len(order_date or "") >= 7 and order_date[4] == "-" else "unknown"
    # Order ids are alphanumeric with dashes on every retailer here, but a stray slash would silently
    # create a nested prefix, so it is neutralized rather than trusted.
    safe_id = order_id.strip().replace("/", "_")
    return f"receipts/{retailer_key}/{month}/{safe_id}.{ext.lstrip('.')}"


# WHEN an order is worth capturing. A receipt is captured ONCE and never refreshed, so the question
# is not "how often" (storage is checked before any browser opens, so a re-check run captures
# nothing) but "at what moment is the document worth taking".
#
# THE RULE: once the order has SHIPPED.
#
# Waiting for delivery was wrong for the job this serves. A receipt is proof of purchase for a
# buying group, and that is needed AT SHIP TIME — when tracking is submitted, and when BFMR asks for
# proof to support a suffixed tracking number. Worse, **a lost package never delivers**, so the one
# order where proof matters most — an insurance claim — is exactly the one a delivered-only rule
# would never capture.
#
# Waiting also bought less than it looked like it did: the receipt is an ORDER-level invoice (items,
# quantities, prices, totals, payment method, ship-to), all of which exists the moment it ships. A
# shipment split does not change it, because it was never a per-shipment document.
#
# `delivered` is included, and that is not redundant: an order can be FIRST SEEN already delivered
# (fast shipping, or discovered outside the lookback window), and on a shipped-only rule it would
# never be captured at all.
#
# `ordered` is excluded — nothing has moved yet and the order can still be cancelled outright.
# `cancelled` is excluded — the order never completed, so its receipt proves nothing.
CAPTURE_STATUSES = ("shipped", "delivered")

# `paid` and `return` are the BUYING GROUP's outcomes, and no scraper can ever emit them —
# sync_tracking writes them to the sheet AFTER a scrape, so they can never appear in the rows a live
# capture sees. They exist here only for scripts/backfill_receipts.py, which reads statuses off the
# SHEET, where a settled order genuinely is finished and genuinely may need proving later.
SETTLED_STATUSES = ("paid", "return")


def is_capturable(statuses, include_settled: bool = False) -> bool:
    """Has this order shipped (or better), and is it worth a receipt?

    Takes EVERY row's status because one order can be several shipments, and asks whether ANY of
    them has moved. A part-shipped order IS worth capturing: the invoice covers the whole order, so
    there is nothing to wait for, and waiting only widens the window in which the receipt is not
    there when someone asks for it.

    `include_settled` widens the rule to the buying group's own terminal outcomes. Only the backfill
    passes it — see SETTLED_STATUSES.
    """
    good = CAPTURE_STATUSES + (SETTLED_STATUSES if include_settled else ())
    return any((s or "").strip().lower() in good for s in statuses)


# Extensions a stored receipt can carry, newest-preferred first: PDF is what we try to render, PNG is
# the fallback when a headful browser refuses Page.printToPDF. The capture step probes for an
# existing object in this order, so a PDF always wins over a PNG left by an older run.
EXTENSIONS = ("pdf", "png")

CONTENT_TYPES = {"pdf": "application/pdf", "png": "image/png"}
