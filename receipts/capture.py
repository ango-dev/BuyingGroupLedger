"""Capture each newly-seen order's receipt and link it from the ledger row.

`attach_receipts(items, profile, retailer_key)` is the whole public surface. It runs once per
profile x retailer inside main.run_scrape, between card tagging and the CSV write, so the link lands
in the SAME sheet sync as the rows it belongs to.

WHY THIS LIVES OUTSIDE THE SCRAPERS. One implementation covers all four retailers and the agent
fallback alike, and it cannot regress a deterministic client because it never runs inside one. Best
Buy reads JSON and never opens an order page; Costco has no browser at all. Hooking receipts into
each `*_api.py` would mean four copies of this — and this repo has already paid for duplicated
retailer logic once.

THE COST PROPERTY THAT MAKES IT SAFE TO RUN EVERY TIME. Storage is asked FIRST, before any browser
exists. An order whose receipt is already stored just gets its link written from the key. So the
common run — re-checking open orders, nothing new — opens ZERO cloud browsers and spends nothing.
A browser is created only when at least one order has no receipt yet, i.e. only for genuinely new
orders, and then one browser covers all of them.

FAILURE IS ALWAYS PARTIAL, NEVER FATAL. A missing receipt is an inconvenience; a missing order is
lost reimbursement money. One order failing to render does not stop the others, and the caller wraps
the whole call so nothing here can cost a row on the sheet.
"""

from __future__ import annotations

import base64
import logging

from receipts import store
from receipts.sources import (
    EXPAND_JS,
    EXTENSIONS,
    UnknownRetailerError,
    expand_selectors,
    is_capturable,
    looks_like_pdf,
    looks_logged_out,
    object_key,
    ready_selector,
    receipt_url,
)

log = logging.getLogger(__name__)

_NAV_TIMEOUT_MS = 90_000
_READY_TIMEOUT_MS = 30_000
# Let late images and webfonts land before rendering. They are exactly what block_resources=False was
# turned off for, and a receipt captured mid-load looks like a broken page.
_SETTLE_MS = 3_000


def _render_pdf(page) -> bytes:
    """Render the current page to PDF over raw CDP.

    Playwright's `page.pdf()` refuses on headful Chromium ("PDF generation is only supported for
    Headless Chromium") and Browser-Use cloud browsers are headful — they serve a live-view URL. The
    restriction is client-side in Playwright, so issuing the protocol command directly is what makes
    a PDF possible at all here. If the browser itself declines, the caller falls back to a
    full-page screenshot.
    """
    session = page.context.new_cdp_session(page)
    try:
        result = session.send("Page.printToPDF", {
            "printBackground": True,
            # Honour the page's own @page CSS — Amazon's print invoice is print-styled, so this is
            # what makes the output read as a document rather than a picture of a web page.
            "preferCSSPageSize": True,
        })
        return base64.b64decode(result["data"])
    finally:
        try:
            session.detach()
        except Exception:  # noqa: BLE001 — cleanup must not lose a PDF we already have
            pass


def _download(page, url: str) -> bytes:
    """Fetch an already-rendered PDF document over the browser context's own cookie jar.

    Playwright's APIRequestContext shares the context's cookies, so this rides exactly the same
    logged-in session the page does — no cookie extraction, no second auth.
    """
    response = page.context.request.get(url, timeout=_NAV_TIMEOUT_MS)
    if not response.ok:
        raise RuntimeError(f"downloading {url} returned HTTP {response.status}")
    return response.body()


def _expand_sections(page, retailer_key: str) -> None:
    """Open the collapsed sections a receipt would be incomplete without.

    Costco hides its entire Order Summary — payment method, subtotal, shipping, tax, grand total —
    behind a "Show Details" toggle, so without this the stored PDF shows WHAT was bought and none of
    what it cost. That is the half a buying group actually wants.

    Best-effort by design: a selector matching nothing is normal (most retailers hide nothing) and
    must never cost the capture. The JS only clicks sections that are genuinely closed, because the
    control is a toggle — firing it on an already-open accordion would collapse the thing we came
    to reveal.
    """
    selectors = expand_selectors(retailer_key)
    if not selectors:
        return
    try:
        clicked = page.evaluate(EXPAND_JS, list(selectors))
        if clicked:
            log.info("Receipts: expanded %d collapsed section(s) before rendering.", clicked)
            page.wait_for_timeout(1500)  # let the accordion finish animating open
    except Exception:  # noqa: BLE001 — a receipt missing one section beats no receipt at all
        log.warning("Receipts: could not expand collapsed sections for %s; capturing as-is.",
                    retailer_key, exc_info=True)


def _capture_one(page, retailer_key: str, order_id: str) -> tuple[bytes, str]:
    """Navigate to one order's receipt page and render it. Returns (body, extension).

    Raises rather than returning junk when the page is a sign-in wall: storing THAT would render
    perfectly, upload successfully, and mark the order as having a receipt forever — the silent
    failure this project keeps engineering against.
    """
    url = receipt_url(retailer_key, order_id)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
    except Exception:
        # A URL that resolves to a downloadable document can abort the navigation while still
        # having settled on the document's URL, so check where we ended up before giving up.
        if not looks_like_pdf(page.url or ""):
            raise

    landed = page.url or ""
    if looks_logged_out(landed):
        raise RuntimeError(
            f"receipt page for {order_id} redirected to a sign-in/captcha page ({landed}) — "
            f"refusing to store it as a receipt"
        )

    # THE RETAILER ALREADY HAS A PDF (Amazon Business) — take theirs. Rendering it
    # would capture Chrome's PDF VIEWER instead of the document, and their invoice is a better
    # receipt than any render of ours could be. Checked on the landed URL, not the retailer, so it
    # works wherever this behaviour appears.
    if looks_like_pdf(landed):
        log.info("Receipt for %s is Amazon's own PDF document; downloading rather than rendering.",
                 order_id)
        return _download(page, landed), "pdf"

    selector = ready_selector(retailer_key)
    try:
        page.wait_for_selector(selector, timeout=_READY_TIMEOUT_MS)
    except Exception:  # noqa: BLE001
        # Not fatal on its own — the selector is a heuristic and the page may simply be shaped
        # differently — but it is the signal that a layout changed, so it is logged loudly and the
        # logged-out check below still has to pass before anything is stored.
        log.warning("Receipt page for %s did not match its ready selector %r; capturing anyway.",
                    order_id, selector)
    _expand_sections(page, retailer_key)
    page.wait_for_timeout(_SETTLE_MS)

    # Re-checked after settling: a session can lapse into a sign-in redirect that only lands once
    # the page's own scripts have run, which is after the check above.
    final_url = page.url or ""
    if looks_logged_out(final_url):
        raise RuntimeError(
            f"receipt page for {order_id} redirected to a sign-in/captcha page ({final_url}) — "
            f"refusing to store it as a receipt"
        )

    try:
        return _render_pdf(page), "pdf"
    except Exception as exc:  # noqa: BLE001 — any refusal means fall back, not fail
        log.warning("Page.printToPDF unavailable for %s (%s: %s); falling back to a full-page "
                    "screenshot.", order_id, type(exc).__name__, exc)
        return page.screenshot(full_page=True), "png"


def _orders_from(items) -> dict[str, str]:
    """{order_id: order_date} for the rows in this batch.

    Keyed on the ORDER, not the ledger row key: one order is one receipt however many line items and
    shipments it becomes. Rows with a blank order id are skipped — `ledger_sync` already refuses to
    write them, so there is nothing to link a receipt to.
    """
    by_order: dict[str, dict] = {}
    for item in items:
        order_id = (getattr(item, "order_id", "") or "").strip()
        if not order_id:
            continue
        entry = by_order.setdefault(order_id, {"date": getattr(item, "order_date", "") or "",
                                               "statuses": []})
        entry["statuses"].append(getattr(item, "status", "") or "")

    # Only FINISHED orders. A receipt is captured once and never refreshed, so capturing one that is
    # still `ordered` or `shipped` would permanently store a document predating its own final totals,
    # tracking and delivery date. Judged across EVERY row, so a part-delivered split order waits for
    # its last box. See receipts.sources.is_capturable.
    orders: dict[str, str] = {}
    waiting = 0
    for order_id, entry in by_order.items():
        if is_capturable(entry["statuses"]):
            orders[order_id] = entry["date"]
        else:
            waiting += 1
    if waiting:
        log.info("Receipts: %d order(s) not finished yet (or cancelled); no receipt taken.", waiting)
    return orders


def _find_existing(retailer_key: str, order_id: str, order_date: str) -> str | None:
    """The object key of an already-stored receipt, or None.

    Probes PDF before PNG so a proper document always wins over a screenshot left by a run made
    before Page.printToPDF worked (or on a browser where it didn't).
    """
    for ext in EXTENSIONS:
        key = object_key(retailer_key, order_id, order_date, ext)
        if store.exists(key):
            return key
    return None


def attach_receipts(items, profile, retailer_key: str, browser_factory=None) -> None:
    """Set `receipt_url` on every row whose order has (or can be given) a stored receipt.

    Mutates `items` in place. `browser_factory` exists so tests can assert the far more important
    negative — that NO browser is created when every receipt is already stored.
    """
    if not items:
        return
    if not store.is_configured():
        store._warn_once()
        return

    orders = _orders_from(items)
    if not orders:
        return

    # PASS 1 — storage only. No browser is created here, which is the point.
    links: dict[str, str] = {}
    missing: dict[str, str] = {}
    for order_id, order_date in orders.items():
        try:
            key = _find_existing(retailer_key, order_id, order_date)
        except Exception:  # noqa: BLE001 — a storage outage must not cost the scrape
            log.warning("Could not check stored receipts for %s; skipping receipt capture this run.",
                        order_id, exc_info=True)
            continue
        if key:
            links[order_id] = store.link_for(key)
        else:
            missing[order_id] = order_date

    if not missing:
        _apply(items, links)
        log.info("Receipts [%s / %s]: %d order(s) already stored; no browser opened.",
                 retailer_key, profile.label, len(links))
        return

    # PASS 2 — one browser for every order that still needs one.
    log.info("Receipts [%s / %s]: capturing %d new receipt(s) (%d already stored).",
             retailer_key, profile.label, len(missing), len(links))

    if browser_factory is None:
        # Imported here so this module stays importable (and testable) without Playwright.
        from scrapers.cdp import CdpBrowser

        # block_resources=False: a receipt stripped of its logos and webfonts is a poor document,
        # and this is the one read where the extra proxy bandwidth buys something. Bounded to new
        # orders only by pass 1.
        def browser_factory(prof):  # noqa: E731 — a def reads better than a lambda here
            return CdpBrowser(prof, block_resources=False)

    failures = 0
    try:
        with browser_factory(profile) as page:
            for order_id, order_date in missing.items():
                try:
                    body, ext = _capture_one(page, retailer_key, order_id)
                    key = object_key(retailer_key, order_id, order_date, ext)
                    links[order_id] = store.put(key, body, ext)
                except UnknownRetailerError:
                    # Misconfiguration, not a per-order problem — every other order would fail the
                    # same way, so stop rather than loop through them all.
                    raise
                except Exception:  # noqa: BLE001
                    failures += 1
                    log.warning("Receipt capture failed for %s / %s; the order is still recorded.",
                                retailer_key, order_id, exc_info=True)
    finally:
        # Whatever was captured before a failure is still worth linking.
        _apply(items, links)

    if failures:
        log.warning("Receipts [%s / %s]: %d of %d capture(s) failed.",
                    retailer_key, profile.label, failures, len(missing))


def _apply(items, links: dict[str, str]) -> None:
    """Write each order's link onto every row of that order."""
    for item in items:
        link = links.get((getattr(item, "order_id", "") or "").strip())
        if link:
            item.receipt_url = link
