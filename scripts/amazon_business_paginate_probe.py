"""
RECON tool (one-off): learn how Amazon Business paginates its order history.

The main capture (scripts/amazon_capture.py) proved Amazon Business is Path B (server-rendered HTML,
no order JSON) and that the order-details / pt-tracking DOM matches consumer Amazon. The ONE thing it
could not settle from a single page load is PAGINATION: business order-history does NOT use consumer's
`?timeFilter=&startIndex=` URL params (startIndex never appears). Instead it is a client-side SPA that
paginates via hash routes (`#pagination/2/`, `#pagination/next/`) driving a **POST**
`/ab/your-orders/orderHistory` fragment that returns HTML.

To build a deterministic (agent-free) paginator we need that POST's request shape. This tool:
  1. Reconnects CdpBrowser on the business profile and loads `/your-orders/orders`.
  2. Hooks requests/responses and records EVERY POST to `/ab/your-orders/orderHistory` — full request
     body (page index / filter params live here), request headers (redacted), and the response HTML.
  3. Clicks the pagination "next"/"2" control to force page 2 to load, capturing that POST.
  4. Then tries an IN-PAGE `fetch('/ab/your-orders/orderHistory', {method:'POST', body:<observed>,
     credentials:'include'})` to confirm the fragment can be fetched directly (the clean paginator) and
     that the returned HTML carries the next page's `order-details?orderID=` links + "Order placed"
     dates.

Output -> gitignored `.amazon_business_capture/` (real PII). Cookie/auth header VALUES redacted.

    .venv\\Scripts\\python -m scripts.amazon_business_paginate_probe --label profile-alpha

NOTE: spends a small Browser-Use CDP fee and touches the live Amazon Business account. It never logs
in (Amazon has OTP/2FA); if the session lapsed it bails.
"""

import argparse
import json
import logging
import re
import sys
from pathlib import Path

# CdpBrowser talks to Browser-Use, which reads BROWSER_USE_API_KEY out of the ENVIRONMENT itself.
# Importing config.settings is what puts it there: the key lives in config.json now, and load_dotenv()
# alone does not look in that file. Without this the script dies at "No API key provided" against a
# perfectly valid setup. (config.settings loads .env too, so the override layer still works.)
import config.settings  # noqa: F401

log = logging.getLogger("ab_paginate_probe")

ORDER_HISTORY_URL = "https://www.amazon.com/your-orders/orders"
FRAGMENT_PATH = "/ab/your-orders/orderHistory"
SIGNIN_MARKERS = ("/ap/signin", "/ap/mfa", "signin", "/ap/cvf")
_ORDER_DETAILS_ID_RE = re.compile(r"order-details\?orderID=(\d{3}-\d{7}-\d{7})", re.IGNORECASE)


def _redact_headers(headers: dict) -> dict:
    out = {}
    for name, value in (headers or {}).items():
        lname = name.lower()
        if lname in ("cookie", "authorization", "x-amz-access-token", "set-cookie", "anti-csrftoken-a2z"):
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


def _order_ids_and_dates(html: str) -> list[dict]:
    """Best-effort {order_id, order_placed_date} pairs from a fragment/page HTML, for eyeballing that a
    fetched page-2 fragment really carries the next orders."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    out = []
    seen = set()
    for a in soup.select("a[href*='order-details?orderID=']"):
        m = _ORDER_DETAILS_ID_RE.search(a.get("href", ""))
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        # walk up to the enclosing card and read its "Order placed <date>"
        date = ""
        node = a
        for _ in range(9):
            node = node.parent
            if node is None:
                break
            t = " ".join(node.get_text(" ", strip=True).split())
            dm = re.search(r"Order placed\s+([A-Z][a-z]+ \d{1,2}, \d{4})", t)
            if dm:
                date = dm.group(1)
                break
        out.append({"order_id": m.group(1), "order_placed": date})
    return out


def run(label: str, out_dir: Path) -> int:
    from config.profiles import load_profiles
    from scrapers.cdp import CdpBrowser

    profile = next((p for p in load_profiles() if p.label == label), None)
    if profile is None:
        sys.exit(f"No profile '{label}' in profiles.json.")
    if not profile.profile_id:
        sys.exit(f"Profile '{label}' has no profile_id — log into Amazon Business first.")

    out_dir.mkdir(parents=True, exist_ok=True)
    posts: list[dict] = []  # every POST to the fragment endpoint

    with CdpBrowser(profile) as page:
        def on_request(req):
            try:
                if FRAGMENT_PATH in req.url and req.method == "POST":
                    body = None
                    try:
                        body = req.post_data
                    except Exception:
                        body = "<unreadable>"
                    posts.append({
                        "when": "observed",
                        "url": req.url,
                        "method": req.method,
                        "post_data": body,
                        "headers": _redact_headers(req.all_headers()),
                    })
                    log.info("captured POST %s (body %d chars)", req.url, len(body or ""))
            except Exception:
                log.debug("request hook error", exc_info=True)

        def on_response(resp):
            try:
                if FRAGMENT_PATH in resp.url and resp.request.method == "POST":
                    text = ""
                    try:
                        text = resp.text()
                    except Exception:
                        pass
                    n = len([p for p in posts if p.get("response_saved")]) + 1
                    fn = f"paginate_fragment_{n:02d}.html"
                    (out_dir / fn).write_text(text, encoding="utf-8")
                    ids = _order_ids_and_dates(text)
                    posts.append({
                        "when": "response",
                        "url": resp.url,
                        "status": resp.status,
                        "response_saved": fn,
                        "order_ids_in_fragment": ids,
                    })
                    log.info("saved fragment response %s (%d order ids)", fn, len(ids))
            except Exception:
                log.debug("response hook error", exc_info=True)

        page.on("request", on_request)
        page.on("response", on_response)

        log.info("Loading %s", ORDER_HISTORY_URL)
        page.goto(ORDER_HISTORY_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        if _looks_logged_out(page):
            log.error("Amazon Business session looks LOGGED OUT (%s). Log in on '%s' and retry.",
                      page.url, label)
            (out_dir / "paginate_logged_out.html").write_text(page.content(), encoding="utf-8")
            return 1

        page1_ids = _order_ids_and_dates(page.content())
        (out_dir / "paginate_page1.html").write_text(page.content(), encoding="utf-8")
        log.info("Page 1 has %d order ids: %s", len(page1_ids), [d["order_id"] for d in page1_ids])

        # --- click to page 2 -------------------------------------------------------------------
        clicked = False
        for sel in ("li.a-last a", "a[href='#pagination/next/']", "a[href='#pagination/2/']",
                    ".a-pagination a[href*='pagination/2']"):
            try:
                loc = page.locator(sel)
                if loc.count() > 0:
                    log.info("Clicking pagination control: %s", sel)
                    loc.first.click(timeout=10000)
                    clicked = True
                    break
            except Exception:
                log.debug("click failed for %s", sel, exc_info=True)
        if clicked:
            page.wait_for_timeout(5000)
            page2_ids = _order_ids_and_dates(page.content())
            (out_dir / "paginate_page2_afterclick.html").write_text(page.content(), encoding="utf-8")
            log.info("After click, page shows %d order ids: %s", len(page2_ids),
                     [d["order_id"] for d in page2_ids])
            log.info("page.url after click: %s", page.url)
        else:
            log.warning("Could not find a pagination control to click.")

        # --- try an in-page fetch replay of the observed POST body -----------------------------
        fetch_result = None
        observed = next((p for p in posts if p.get("when") == "observed" and p.get("post_data")), None)
        if observed and isinstance(observed["post_data"], str):
            try:
                fetch_result = page.evaluate(
                    """async (body) => {
                        const r = await fetch('/ab/your-orders/orderHistory', {
                            method: 'POST', credentials: 'include',
                            headers: {'content-type': 'application/x-www-form-urlencoded'},
                            body
                        });
                        const t = await r.text();
                        return {status: r.status, length: t.length, sample: t.slice(0, 2000)};
                    }""",
                    observed["post_data"],
                )
                (out_dir / "paginate_fetch_replay.json").write_text(
                    json.dumps(fetch_result, indent=2), encoding="utf-8")
                log.info("in-page fetch replay: status=%s length=%s",
                         fetch_result.get("status"), fetch_result.get("length"))
            except Exception:
                log.warning("in-page fetch replay failed", exc_info=True)

    (out_dir / "paginate_summary.json").write_text(
        json.dumps({
            "profile": label,
            "page1_order_ids": [d["order_id"] for d in page1_ids],
            "posts": posts,
            "fetch_replay": fetch_result,
        }, indent=2), encoding="utf-8")

    print(f"\nCaptured {len([p for p in posts if p.get('when')=='observed'])} POST(s) to {FRAGMENT_PATH}.")
    print(f"Inspect {out_dir}/paginate_summary.json, paginate_fragment_*.html, paginate_fetch_replay.json")
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", default="profile-alpha", help="Profile logged into Amazon Business")
    parser.add_argument("--out", default=".amazon_business_capture", help="Output dir (gitignored)")
    args = parser.parse_args()
    sys.exit(run(args.label, Path(args.out)))


if __name__ == "__main__":
    main()
