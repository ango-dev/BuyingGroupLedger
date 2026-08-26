"""
RECON tool: capture Amazon's order-page traffic + markup from a logged-in session.

One-off developer tool used to DECIDE and BUILD the deterministic, agent-free Amazon path (the
Best-Buy/Costco-style scraper that replaces the paid Browser-Use agent — see the design notes). It does NOT
run in production. It:

  1. Reconnects to a profile's Browser-Use cloud browser over CDP (scrapers.cdp.CdpBrowser) on the
     Amazon profile (profile-bravo). Amazon sessions are long-lived, so it does NOT try to log in — if
     the session has lapsed it dumps diagnostics and bails (Amazon login has OTP/2FA; the production
     path detects logged-out -> alert -> agent fallback, it never auto-logs-in).
  2. HOOKS THE NETWORK (page.on("request") / page.on("response")) and records EVERY request's
     url/method/status/content-type into a manifest, saving the full body of any `application/json`
     response — this is the decision gate: does Amazon serve order/shipment/cost data over a JSON/XHR
     endpoint (build an API path like Best Buy) or only as server-rendered HTML (build an in-page
     fetch + HTML-parse path)?
  3. Loads, in ONE session: order-history (`/gp/css/order-history` and `/your-orders/orders`), one
     order-details page (BOTH `/gp/css/order-details?orderID=` and `/your-orders/order-details?
     orderID=`), one print-invoice (`/gp/css/summary/print.html?orderID=`), and one package-tracking
     ("pt") page (followed from the order-details "Track package" href).
  4. For every page dumps the HTML, a screenshot, and any embedded JSON — `<script type="application/
     json">`, Amazon's `<script type="a-state">` blocks, `__NEXT_DATA__`, `self.__next_f` flight
     chunks, and `window.<name> = …` assignments — so a hidden data blob is caught even if it never
     rode the wire.

Everything is written to an output dir (default `.amazon_capture/`, gitignored — it holds the
account's real order data / PII). Cookie/authorization header VALUES are redacted; their names/lengths
are kept so we can see what the session carries without writing raw session cookies to disk.

    python -m scripts.amazon_capture --label profile-bravo

Then inspect `.amazon_capture/summary.json`, `xhr_manifest.json`, and the per-page files to design
`scrapers/amazon_api.py` + `scrapers/amazon_mapping.py`.

NOTE: this spends a small Browser-Use CDP-browser fee and touches the live Amazon account.
"""

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# CdpBrowser talks to Browser-Use, which reads BROWSER_USE_API_KEY out of the ENVIRONMENT itself.
# Importing config.settings is what puts it there: the key lives in config.json now, and load_dotenv()
# alone does not look in that file. Without this the script dies at "No API key provided" against a
# perfectly valid setup. (config.settings loads .env too, so the override layer still works.)
import config.settings  # noqa: F401

log = logging.getLogger("amazon_capture")

ORDER_HISTORY_URLS = [
    "https://www.amazon.com/gp/css/order-history",
    "https://www.amazon.com/your-orders/orders",
]
ORDER_DETAILS_URLS = [
    "https://www.amazon.com/gp/css/order-details?orderID={}",
    "https://www.amazon.com/your-orders/order-details?orderID={}",
]
PRINT_INVOICE_URL = "https://www.amazon.com/gp/css/summary/print.html?orderID={}"
SIGNIN_MARKERS = ("/ap/signin", "/ap/mfa", "signin", "/ap/cvf")
ORDER_ID_RE = re.compile(r"\d{3}-\d{7}-\d{7}")


def _redact_headers(headers: dict) -> dict:
    """Keep header names/shape but never write raw session cookies / auth to disk."""
    out = {}
    for name, value in (headers or {}).items():
        lname = name.lower()
        if lname in ("cookie", "authorization", "x-amz-access-token", "set-cookie", "anti-csrftoken-a2z"):
            if lname == "cookie" and value:
                names = sorted({c.split("=", 1)[0].strip() for c in value.split(";") if c.strip()})
                out[name] = f"<redacted {len(value)} chars; cookies: {', '.join(names)}>"
            else:
                out[name] = f"<redacted {len(value)} chars>"
        else:
            out[name] = value
    return out


def _looks_logged_out(page) -> bool:
    url = (page.url or "").lower()
    if any(m in url for m in SIGNIN_MARKERS):
        return True
    try:
        return page.locator("#ap_email, input[name='email'], #ap_password").count() > 0
    except Exception:
        return False


# --- network capture ----------------------------------------------------------------------------
def _capture(page, manifest: list, out_dir: Path) -> None:
    """Record EVERY request (url/method/status/content-type) and save the full body of any JSON
    response. This is the decision gate — it reveals whether Amazon has a hidden order/tracking JSON
    endpoint (-> API path) or only serves HTML (-> in-page-fetch + parse path)."""
    saved = {"n": 0}

    def on_response(resp):
        try:
            req = resp.request
            ctype = ""
            try:
                ctype = (resp.headers or {}).get("content-type", "")
            except Exception:
                pass
            entry = {
                "method": req.method,
                "url": resp.url,
                "status": resp.status,
                "resource_type": req.resource_type,
                "content_type": ctype,
            }
            is_json = "application/json" in ctype.lower() or resp.url.lower().rstrip("/").endswith(".json")
            if is_json and req.resource_type in ("xhr", "fetch", "document", "other", "script"):
                try:
                    text = resp.text()
                except Exception:
                    text = ""
                if text.strip():
                    saved["n"] += 1
                    safe = "".join(c if c.isalnum() else "_" for c in resp.url.split("?")[0])[-70:]
                    (out_dir / f"json_{saved['n']:02d}_{safe}.json").write_text(text, encoding="utf-8")
                    entry["saved_json_body"] = f"json_{saved['n']:02d}_{safe}.json"
                    entry["request_headers"] = _redact_headers(req.all_headers())
                    log.info("saved JSON response: %s %s (%s)", req.method, resp.url, ctype)
            manifest.append(entry)
        except Exception:
            log.debug("capture handler error", exc_info=True)

    page.on("response", on_response)


# --- embedded-markup dump -----------------------------------------------------------------------
_EMBEDDED_JS = r"""
() => {
  const out = {a_state: [], json_scripts: [], next_data: null, next_f_chunks: 0, window_keys: []};
  try {
    for (const s of document.querySelectorAll('script[type="a-state"]')) {
      out.a_state.push({data_a_state: s.getAttribute('data-a-state'), text: s.textContent});
    }
  } catch (e) {}
  try {
    for (const s of document.querySelectorAll('script[type="application/json"]')) {
      out.json_scripts.push({id: s.id || null, text: s.textContent});
    }
  } catch (e) {}
  try {
    const nd = document.getElementById('__NEXT_DATA__');
    if (nd) out.next_data = JSON.parse(nd.textContent);
  } catch (e) {}
  try {
    out.next_f_chunks = (document.documentElement.innerHTML.match(/self\.__next_f\.push/g) || []).length;
  } catch (e) {}
  try {
    for (const k of Object.keys(window)) {
      const v = window[k];
      if (v && typeof v === 'object' && /order|track|state|data|shipment/i.test(k)) out.window_keys.push(k);
    }
  } catch (e) {}
  return out;
}
"""


def _dump_page(page, out_dir: Path, tag: str) -> None:
    """Save a page's url + HTML + screenshot + any embedded JSON. Never raises (recon must not lose
    what it already captured to a single failing page)."""
    try:
        (out_dir / f"{tag}_url.txt").write_text(page.url or "", encoding="utf-8")
    except Exception:
        pass
    html = ""
    try:
        html = page.content()
        (out_dir / f"{tag}.html").write_text(html, encoding="utf-8")
    except Exception:
        log.debug("content() dump failed for %s", tag, exc_info=True)
    try:
        page.screenshot(path=str(out_dir / f"{tag}.png"), full_page=True)
    except Exception:
        log.debug("screenshot failed for %s", tag, exc_info=True)
    try:
        embedded = page.evaluate(_EMBEDDED_JS)
        # Also scan the raw HTML for `window.<name> = {…/[…` assignments (a common Amazon data channel).
        win_assign = sorted(set(re.findall(r"window\.([A-Za-z_$][\w$]*)\s*=\s*[\{\[]", html)))
        embedded["window_assignments_in_html"] = win_assign
        (out_dir / f"{tag}_embedded.json").write_text(
            json.dumps(embedded, indent=2, default=str), encoding="utf-8"
        )
        log.info(
            "embedded [%s]: a_state=%d json_scripts=%d next_data=%s next_f_chunks=%d window_assign=%s",
            tag, len(embedded.get("a_state", [])), len(embedded.get("json_scripts", [])),
            embedded.get("next_data") is not None, embedded.get("next_f_chunks", 0), win_assign[:8],
        )
    except Exception:
        log.debug("embedded dump failed for %s", tag, exc_info=True)


_ORDER_DETAILS_ID_RE = re.compile(r"order-details\?orderID=(\d{3}-\d{7}-\d{7})", re.IGNORECASE)


def _order_ids_from_page(page) -> list[str]:
    """Distinct REAL Amazon order ids from the order-history page.

    IMPORTANT (learned in the first capture): a bare NNN-NNNNNNN-NNNNNNN regex over the whole page
    picks up JUNK ids from recommendation/ad widgets ("Buy again", "000-0000000-8675309", etc.) whose
    order-details pages 404. The account's genuine orders are exactly the ids that appear inside an
    `order-details?orderID=<id>` link — so discovery (STEP 1 of the real scraper) must scope to those.
    Falls back to the bare regex only if no such links are found (page shape changed)."""
    try:
        html = page.content()
    except Exception:
        return []
    seen: dict[str, None] = {}
    for oid in _ORDER_DETAILS_ID_RE.findall(html):
        seen.setdefault(oid, None)
    if seen:
        return list(seen.keys())
    for oid in ORDER_ID_RE.findall(html):
        seen.setdefault(oid, None)
    return list(seen.keys())


def _track_href(page) -> str | None:
    """The first 'Track package' href on an order-details page (the pt-page URL Amazon uses for the
    tracking number — the data we can't get off order-details)."""
    try:
        return page.evaluate(
            "() => { const a = document.querySelector(\"a[href*='ship-track'], a[href*='progress-tracker'],"
            " a[href*='/gp/css/shiptrack'], a[href*='shipmentId']\");"
            " return a ? a.href : null; }"
        )
    except Exception:
        return None


def run(label: str, out_dir: Path, force_order_ids: list[str] | None = None,
        max_orders: int = 3) -> int:
    from config.profiles import load_profiles
    from scrapers.cdp import CdpBrowser

    profile = next((p for p in load_profiles() if p.label == label), None)
    if profile is None:
        sys.exit(f"No profile '{label}' in profiles.json.")
    if not profile.profile_id:
        sys.exit(f"Profile '{label}' has no profile_id — run scripts.create_profile and log into Amazon first.")

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: list = []
    order_id = None

    # Wrap so ANY failure still writes what we captured + diagnostics (a crash mid-capture must not
    # lose the recon data we paid for).
    try:
        with CdpBrowser(profile) as page:
            _capture(page, manifest, out_dir)

            # --- order history (both URL shapes) ---------------------------------------------------
            for i, url in enumerate(ORDER_HISTORY_URLS):
                log.info("Loading order history: %s", url)
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(4000)
                if i == 0 and _looks_logged_out(page):
                    log.error("Amazon session looks LOGGED OUT (%s). Amazon has OTP/2FA, so this recon "
                              "tool does not auto-login. Re-run scripts.create_profile and log into "
                              "Amazon on profile '%s', then retry.", page.url, label)
                    _dump_page(page, out_dir, "logged_out")
                    return 1
                _dump_page(page, out_dir, f"order_history_{i}")

            order_ids = force_order_ids or _order_ids_from_page(page)
            log.info("Found %d distinct REAL order id(s): %s", len(order_ids), order_ids[:12])
            order_ids = order_ids[:max_orders]
            order_id = order_ids[0] if order_ids else None

            if not order_ids:
                log.warning("No order id discovered — cannot capture order-details / invoice / tracking.")
            for n, oid in enumerate(order_ids):
                log.info("=== Capturing order %d/%d: %s ===", n + 1, len(order_ids), oid)
                # --- order details (both URL shapes) ----------------------------------------------
                track_url = None
                for i, tmpl in enumerate(ORDER_DETAILS_URLS):
                    url = tmpl.format(oid)
                    log.info("Loading order details: %s", url)
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(4000)
                    _dump_page(page, out_dir, f"order_details_{oid}_{i}")
                    track_url = track_url or _track_href(page)

                # --- print invoice ----------------------------------------------------------------
                url = PRINT_INVOICE_URL.format(oid)
                log.info("Loading print invoice: %s", url)
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(4000)
                _dump_page(page, out_dir, f"print_invoice_{oid}")

                # --- package-tracking (pt) page ---------------------------------------------------
                if track_url:
                    log.info("Loading tracking page: %s", track_url)
                    try:
                        page.goto(track_url, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(4000)
                        _dump_page(page, out_dir, f"tracking_pt_{oid}")
                        (out_dir / f"tracking_url_{oid}.txt").write_text(track_url, encoding="utf-8")
                    except Exception:
                        log.warning("Failed to load tracking page %s", track_url, exc_info=True)
                else:
                    log.info("No 'Track package' href on order-details for %s (likely delivered/no "
                             "active shipment) — no tracking page captured.", oid)
    except Exception:
        log.exception("Capture run hit an error; writing out whatever was captured before it.")

    # --- write manifest + summary -----------------------------------------------------------------
    json_bodies = [e for e in manifest if e.get("saved_json_body")]
    (out_dir / "xhr_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    summary = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "profile": label,
        "order_id_opened": order_id,
        "total_requests": len(manifest),
        "json_responses_saved": len(json_bodies),
        "json_response_urls": [e["url"] for e in json_bodies],
        "xhr_fetch_urls": [e["url"] for e in manifest if e.get("resource_type") in ("xhr", "fetch")],
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"\nCaptured {len(manifest)} request(s); saved {len(json_bodies)} JSON response body(ies) -> {out_dir}")
    print("\nJSON responses (the API-path decision gate — if any hold order/shipment/cost data, "
          "build amazon_api.py around them):")
    for e in json_bodies[:40]:
        print(f"  {e['method']} {e['status']} {e['url']}  [{e.get('content_type','')}] -> {e['saved_json_body']}")
    if not json_bodies:
        print("  (none — expect the HTML-fetch-and-parse path; inspect the *_embedded.json for hidden blobs)")
    print(f"\nInspect {out_dir / 'summary.json'}, xhr_manifest.json, and the per-page *.html / "
          "*_embedded.json files next.")
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--label", default="profile-bravo", help="Profile that owns the Amazon login")
    parser.add_argument("--out", default=".amazon_capture", help="Output dir (gitignored)")
    parser.add_argument("--order-ids", default="",
                        help="Comma-separated order ids to open (overrides auto-discovery)")
    parser.add_argument("--max-orders", type=int, default=3,
                        help="How many discovered orders to capture details/invoice/tracking for")
    args = parser.parse_args()
    forced = [o.strip() for o in args.order_ids.split(",") if o.strip()] or None
    run(args.label, Path(args.out), forced, max_orders=args.max_orders)


if __name__ == "__main__":
    main()
