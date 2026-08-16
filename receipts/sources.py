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
    # UNVERIFIED. The 2026-08-15 probe never got to test it: costco.com redirected straight to
    # signin.costco.com, because the profile's BROWSER session had lapsed independently of the
    # GraphQL refresh token that Costco's data path runs on (that path opens no browser at all, so
    # nothing keeps this session warm). looks_logged_out caught it and production skips the capture.
    # Re-run scripts/receipt_probe.py once the profile is logged back into costco.com in a browser.
    "costco": "[class*='order-details'], [class*='orderDetails'], table",
}

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


# Extensions a stored receipt can carry, newest-preferred first: PDF is what we try to render, PNG is
# the fallback when a headful browser refuses Page.printToPDF. The capture step probes for an
# existing object in this order, so a PDF always wins over a PNG left by an older run.
EXTENSIONS = ("pdf", "png")

CONTENT_TYPES = {"pdf": "application/pdf", "png": "image/png"}
