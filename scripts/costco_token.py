"""
Save the Costco **refresh token** that the GraphQL scraper uses to authenticate.

Costco's site keeps a long-lived refresh token in your browser after you log in; the scraper
exchanges it for short-lived access tokens on each run, so you only do this once (and again only if
the refresh token is ever revoked). No AI agent and no password are stored — just the token.

HOW TO GET THE TOKEN (from a browser session, ~1 minute):
  1. Log into https://www.costco.com in your browser.
  2. Open DevTools (F12) -> Application -> Local Storage -> https://signin.costco.com
  3. Find the key whose name contains "refreshtoken" and copy its "secret" value (the long string).

OR TRY TO GRAB IT AUTOMATICALLY from a profile already logged into Costco (no DevTools copy) — this
reconnects to that Browser-Use profile over CDP and (a) intercepts the token endpoint if the app
refreshes on load, else (b) scans local storage / IndexedDB:
    python -m scripts.costco_token --label profile-alpha --grab

  NOTE: --grab is best-effort. On the Browser-Use cloud profile Costco stores its MSAL cache
  ENCRYPTED (opaque localStorage blobs, no IndexedDB) and doesn't always refresh on a plain page
  load, so the grab can come up empty — in that case use the manual --token method above (proven).

THEN SAVE IT (run from the project root), keyed to the profile that owns this Costco membership:
    python -m scripts.costco_token --label profile-2 --token "<REFRESH_TOKEN>"

Set the warehouse number(s) getOnlineOrders is queried against (your home warehouse; default 847).
Find it on costco.com under your account, or leave the default and adjust if a live run finds nothing:
    python -m scripts.costco_token --label profile-2 --warehouses 847,123

Check what's stored (the token is shown masked):
    python -m scripts.costco_token --label profile-2 --show

The token is written to .costco/<label>.json, which is gitignored — treat that file like a password.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from scrapers.costco_api import DEFAULT_WAREHOUSES, TOKEN_DIR

# The CDP grab talks to Browser-Use, which reads BROWSER_USE_API_KEY from the environment; this
# script doesn't import config.settings, so load .env ourselves.
load_dotenv()

log = logging.getLogger("costco_token")


def _try_json(value):
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def _token_from_value(value) -> str | None:
    obj = _try_json(value)
    if isinstance(obj, dict) and obj.get("secret"):
        return obj["secret"]
    # Some builds store the raw token string directly (B2C refresh tokens are JWE-shaped: eyJ...).
    if isinstance(value, str) and value.startswith("eyJ") and value.count(".") >= 2:
        return value
    return None


def _extract_refresh_token(local_storage: dict) -> str | None:
    """Pull the refresh token out of an MSAL/B2C local-storage dump.

    MSAL stores each credential as a JSON blob whose `secret` field is the token; the refresh-token
    entry has `credentialType: "RefreshToken"`. Costco HASHES the cache key names, so we can't rely
    on the key containing 'refreshtoken' — match on the value's shape instead."""
    local_storage = local_storage or {}
    # Pass 1: obvious key name (works on un-hashed MSAL builds).
    for key, value in local_storage.items():
        if "refreshtoken" in key.lower():
            token = _token_from_value(value)
            if token:
                return token
    # Pass 2: MSAL cache entry identified by its value shape.
    for value in local_storage.values():
        obj = _try_json(value)
        if isinstance(obj, dict) and str(obj.get("credentialType", "")).lower() == "refreshtoken" and obj.get("secret"):
            return obj["secret"]
    return None


def _find_token_deep(obj) -> str | None:
    """Recursively search a decoded structure (dicts/lists/JSON-strings) for an MSAL RefreshToken
    cache entry and return its `secret`."""
    if isinstance(obj, dict):
        if str(obj.get("credentialType", "")).lower() == "refreshtoken" and obj.get("secret"):
            return obj["secret"]
        for value in obj.values():
            found = _find_token_deep(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_token_deep(value)
            if found:
                return found
    elif isinstance(obj, str):
        parsed = _try_json(obj)
        if isinstance(parsed, (dict, list)):
            return _find_token_deep(parsed)
    return None


# Dump every IndexedDB database/objectStore for the current origin. MSAL keeps large B2C tokens here
# when localStorage is unsuitable. Returns {dbName: {storeName: [records...]}}.
_READ_INDEXEDDB = """
async () => {
  const out = {};
  try {
    if (!indexedDB.databases) return {__error: 'indexedDB.databases() unsupported'};
    const dbs = await indexedDB.databases();
    for (const info of dbs) {
      const name = info.name; if (!name) continue;
      const db = await new Promise((res, rej) => {
        const r = indexedDB.open(name); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
      });
      out[name] = {};
      for (const store of Array.from(db.objectStoreNames)) {
        try {
          const rows = await new Promise((res, rej) => {
            const r = db.transaction(store, 'readonly').objectStore(store).getAll();
            r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
          });
          out[name][store] = rows;
        } catch (e) { out[name][store] = {__error: String(e)}; }
      }
      db.close();
    }
  } catch (e) { out.__error = String(e); }
  return out;
}
"""


def _log_storage_shape(origin: str, local_storage: dict) -> None:
    """Log the STRUCTURE of each entry (key name + JSON field names or length) to locate the token
    without ever printing a secret value."""
    for key, value in (local_storage or {}).items():
        obj = _try_json(value)
        if isinstance(obj, dict):
            shape = "json{" + ",".join(sorted(obj.keys())[:8]) + "}"
            ctype = obj.get("credentialType")
            if ctype:
                shape += f" credentialType={ctype}"
        else:
            shape = f"<{len(value)} chars>" if isinstance(value, str) else type(value).__name__
        log.info("    %s @ %s = %s", key, origin, shape)


def _grab_refresh_token(label: str) -> str | None:
    """Reconnect to the profile's Browser-Use browser over CDP and read the Costco refresh token
    from signin.costco.com local storage. Requires the profile to be logged into costco.com."""
    from config.profiles import load_profiles
    from scrapers.cdp import CdpBrowser

    profile = next((p for p in load_profiles() if p.label == label), None)
    if profile is None:
        sys.exit(f"No profile '{label}' in profiles.json.")
    if not profile.profile_id:
        sys.exit(f"Profile '{label}' has no profile_id — run scripts.create_profile and log into Costco first.")

    read_ls = (
        "() => { const o = {}; for (let i = 0; i < localStorage.length; i++)"
        " { const k = localStorage.key(i); o[k] = localStorage.getItem(k); } return o; }"
    )
    # A sentinel URL we fulfill ourselves with a blank page, so the document sits on the EXACT
    # signin.costco.com origin (no server redirect) and that origin's localStorage is readable.
    sentinel = "https://signin.costco.com/__ls_probe__"

    with CdpBrowser(profile) as page:
        page.context.route(sentinel, lambda route: route.fulfill(
            status=200, content_type="text/html", body="<html><body>probe</body></html>"))

        # STRATEGY 0 (most robust): the app silently refreshes its token on load of an authenticated
        # area — the B2C token endpoint's RESPONSE carries a fresh refresh_token. Capture it there,
        # since the stored copy is encrypted/opaque.
        captured: dict[str, str] = {}

        def _on_response(resp):
            if "oauth2/v2.0/token" not in resp.url or captured.get("rt"):
                return
            try:
                body = resp.json()
            except Exception:
                return
            if isinstance(body, dict) and body.get("refresh_token"):
                captured["rt"] = body["refresh_token"]
                log.info("Captured refresh_token from a token-endpoint response.")

        page.on("response", _on_response)
        for url in ("https://www.costco.com/OrderStatusCmd",
                    "https://www.costco.com/myaccount/orderdetails",
                    "https://www.costco.com/"):
            if captured.get("rt"):
                break
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(6000)  # let MSAL's silent token XHR fire
            except Exception:
                log.warning("Navigation to %s failed while forcing a token refresh.", url, exc_info=True)
        if captured.get("rt"):
            return captured["rt"]

        candidates: list[dict] = []
        # 1) Warm cookies on the main site.
        try:
            page.goto("https://www.costco.com/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
            candidates.append({"origin": "www.costco.com (live)", "ls": page.evaluate(read_ls)})
        except Exception:
            log.warning("Could not load costco.com to warm the session.", exc_info=True)
        # 2) Sit on the signin origin via the sentinel and read its localStorage + IndexedDB.
        try:
            page.goto(sentinel, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(500)
            candidates.append({"origin": "signin.costco.com (sentinel)", "ls": page.evaluate(read_ls)})
            try:
                idb = page.evaluate(_READ_INDEXEDDB)
                for db_name, stores in (idb or {}).items():
                    if not isinstance(stores, dict):
                        continue
                    for store_name, rows in stores.items():
                        count = len(rows) if isinstance(rows, list) else "?"
                        log.info("IndexedDB @ signin.costco.com: %s / %s -> %s record(s)", db_name, store_name, count)
                        token = _find_token_deep(rows)
                        if token:
                            log.info("Found RefreshToken in IndexedDB %s/%s", db_name, store_name)
                            return token
            except Exception:
                log.warning("IndexedDB read on the signin origin failed.", exc_info=True)
        except Exception:
            log.warning("Sentinel navigation to the signin origin failed.", exc_info=True)
        # 3) Whatever else the persisted storage state knows about.
        try:
            for origin in page.context.storage_state().get("origins", []):
                candidates.append({
                    "origin": f"{origin.get('origin')} (storage_state)",
                    "ls": {e["name"]: e["value"] for e in origin.get("localStorage", [])},
                })
        except Exception:
            log.warning("storage_state() enumeration failed.", exc_info=True)

        for c in candidates:
            token = _extract_refresh_token(c["ls"])
            if token:
                return token
        # Nothing matched — dump the structure of the signin origin so we can see where it lives.
        log.info("No refresh token matched. Storage structure (values redacted):")
        for c in candidates:
            if "signin.costco.com" in c["origin"]:
                _log_storage_shape(c["origin"], c["ls"])
    return None


def _token_path(label: str) -> Path:
    return Path(TOKEN_DIR) / f"{label}.json"


def _load(label: str) -> dict:
    path = _token_path(label)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save(label: str, data: dict) -> Path:
    path = _token_path(label)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _mask(token: str) -> str:
    return f"{token[:6]}…{token[-4:]} ({len(token)} chars)" if token else "(none)"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--label", required=True, help="Profile label that owns this Costco membership")
    parser.add_argument("--token", help="The refresh token 'secret' copied from browser local storage")
    parser.add_argument(
        "--grab",
        action="store_true",
        help="Grab the refresh token automatically from the profile's logged-in Costco session (CDP)",
    )
    parser.add_argument(
        "--warehouses",
        help="Comma-separated warehouse number(s) to query, e.g. '847' or '847,123' (default 847)",
    )
    parser.add_argument("--show", action="store_true", help="Show what's stored (token masked) and exit")
    args = parser.parse_args()

    data = _load(args.label)

    if args.show:
        print(f"Costco token store for '{args.label}': {_token_path(args.label)}")
        print(f"  refresh_token: {_mask(data.get('refresh_token', ''))}")
        print(f"  id_token cached: {'yes' if data.get('id_token') else 'no'}")
        print(f"  warehouse_numbers: {data.get('warehouse_numbers') or DEFAULT_WAREHOUSES}")
        return

    if not args.token and not args.warehouses and not args.grab:
        sys.exit("Nothing to do. Pass --grab (auto) or --token (manual) to save a refresh token, "
                 "--warehouses to set warehouses, or --show to inspect. See --help.")

    if args.grab:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
        print(f"Grabbing Costco refresh token from profile '{args.label}' over CDP…")
        token = _grab_refresh_token(args.label)
        if not token:
            sys.exit(
                "Could not find a Costco refresh token in that profile's browser storage. Make sure "
                "the profile is logged into costco.com, or grab it manually (see --help)."
            )
        data["refresh_token"] = token
        data.pop("id_token", None)
        print(f"Grabbed refresh token: {_mask(token)}")

    if args.token:
        data["refresh_token"] = args.token.strip()
        data.pop("id_token", None)  # force a fresh exchange on the next run

    if args.warehouses:
        data["warehouse_numbers"] = [w.strip() for w in args.warehouses.split(",") if w.strip()]

    path = _save(args.label, data)
    print(f"Saved Costco credentials for '{args.label}' to {path}")
    print(f"  refresh_token: {_mask(data.get('refresh_token', ''))}")
    print(f"  warehouse_numbers: {data.get('warehouse_numbers') or DEFAULT_WAREHOUSES}")
    print("\nTest it:  python main.py costco")


if __name__ == "__main__":
    main()
