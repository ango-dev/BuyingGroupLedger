"""
RECON tool: capture Best Buy's private GraphQL traffic from a logged-in session.

This is a one-off developer tool used to BUILD the deterministic Best Buy GraphQL path (the Costco-
style scraper that replaces the paid Browser-Use agent — see the design notes). It does NOT run in
production. It:

  1. Reconnects to a profile's Browser-Use cloud browser over CDP (scrapers.cdp.CdpBrowser).
  2. If the Best Buy web session has lapsed, logs back in DETERMINISTICALLY (no agent) using the
     profile's `auth.bestbuy` password creds: #fld-e (email) -> Continue -> #password-radio
     ("Use password") -> password field -> submit. This is the exact flow confirmed live in 3b.
  3. Hooks the network and records every POST to `https://www.bestbuy.com/gateway/graphql` while it
     loads the purchase-history page and one order-details page — capturing the operation name,
     request variables, the query text OR the Apollo persisted-query (APQ) sha256 hash, the request
     headers Best Buy requires, and the full JSON response.
  4. Also dumps the in-page Apollo cache / __NEXT_DATA__ if present (another source of query text).

Everything is written to an output dir (default `.bestbuy_capture/`, gitignored — it contains the
account's real order data). Cookie header VALUES are redacted; their names/lengths are kept so we can
see what the session carries without writing raw session cookies to disk.

    python -m scripts.bestbuy_capture --label profile-alpha

Then inspect `.bestbuy_capture/summary.json` and the per-operation files to design
`scrapers/bestbuy_graphql.py` + `scrapers/bestbuy_mapping.py`.

NOTE: this spends a small Browser-Use CDP-browser fee and touches the live Best Buy account. The
session dies ~20 min after login, so capture promptly.
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# CdpBrowser talks to Browser-Use, which reads BROWSER_USE_API_KEY out of the ENVIRONMENT itself.
# Importing config.settings is what puts it there: the key lives in config.json now, and load_dotenv()
# alone does not look in that file. Without this the script dies at "No API key provided" against a
# perfectly valid setup. (config.settings loads .env too, so the override layer still works.)
import config.settings  # noqa: F401

log = logging.getLogger("bestbuy_capture")

GRAPHQL_PATH = "/gateway/graphql"
PURCHASE_HISTORY_URL = "https://www.bestbuy.com/purchasehistory/purchases"
ORDER_DETAILS_URL = "https://www.bestbuy.com/profile/ss/orders/order-details/{}/view"
SIGNIN_MARKERS = ("identity/signin", "/login", "signin/options")


def _redact_headers(headers: dict) -> dict:
    """Keep header names/shape but never write raw session cookies / auth to disk."""
    out = {}
    for name, value in (headers or {}).items():
        lname = name.lower()
        if lname in ("cookie", "authorization", "x-csrf-token", "set-cookie"):
            # Record the cookie NAMES (not values) so we can see what the session carries.
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
        return page.locator("#fld-e").count() > 0
    except Exception:
        return False


def _dismiss_survey(page) -> None:
    """Best Buy intermittently renders a #survey_window modal that intercepts clicks (noted in 3b)."""
    try:
        page.evaluate(
            "() => { const s = document.getElementById('survey_window'); if (s) s.remove(); }"
        )
    except Exception:
        pass


def _dump_page_diagnostics(page, out_dir: Path, tag: str) -> None:
    """Save URL + HTML + screenshot so a failed/unknown page can be inspected offline. Screenshots
    render fine even though CdpBrowser blocks images. Never raises."""
    try:
        (out_dir / f"{tag}_url.txt").write_text(page.url or "", encoding="utf-8")
    except Exception:
        pass
    try:
        (out_dir / f"{tag}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        log.debug("content() dump failed for %s", tag, exc_info=True)
    try:
        page.screenshot(path=str(out_dir / f"{tag}.png"), full_page=True)
    except Exception:
        log.debug("screenshot failed for %s", tag, exc_info=True)
    # Also log which known sign-in selectors are present, to correct the flow quickly.
    try:
        probe = page.evaluate(
            "() => ({ url: location.href,"
            " fld_e: !!document.querySelector('#fld-e'),"
            " any_email: !!document.querySelector(\"input[type='email'], input[name*='email' i]\"),"
            " password_radio: !!document.querySelector('#password-radio'),"
            " any_password: !!document.querySelector(\"input[type='password']\"),"
            " signin_link: !!document.querySelector(\"a[href*='signin'], button\"),"
            " iframes: document.querySelectorAll('iframe').length })"
        )
        log.info("page diagnostics [%s]: %s", tag, probe)
    except Exception:
        log.debug("selector probe failed for %s", tag, exc_info=True)


def _deterministic_login(page, auth, out_dir: Path) -> bool:
    """Drive Best Buy's 3-screen password login without the agent. Mirrors the proven flow encoded
    in scrapers/bestbuy.py:_password_signin_block. NON-THROWING: this is recon, so on any failure it
    dumps diagnostics and returns False rather than crashing (which would lose everything)."""
    log.info("Session looks logged out; attempting deterministic password login for %s.", auth.username)
    _dismiss_survey(page)
    _dump_page_diagnostics(page, out_dir, "signin_landing")

    # Screen 1: email -> Continue. TWO variants (confirmed live):
    #   * FRESH login: an editable email field #fld-e is shown -> type the email.
    #   * REMEMBERED user ("Keep me signed in" persisted): the email is shown as PREFILLED STATIC
    #     TEXT (div.prefilled-value + a "Not You?" link), with NO #fld-e — just click Continue.
    # Either way the primary action is the blue submit `button.cia-form__controls__submit`
    # ("Sign In: Continue") — NOT the Passkey/Apple/Google buttons also on this page.
    try:
        page.wait_for_selector("#fld-e", state="visible", timeout=8000)
        page.fill("#fld-e", auth.username)
        log.info("Typed email into #fld-e (fresh login).")
    except Exception:
        if page.locator(".prefilled-value, .cia-signin__username").count() == 0:
            log.error("Neither an editable #fld-e nor a prefilled email is present — see "
                      "signin_landing.* in %s. The sign-in flow may have changed.", out_dir)
            return False
        log.info("Email is prefilled (remembered user); skipping email entry.")

    clicked = False
    for attempt in (
        lambda: page.locator("button.cia-form__controls__submit").first.click(timeout=8000),
        lambda: page.get_by_role("button", name="Continue", exact=False).first.click(timeout=8000),
    ):
        try:
            attempt()
            clicked = True
            break
        except Exception:
            continue
    if not clicked:
        log.error("Could not click the Continue submit on the sign-in page.")
        _dump_page_diagnostics(page, out_dir, "signin_no_continue")
        return False
    log.info("Clicked Continue.")

    # Screen 2: method chooser -> "Use password" radio (id password-radio), below the fold.
    try:
        page.wait_for_selector("#password-radio", timeout=30000)
        _dismiss_survey(page)
        page.locator("#password-radio").scroll_into_view_if_needed(timeout=8000)
        page.locator("#password-radio").click(timeout=8000)
        log.info("Selected 'Use password' (#password-radio).")
    except Exception:
        log.warning("Did not see #password-radio — the account may go straight to a password field.",
                    exc_info=True)
        _dump_page_diagnostics(page, out_dir, "signin_method_chooser")

    # Screen 3: password field -> submit.
    try:
        page.wait_for_selector("input[type=password]", state="visible", timeout=30000)
        page.fill("input[type=password]", auth.password)
        log.info("Typed password.")
        for attempt in (
            lambda: page.get_by_role("button", name="Sign In", exact=False).first.click(timeout=8000),
            lambda: page.get_by_role("button", name="Continue", exact=False).first.click(timeout=8000),
            lambda: page.press("input[type=password]", "Enter"),
        ):
            try:
                attempt()
                break
            except Exception:
                continue
    except Exception:
        log.warning("Never saw a password field; login may have failed.", exc_info=True)
        _dump_page_diagnostics(page, out_dir, "signin_password_step")
        return False

    # Wait to land back on an authenticated Best Buy page.
    try:
        page.wait_for_url(lambda u: "bestbuy.com" in u and not any(m in u for m in SIGNIN_MARKERS),
                          timeout=45000)
    except Exception:
        log.warning("Post-login navigation wait timed out (url=%s).", page.url)
        _dump_page_diagnostics(page, out_dir, "signin_post_submit")
    return not _looks_logged_out(page)


# Keywords that flag an XHR/fetch worth saving in full (the order-details data endpoint we're hunting
# — tracking#, per-item cost, card — is NOT /gateway/graphql, so we cast a wide net).
_INTERESTING_URL = ("order", "tcfb", "model.json", "tracking", "payment", "fulfillment",
                    "graphql", "/api/", "shipment")


def _capture(page, captured: list, manifest: list | None = None, out_dir: Path | None = None) -> None:
    """Record every /gateway/graphql POST (request + response), AND a manifest of all xhr/fetch
    traffic — saving in full any XHR whose URL looks order/tracking/payment-related, so we can find
    where the order-details page gets its data (it is not GraphQL)."""

    def on_response(resp):
        try:
            req = resp.request
            rtype = req.resource_type
            # --- manifest: every xhr/fetch, plus full dump of interesting ones -----------------
            if manifest is not None and rtype in ("xhr", "fetch"):
                url_l = resp.url.lower()
                interesting = any(k in url_l for k in _INTERESTING_URL) and GRAPHQL_PATH not in url_l
                entry = {"method": req.method, "url": resp.url, "status": resp.status,
                         "resource_type": rtype, "interesting": interesting}
                manifest.append(entry)
                if interesting and out_dir is not None:
                    try:
                        text = resp.text()
                    except Exception:
                        text = ""
                    # Only keep bodies that actually mention tracking/price/card, to cut noise.
                    if any(kw in text.lower() for kw in ("tracking", "carrier", "cardtype",
                                                          "lastfour", "unitprice", "ordertotal")):
                        idx = len([e for e in manifest if e.get("interesting")])
                        safe = "".join(c if c.isalnum() else "_" for c in resp.url.split("?")[0])[-80:]
                        (out_dir / f"xhr_{idx:02d}_{safe}.json").write_text(text, encoding="utf-8")
                        entry["saved_body_matched_keywords"] = True
                        log.info("saved interesting XHR: %s %s", req.method, resp.url)

            if GRAPHQL_PATH not in resp.url or req.method != "POST":
                return
            body = None
            try:
                body = json.loads(req.post_data or "null")
            except Exception:
                body = {"__raw_post_data__": req.post_data}
            # A GraphQL POST may be a single op or a batch (list).
            ops = body if isinstance(body, list) else [body]
            op_names = [o.get("operationName") for o in ops if isinstance(o, dict)]
            has_query = any(isinstance(o, dict) and o.get("query") for o in ops)
            has_apq = any(
                isinstance(o, dict) and (o.get("extensions") or {}).get("persistedQuery")
                for o in ops
            )
            resp_json = None
            try:
                resp_json = resp.json()
            except Exception:
                resp_json = {"__non_json_response__": True, "status": resp.status}
            captured.append(
                {
                    "url": resp.url,
                    "status": resp.status,
                    "operation_names": op_names,
                    "has_inline_query": has_query,
                    "uses_apq_hash": has_apq,
                    "request_headers": _redact_headers(req.all_headers()),
                    "request_body": body,
                    "response_body": resp_json,
                }
            )
            log.info("captured GraphQL op(s)=%s  inline_query=%s  apq=%s  status=%s",
                     op_names, has_query, has_apq, resp.status)
        except Exception:
            log.debug("capture handler error", exc_info=True)

    page.on("response", on_response)


def _inpage_fetch(page, paths: list[str]) -> dict:
    """Run fetch() INSIDE the logged-in page for each path, riding the session cookie + Akamai (this
    is the production data-access method for the deterministic Best Buy path). Returns
    {path: {status, content_type, body}}."""
    js = """
    async (paths) => {
      const out = {};
      for (const p of paths) {
        try {
          const r = await fetch(p, {credentials: 'include', headers: {'accept': 'application/json'}});
          out[p] = {status: r.status, content_type: r.headers.get('content-type'), body: await r.text()};
        } catch (e) { out[p] = {status: 'ERR', content_type: null, body: String(e)}; }
      }
      return out;
    }
    """
    try:
        return page.evaluate(js, paths)
    except Exception:
        log.warning("in-page fetch failed", exc_info=True)
        return {}


def _order_ids_from_page(page) -> list[str]:
    """Distinct bare Best Buy order ids (BBY0N-<digits>, group suffix stripped) present anywhere in
    the page — the orders are embedded in the Next.js flight data, so a content regex finds them."""
    import re
    try:
        html = page.content()
    except Exception:
        return []
    ids = re.findall(r"BBY0\d-\d+", html)  # BBY01 usually, BBY03 exists live (2026-09-04)
    seen: dict[str, None] = {}
    for i in ids:
        seen.setdefault(i, None)
    return list(seen.keys())


def _first_order_id(captured: list, page) -> str | None:
    """Find one order id to open its details page. Prefer the purchase-history GraphQL response;
    fall back to scraping an order link out of the DOM."""
    for entry in captured:
        resp = entry.get("response_body")
        found = _search_order_id(resp)
        if found:
            return found
    # DOM fallback: any link that looks like an order-details URL.
    try:
        href = page.evaluate(
            "() => { const a = document.querySelector(\"a[href*='order-details/']\");"
            " return a ? a.getAttribute('href') : null; }"
        )
        if href and "order-details/" in href:
            return href.split("order-details/")[1].split("/")[0]
    except Exception:
        pass
    return None


def _search_order_id(obj) -> str | None:
    """Walk a JSON structure looking for a BBY0N-... order id or an obvious order-number field."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, str) and re.match(r"BBY0\d-", value):
                # Strip any trailing -group_N so we get the bare order id.
                return value.split("-group")[0]
            if key.lower() in ("ordernumber", "orderid", "order_number", "customerorderid") and value:
                return str(value)
            found = _search_order_id(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _search_order_id(value)
            if found:
                return found
    return None


def _dump_apollo_state(page, out_dir: Path) -> None:
    """The app may embed its Apollo cache / Next.js data, another source of query text + structure."""
    script = """
    () => {
      const out = {};
      try { if (window.__APOLLO_STATE__) out.__APOLLO_STATE__ = window.__APOLLO_STATE__; } catch (e) {}
      try {
        const nd = document.getElementById('__NEXT_DATA__');
        if (nd) out.__NEXT_DATA__ = JSON.parse(nd.textContent);
      } catch (e) {}
      return out;
    }
    """
    try:
        state = page.evaluate(script)
        if state:
            (out_dir / "apollo_state.json").write_text(
                json.dumps(state, indent=2, default=str), encoding="utf-8"
            )
            log.info("Wrote apollo_state.json (keys: %s).", list(state.keys()))
    except Exception:
        log.debug("Apollo/Next data dump failed.", exc_info=True)


def run(label: str, out_dir: Path, force_order_ids: list[str] | None = None) -> int:
    from config.profiles import load_profiles
    from scrapers.cdp import CdpBrowser

    profile = next((p for p in load_profiles() if p.label == label), None)
    if profile is None:
        sys.exit(f"No profile '{label}' in profiles.json.")
    if not profile.profile_id:
        sys.exit(f"Profile '{label}' has no profile_id — run scripts.create_profile and log into Best Buy first.")
    auth = profile.auth.get("bestbuy")

    out_dir.mkdir(parents=True, exist_ok=True)
    captured: list = []
    manifest: list = []
    order_id = None

    # Wrap the browser interaction so ANY failure still writes out what we captured + diagnostics
    # (a crash mid-capture must not lose the recon data we paid for).
    try:
        with CdpBrowser(profile) as page:
            _capture(page, captured, manifest, out_dir)

            log.info("Loading purchase history…")
            page.goto(PURCHASE_HISTORY_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)

            if _looks_logged_out(page):
                if auth is None or auth.method != "password" or not auth.username:
                    log.error("Profile '%s' looks logged out but has no bestbuy password auth to log "
                              "in with. Re-run scripts.create_profile to log into Best Buy.", label)
                    _dump_page_diagnostics(page, out_dir, "logged_out_no_auth")
                    return 1
                ok = _deterministic_login(page, auth, out_dir)
                log.info("Login attempt returned success=%s.", ok)
                # Reload purchase history now that we're (hopefully) authenticated.
                page.goto(PURCHASE_HISTORY_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(4000)

            if _looks_logged_out(page):
                log.error("Still logged out after the login attempt; see the signin_* diagnostics.")
                _dump_page_diagnostics(page, out_dir, "still_logged_out")

            # Lazy-load the purchase list so the history GraphQL op(s) fire (and pagination, if any).
            for _ in range(6):
                try:
                    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    pass
                page.wait_for_timeout(1500)

            _dump_page_diagnostics(page, out_dir, "purchase_history")
            _dump_apollo_state(page, out_dir)

            order_ids = force_order_ids or _order_ids_from_page(page)
            log.info("Found %d distinct order id(s) on the page: %s", len(order_ids), order_ids[:8])
            order_id = order_ids[0] if order_ids else None

            # PROVE THE PRODUCTION DATA PATH: in-page fetch (rides the logged-in cookie + Akamai, no
            # navigation) of the ss-api. Probe candidate LIST endpoints for discovery, then fetch each
            # order's detail JSON — these become the offline mapping fixtures (varied states).
            list_candidates = [
                "/profile/ss/api/v1/orders",
                "/profile/ss/api/v1/orders?page=1&pageSize=20",
                "/profile/ss/api/v1/orders/history",
                "/profile/ss/api/v1/purchase-history",
            ]
            probes = _inpage_fetch(page, list_candidates)
            (out_dir / "fetch_list_probes.json").write_text(
                json.dumps({u: {k: v for k, v in r.items() if k != "body"} | {"body_len": len(r.get("body", ""))}
                            for u, r in probes.items()}, indent=2), encoding="utf-8")
            for u, r in probes.items():
                log.info("list-probe %s -> status=%s ct=%s len=%s", u, r.get("status"),
                         r.get("content_type"), len(r.get("body", "")))
                if str(r.get("status", "")).startswith("2") and r.get("body"):
                    safe = "".join(c if c.isalnum() else "_" for c in u)[-60:]
                    (out_dir / f"list_{safe}.json").write_text(r["body"], encoding="utf-8")

            detail_urls = [f"/profile/ss/api/v1/orders/{oid}" for oid in order_ids]
            details = _inpage_fetch(page, detail_urls)
            for u, r in details.items():
                oid = u.rsplit("/", 1)[-1]
                log.info("detail %s -> status=%s len=%s", oid, r.get("status"), len(r.get("body", "")))
                if r.get("body"):
                    (out_dir / f"detail_{oid}.json").write_text(r["body"], encoding="utf-8")
    except Exception:
        log.exception("Capture run hit an error; writing out whatever was captured before it.")

    # --- write everything out ---------------------------------------------------------------------
    for i, entry in enumerate(captured):
        name = "-".join(n for n in entry["operation_names"] if n) or "unknown"
        (out_dir / f"op_{i:02d}_{name}.json").write_text(
            json.dumps(entry, indent=2, default=str), encoding="utf-8"
        )

    summary = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "profile": label,
        "order_id_opened": order_id,
        "graphql_calls": len(captured),
        "operations": [
            {
                "index": i,
                "operation_names": e["operation_names"],
                "uses_apq_hash": e["uses_apq_hash"],
                "has_inline_query": e["has_inline_query"],
                "status": e["status"],
                "response_top_level_keys": list(e["response_body"].keys())
                if isinstance(e["response_body"], dict) else None,
            }
            for i, e in enumerate(captured)
        ],
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (out_dir / "xhr_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    interesting = [e for e in manifest if e.get("interesting")]
    print(f"\nXHR/fetch manifest: {len(manifest)} total, {len(interesting)} interesting:")
    for e in interesting[:30]:
        print(f"  {e['method']} {e['status']} {e['url']}"
              + ("  [BODY SAVED]" if e.get("saved_body_matched_keywords") else ""))

    print(f"\nCaptured {len(captured)} GraphQL call(s) -> {out_dir}")
    for op in summary["operations"]:
        print(f"  [{op['index']:02d}] {op['operation_names']}  apq={op['uses_apq_hash']} "
              f"inline_query={op['has_inline_query']} status={op['status']}")
    print(f"\nInspect {out_dir / 'summary.json'} and the op_*.json files next.")
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--label", default="profile-alpha", help="Profile that owns the Best Buy login")
    parser.add_argument("--out", default=".bestbuy_capture", help="Output dir (gitignored)")
    parser.add_argument("--order-ids", default="",
                        help="Comma-separated order ids to open (overrides auto-discovery)")
    args = parser.parse_args()
    forced = [o.strip() for o in args.order_ids.split(",") if o.strip()] or None
    run(args.label, Path(args.out), forced)


if __name__ == "__main__":
    main()
