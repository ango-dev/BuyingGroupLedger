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

  NOTE: --grab works, but it is OPPORTUNISTIC. Costco's MSAL cache is stored ENCRYPTED (opaque
  localStorage blobs, no IndexedDB), so (b) is a dead end and everything rides on (a) — and (a) needs
  the app to actually perform a refresh while we watch. If the profile's token is still valid, MSAL
  correctly does nothing and the grab comes up empty. So it succeeds precisely when the cached token
  has EXPIRED, which is when you need a new one anyway. An empty grab means "the session is still
  warm", not "this is broken" — retry later, or use the manual --token method above.

THEN SAVE IT (run from the project root), keyed to the profile that owns this Costco membership:
    python -m scripts.costco_token --label profile-2 --token "<REFRESH_TOKEN>"

Set the warehouse number(s) getOnlineOrders is queried against (your home warehouse; default 847).
Find it on costco.com under your account, or leave the default and adjust if a live run finds nothing:
    python -m scripts.costco_token --label profile-2 --warehouses 847,123

Check what's stored (the token is shown masked):
    python -m scripts.costco_token --label profile-2 --show

The token is written to .state.json under `costco.<label>`, which is gitignored — treat it like a password.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

import diagnostics

from config.loader import STATE_FILE
from scrapers.costco_api import DEFAULT_WAREHOUSES, load_costco_auth, save_costco_auth

# The CDP grab talks to Browser-Use, which reads BROWSER_USE_API_KEY out of the ENVIRONMENT itself.
# scrapers.costco_api pulls in config.settings, whose import puts the config.json value there (and
# loads .env). Kept explicit so a future import tidy-up cannot quietly break the grab.
import config.settings  # noqa: E402,F401

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
        elif isinstance(value, str):
            # Describe the VALUE without ever printing it. A bare length cannot tell an encrypted
            # blob from a JWT from a plain id, and that distinction is the whole question when a
            # token is hiding under an obfuscated key name.
            shape = f"<{len(value)} chars"
            if value.startswith("eyJ"):
                # Base64url-encoded JSON: the start of a JWT, and a refresh token's usual look.
                shape += f", JWT-like, {value.count('.')} dot-segments"
            elif value.count(".") >= 2:
                shape += f", {value.count('.')} dot-segments"
            else:
                alphabet = set(value)
                if alphabet and alphabet <= set(
                        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=-_"):
                    shape += ", base64-ish"
                else:
                    shape += ", mixed/opaque"
            shape += ">"
        else:
            shape = type(value).__name__
        log.info("    %s @ %s = %s", key, origin, shape)
        # An opaque dotted blob is either an encrypted cache (a dead end -- the documented finding)
        # or a delimited container we can open. Decode each segment far enough to report its JSON
        # FIELD NAMES, never its contents, so the difference is settled by evidence instead of
        # assumption. MSAL cache entries carry `credentialType`/`secret`, so if those names appear
        # here the token is recoverable from storage after all.
        if isinstance(value, str) and "." in value and 60 < len(value) < 4000:
            import base64 as _b64
            for i, seg in enumerate(value.split(".")[:10]):
                if len(seg) < 8:
                    continue
                try:
                    raw = _b64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))
                except Exception:
                    continue
                inner = _try_json(raw.decode("utf-8", "ignore"))
                if isinstance(inner, dict):
                    log.info("        segment %d decodes to json{%s}", i,
                             ",".join(sorted(inner.keys())[:10]))


def _grab_refresh_token(label: str) -> str | None:
    """Reconnect to the profile's Browser-Use browser over CDP and capture a Costco refresh token.

    SIGNS ITSELF IN FIRST if the browser session has lapsed and the profile carries an
    `auth["costco"]` block -- which is what turned this from opportunistic into dependable. The
    sign-in performs a token exchange of its own, and that response is what the capture reads, so the
    old weakness ("it only works when the app happened to refresh while we watched") does not apply
    once a login has just occurred. Without an auth block the behaviour is unchanged: it hunts for a
    refresh that may never come, and returns None if the session is dead.
    """
    from config.profiles import load_profiles
    from scrapers import costco_signin
    from scrapers.cdp import CdpBrowser

    profile = next((p for p in load_profiles() if p.label == label), None)
    if profile is None:
        sys.exit(f"No profile '{label}' in config.json `profiles`.")
    if not profile.profile_id:
        sys.exit(f"Profile '{label}' has no profile_id — run scripts.create_profile and log into Costco first.")

    read_ls = (
        "() => { const o = {}; for (let i = 0; i < localStorage.length; i++)"
        " { const k = localStorage.key(i); o[k] = localStorage.getItem(k); } return o; }"
    )
    # SESSIONSTORAGE WAS NEVER READ, and that is a real hole rather than an oversight worth
    # shrugging at: MSAL.js is routinely configured with `cacheLocation: "sessionStorage"`, and
    # sessionStorage is scoped to the TAB as well as the origin, so nothing in a localStorage or
    # IndexedDB sweep can see it. Observed 2026-08-25: www.costco.com's localStorage came back
    # completely EMPTY while the account was signed in, which is exactly what a sessionStorage-backed
    # cache looks like from here.
    read_ss = (
        "() => { const o = {}; try { for (let i = 0; i < sessionStorage.length; i++)"
        " { const k = sessionStorage.key(i); o[k] = sessionStorage.getItem(k); } } catch (e) {}"
        " return o; }"
    )
    # A sentinel URL we fulfill ourselves with a blank page, so the document sits on the EXACT
    # signin.costco.com origin (no server redirect) and that origin's localStorage is readable.
    sentinel = "https://signin.costco.com/__ls_probe__"

    def _one_pass(sign_in: bool) -> str | None:
        """One browser session: optionally sign in, then watch for a token redeem and sweep."""
        with CdpBrowser(profile) as page:
            page.context.route(sentinel, lambda route: route.fulfill(
                status=200, content_type="text/html", body="<html><body>probe</body></html>"))

            # STRATEGY 0 (most robust): the app silently refreshes its token on load of an authenticated
            # area — the B2C token endpoint's RESPONSE carries a fresh refresh_token. Capture it there,
            # since the stored copy is encrypted/opaque.
            captured: dict[str, str] = {}

            def _on_response(resp):
                # Match on "/token" rather than the full "oauth2/v2.0/token" path: B2C's endpoint has
                # appeared under more than one casing/prefix, and a near-miss here fails SILENTLY --
                # the capture simply never happens and the run reports "no token found", which reads
                # like a dead session rather than a predicate that did not match.
                url = resp.url or ""
                if "/token" not in url.lower() or captured.get("rt"):
                    return
                # Say what came back, in KEY NAMES ONLY -- these bodies hold live credentials, so values
                # must never reach a log. Without this, a token response that does not carry a
                # refresh_token is indistinguishable from no response at all.
                body = None
                try:
                    body = resp.json()
                except Exception:
                    try:
                        log.info("Token-ish response %s -> status=%s, body was not JSON (%d bytes).",
                                 url[:100], resp.status, len(resp.body() or b""))
                    except Exception:
                        log.info("Token-ish response %s -> status=%s, body unreadable.",
                                 url[:100], resp.status)
                    return
                if isinstance(body, dict):
                    log.info("Token-ish response %s -> status=%s, JSON keys=%s.",
                             url[:100], resp.status, sorted(body.keys())[:12])
                if isinstance(body, dict) and body.get("refresh_token"):
                    captured["rt"] = body["refresh_token"]
                    log.info("Captured refresh_token from a token-endpoint response.")

            page.on("response", _on_response)

            # SIGN IN IF THE BROWSER SESSION HAS LAPSED TOO. Until 2026-08-25 this was the one Costco
            # failure that genuinely needed a human: a logged-out profile cannot refresh anything, so
            # the grab had nothing to watch and the run ended there. Signing in restores the session so
            # the grab can run at all.
            #
            # IT DOES NOT GUARANTEE A CAPTURE, and the opposite was asserted here first: a fresh login
            # leaves MSAL's cache FULL, so nothing refreshes and nothing is captured. Three live runs
            # signed in successfully and produced ZERO /token requests. The capture still needs MSAL to
            # actually redeem the refresh token -- which it does when its cached token has EXPIRED, i.e.
            # exactly the state costco.py calls this from. Making it deterministic after a fresh login
            # needs the cached entries EVICTED first; that is not built.
            #
            # Ordering is still not incidental: the response handler is attached ABOVE, before any
            # navigation, so any exchange that does happen is already being watched.
            try:
                page.goto("https://www.costco.com/myaccount", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(5000)
                # Costco redirects to B2C on its own schedule, so report WHERE we actually landed and
                # what we concluded. Without this line a skipped self-login is indistinguishable from a
                # session that was genuinely still warm -- which is exactly the ambiguity that cost a
                # paid run to diagnose on 2026-08-25.
                # resolve_session_state, NOT looks_logged_out: the latter infers 'signed in'
                # from the absence of a sign-in page, and a profile with no Costco session
                # can land on a bare /myaccount that trips neither check.
                logged_out = costco_signin.resolve_session_state(page)
                log.info("Costco [%s]: pass %d landed on %s (logged_out=%s).", label,
                         1 if sign_in else 2, (page.url or "")[:110], logged_out)
                if logged_out and not sign_in:
                    # Pass 2 deliberately does not sign in: pass 1 already did, and its whole purpose
                    # is to arrive with a COLD MSAL cache against the warm cookie that left behind.
                    # Landing logged out here means the sign-in did not stick, which is worth saying
                    # rather than silently sweeping empty storage.
                    log.warning("Costco [%s]: still logged out on the fresh-browser pass — the "
                                "sign-in did not persist, so there is no session to redeem against.",
                                label)
                    diagnostics.snapshot(page, "Costco token grab: pass 2 landed logged out")
                if logged_out and sign_in:
                    auth = (profile.auth or {}).get("costco")
                    if auth is None:
                        log.warning(
                            "Costco [%s]: the browser session is logged out and this profile has no "
                            "auth block, so it cannot sign itself in. Add auth['costco'] "
                            "(method/username/password) to config.json, or log in by hand with "
                            "`python -m scripts.create_profile`.", label)
                        diagnostics.snapshot(page, "Costco token grab: logged out, no auth block")
                        diagnostics.problem("Costco token grab: the profile is logged out and has no "
                                            "auth['costco'] block to sign itself in with")
                    else:
                        log.info("Costco [%s]: browser session logged out; attempting deterministic "
                                 "self-login before grabbing a token.", label)
                        outcome = costco_signin.deterministic_login(page, auth)
                        if outcome.ok:
                            log.info("Costco [%s]: deterministic self-login succeeded.", label)
                        else:
                            log.warning("Costco [%s]: deterministic self-login did not succeed. %s",
                                        label, outcome.reason or "")
            except Exception as exc:  # noqa: BLE001 -- a failed recovery must not replace the real diagnosis
                log.warning("Costco [%s]: self-login attempt errored; continuing with the grab.",
                            label, exc_info=True)
                # Swallowed here on purpose, which is exactly why CdpBrowser.__exit__ would never see
                # it: capture the page now or the dossier for this run has no page at all.
                diagnostics.snapshot(page, f"Costco token grab: self-login raised {type(exc).__name__}")
                diagnostics.problem(f"Costco token grab: self-login raised {type(exc).__name__}: {exc}")
            if captured.get("rt"):
                # The login's own exchange already handed us a token; no need to go hunting.
                return captured["rt"]

            # SWEEP IMMEDIATELY AFTER THE LOGIN, before navigating anywhere else. MSAL writes its cache
            # as part of completing the flow, and the late sweep below only reaches the sign-in origin
            # after several costco.com navigations -- so anything the site clears on unload would be
            # gone by then. The user reports a readable
            # `<account>-<policy>.<tenant>-signin.costco.com-refreshtoken-<clientId>----` key holding the
            # token as `secret`, which is MSAL's canonical key format, so it IS written somewhere; this
            # looks for it at the earliest possible moment.
            for origin_label, read_js in (("post-login www.costco.com localStorage", read_ls),
                                          ("post-login www.costco.com sessionStorage", read_ss)):
                try:
                    entries = page.evaluate(read_js) or {}
                except Exception:
                    continue
                hits = [k for k in entries if "token" in k.lower()]
                if hits:
                    log.info("post-login: %s has %d key(s) containing 'token': %s",
                             origin_label, len(hits), hits[:6])
                token = _extract_refresh_token(entries)
                if token:
                    log.info("Captured refresh_token from %s immediately after signing in.", origin_label)
                    return token
            try:
                page.goto(sentinel, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(500)
                for origin_label, read_js in (("post-login signin.costco.com localStorage", read_ls),
                                              ("post-login signin.costco.com sessionStorage", read_ss)):
                    entries = page.evaluate(read_js) or {}
                    hits = [k for k in entries if "token" in k.lower()]
                    log.info("post-login: %s -> %d key(s), %d containing 'token'%s",
                             origin_label, len(entries), len(hits),
                             (": " + ", ".join(hits[:4])) if hits else "")
                    token = _extract_refresh_token(entries)
                    if token:
                        log.info("Captured refresh_token from %s immediately after signing in.",
                                 origin_label)
                        return token
            except Exception:
                log.warning("post-login sentinel read failed.", exc_info=True)

            # The SPA route comes FIRST and is not interchangeable with the others. The myaccount
            # single-page app is what boots MSAL and asks it for an access token, which is what drives
            # the /token call this capture reads. The legacy servlet URLs below it (OrderStatusCmd et al)
            # are server-rendered and may never start the SPA at all -- observed 2026-08-25: a login
            # followed only by those produced ZERO token-endpoint requests, while a run that visited the
            # SPA route did produce one.
            for url in ("https://www.costco.com/myaccount/#/app/orderstatus",
                        "https://www.costco.com/OrderStatusCmd",
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
                # The myaccount SPA, not the homepage: it is what boots MSAL and populates its cache.
                # sessionStorage is per-TAB, so it must be read from the same tab that ran the SPA --
                # which is why this reads immediately rather than after the sentinel hop below.
                page.goto("https://www.costco.com/myaccount/#/app/orderstatus",
                          wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(6000)
                candidates.append({"origin": "www.costco.com (localStorage)", "ls": page.evaluate(read_ls)})
                candidates.append({"origin": "www.costco.com (sessionStorage)", "ls": page.evaluate(read_ss)})
            except Exception:
                log.warning("Could not load costco.com to warm the session.", exc_info=True)
            # 2) Sit on the signin origin via the sentinel and read its localStorage + IndexedDB.
            try:
                page.goto(sentinel, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(500)
                candidates.append({"origin": "signin.costco.com (sentinel, localStorage)",
                                   "ls": page.evaluate(read_ls)})
                candidates.append({"origin": "signin.costco.com (sentinel, sessionStorage)",
                                   "ls": page.evaluate(read_ss)})
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
            # Nothing matched -- dump EVERY origin's structure so we can see where the token lives.
            #
            # This used to filter to `signin.costco.com`, which quietly hid the other origins: an origin
            # with keys and an origin that was never printed looked identical in the log, and on
            # 2026-08-25 that led to the confident-but-unfounded conclusion "www.costco.com storage is
            # empty". An absence the code guarantees is not evidence of anything.
            log.info("No refresh token matched. Storage structure (values redacted):")
            for c in candidates:
                hits = [k for k in (c.get("ls") or {}) if "token" in k.lower()]
                if hits:
                    log.info("  NOTE: %s holds %d key(s) whose NAME contains 'token': %s",
                             c["origin"], len(hits), hits[:8])
            for c in candidates:
                entries = c.get("ls") or {}
                if not entries:
                    log.info("    (no entries) @ %s", c["origin"])
                    continue
                _log_storage_shape(c["origin"], entries)
        return None

    # PASS 1: recover the session if it has lapsed. This is what makes the grab possible at all
    # against a logged-out profile -- but on its own it rarely CAPTURES anything, because our
    # sign-in completes Costco's WCS flow (server-side) and hands the SPA a ready-made
    # `authToken_*`, leaving MSAL with nothing to acquire and nothing to redeem.
    token = _one_pass(sign_in=True)
    if token:
        return token

    # PASS 2: THROW THE BROWSER AWAY AND OPEN A FRESH ONE.
    #
    # This is the condition observed to actually capture, and it is worth stating plainly because
    # it is the opposite of the intuitive one. Four live runs: the three that SIGNED IN saw zero
    # `/token` requests, while the one that captured had a WARM session in a BRAND-NEW browser.
    # PROVEN AS A FALLBACK 2026-08-29: profile-bravo (logged out, no token at all) signed in on pass 1,
    # saw zero /token, and pass 2 captured within 17s of the fresh browser opening.
    # A new cloud browser starts with an empty MSAL cache, so the SPA cannot serve itself from
    # cache and must acquire -- and with the B2C SSO cookie left warm by pass 1, that acquisition
    # succeeds and redeems, putting a plaintext refresh_token on the wire where the response
    # handler is waiting.
    #
    # So the two passes are not a retry. Pass 1 supplies the warm cookie; pass 2 supplies the cold
    # cache. Neither produces a capture alone, which is exactly why this went unexplained for so
    # long. It costs a second cloud browser, and only on the path where the first pass already
    # failed -- i.e. a run that would otherwise have ended with a human being asked to intervene.
    log.info("Costco [%s]: no token captured in the first pass; retrying in a FRESH browser so "
             "MSAL has to acquire rather than serve itself from cache.", label)
    return _one_pass(sign_in=False)


def _load(label: str) -> dict:
    return load_costco_auth(label)


def _save(label: str, data: dict) -> Path:
    """Persist this profile's tokens into `.state.json` and return the file, for the printed hint."""
    save_costco_auth(label, data)
    return STATE_FILE


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
        print(f"Costco token store for '{args.label}': {STATE_FILE}")
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
