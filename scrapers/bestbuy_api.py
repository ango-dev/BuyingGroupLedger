"""Best Buy order data over its own private endpoints — the cheap, deterministic primary path.

No AI agent: a CDP browser (scrapers.cdp.CdpBrowser) holds the profile's logged-in cookie, and the two
data reads ride that session. Discovered live (see reference-bestbuy-order-api /
scripts/bestbuy_capture.py). Best Buy has no portable token like Costco — auth IS the web-session
cookie (Akamai-guarded) — so the reads happen INSIDE the page:

  1. DISCOVERY: load `purchasehistory/purchases`; the order list is server-rendered as Next.js flight
     data embedded in the HTML. `_order_ids_and_dates` reassembles it and pulls each order id + date.
  2. DETAILS: for each in-window / still-open order, `fetch('/profile/ss/api/v1/orders/<id>',
     {credentials:'include'})` run in-page returns the full ss-api order JSON (tracking#, per-item cost,
     card, shipment groups). scrapers/bestbuy_mapping.py turns those into ledger rows.

If the session has lapsed (Best Buy dies ~20 min), `_deterministic_login` logs back in with the
profile's `auth.bestbuy` password creds — the exact 3-screen flow proven live (prefilled email ->
Continue -> #password-radio -> password).

WHICH ERROR IS RAISED DECIDES WHETHER MONEY IS SPENT, so the two are kept strictly apart:
  - `ApiLoginError` -> scrapers/bestbuy.py alerts and SKIPS. The agent is never run for an auth
    failure, because it cannot fix one — it would just spend ~$0.02 rediscovering the logout.
  - `BestBuyApiError` -> falls back to the Browser-Use agent (like Costco). Reserved for a genuine
    page/shape change, which is the one thing the agent CAN adapt to.
The hard case is discovery coming back empty, which both causes produce identically; see
`_signin_affordances` for how they are told apart.

The flight reassembly / order-id extraction is pure and unit-tested (tests/test_bestbuy_api.py); the
mapping is pure and unit-tested (tests/test_bestbuy_mapping.py). Only the browser mechanics need a live
run to prove.
"""

import json
import logging
import re
from typing import NamedTuple

from scrapers.base import ApiLoginError
from scrapers.cdp import CdpBrowser

log = logging.getLogger(__name__)

PURCHASE_HISTORY_URL = "https://www.bestbuy.com/purchasehistory/purchases"
ORDER_DETAIL_PATH = "/profile/ss/api/v1/orders/{}"
_SIGNIN_MARKERS = ("identity/signin", "/login", "signin/options")

# Things a page shows only when it wants you to sign in. Used ONLY as corroboration once discovery
# has already found zero orders -- deliberately NOT folded into _looks_logged_out, because a false
# positive THERE would send a perfectly good session into a doomed self-login and end in
# skip-with-alert, i.e. a silently missed run. Here the run is failing either way and the only open
# question is whether to spend money on an agent, so the safe direction is to assume logged out.
_SIGNIN_CTA_SELECTORS = (
    ".cia-signin",
    "#fld-e",
    *(f'a[href*="{marker}"]' for marker in _SIGNIN_MARKERS),
)


class BestBuyApiError(Exception):
    """The deterministic path could not run (login failed, page shape changed, network) — the caller
    should fall back to the agent."""


class _AuthTransportError(Exception):
    """INTERNAL: sign-in failed because the auth requests died at the network layer (the proxy), not
    because of anything on the page. Never escapes fetch_order_payloads — it only routes the retry to
    the off-proxy sign-in."""


# --- pure flight parsing (unit-tested) ----------------------------------------------------------
def _scan_string_literal(s: str, i: int) -> tuple[str, int]:
    """Read a JS/JSON string literal beginning at s[i] == '"', returning (literal_incl_quotes, end)."""
    out = []
    j = i + 1
    while j < len(s):
        c = s[j]
        if c == "\\":
            out.append(s[j:j + 2])
            j += 2
            continue
        if c == '"':
            return '"' + "".join(out) + '"', j + 1
        out.append(c)
        j += 1
    raise ValueError("unterminated string literal")


def _match_json(s: str, start: int) -> str:
    """Return the JSON object/array substring starting at s[start] via bracket matching."""
    open_c = s[start]
    close_c = {"{": "}", "[": "]"}[open_c]
    depth = 0
    in_str = False
    esc = False
    for k in range(start, len(s)):
        c = s[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return s[start:k + 1]
    raise ValueError("unterminated json")


def _reassemble_flight(html: str) -> str:
    """Concatenate the Next.js flight chunks (`self.__next_f.push([1,"…"])`) embedded in the page into
    the raw flight payload string."""
    parts = []
    for m in re.finditer(r"self\.__next_f\.push\(\[1,", html):
        i = m.end()
        while i < len(html) and html[i] in " \t\r\n":
            i += 1
        if i < len(html) and html[i] == '"':
            lit, _ = _scan_string_literal(html, i)
            try:
                parts.append(json.loads(lit))
            except json.JSONDecodeError:
                continue
    return "".join(parts)


def _order_ids_and_dates(html: str) -> dict[str, str]:
    """Bare order id (BBY01-<digits>, group suffix stripped) -> order date (YYYY-MM-DD) for every
    order in the purchase-history flight data. Date is best-effort ('' if not found)."""
    flight = _reassemble_flight(html)
    key = '"purchaseHistoryOrdersExperience"'
    idx = flight.find(key)
    if idx < 0:
        # Fall back to any bare ids in the raw HTML (no dates) so discovery still works.
        return {oid: "" for oid in dict.fromkeys(re.findall(r"BBY01-\d+", html))}
    colon = flight.index(":", idx + len(key))
    v = colon + 1
    while v < len(flight) and flight[v] in " \t\r\n":
        v += 1
    try:
        obj = json.loads(_match_json(flight, v))
    except (ValueError, json.JSONDecodeError):
        return {oid: "" for oid in dict.fromkeys(re.findall(r"BBY01-\d+", html))}

    result: dict[str, str] = {}

    def walk(node):
        if isinstance(node, dict):
            raw_id = node.get("id")
            if isinstance(raw_id, str) and raw_id.startswith("BBY01-"):
                bare = raw_id.split("-group")[0]
                created = node.get("created")
                date = created[:10] if isinstance(created, str) and len(created) >= 10 else ""
                # Prefer a non-empty date if we see one across the order's entries.
                if bare not in result or (date and not result[bare]):
                    result[bare] = date
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(obj)
    return result


# --- browser mechanics (needs a live session) ---------------------------------------------------
def _looks_logged_out(page) -> bool:
    url = (page.url or "").lower()
    if any(m in url for m in _SIGNIN_MARKERS):
        return True
    try:
        return page.locator(".cia-signin, #fld-e").count() > 0
    except Exception:
        return False


def _discover_orders(page) -> dict:
    """Scroll the lazily-rendered purchase list, then parse order ids + dates out of the flight data.

    Extracted so it can be run a SECOND time after a late-detected logout is self-healed -- otherwise
    recovering the session would still return the empty result parsed before signing in.
    """
    for _ in range(5):
        try:
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        page.wait_for_timeout(1200)
    return _order_ids_and_dates(page.content())


def _signin_affordances(page) -> list[str]:
    """Which sign-in CTAs the page is showing, if any.

    This exists because a silently-expired session and a genuine markup change look IDENTICAL at the
    point discovery comes back empty, and they have opposite correct responses: a logout must skip
    for free (the agent cannot fix an auth failure), while a shape change is exactly what the paid
    agent is for. Live the logged-out purchase-history page tripped neither URL redirect
    nor the sign-in form selectors, so `not dates` fired, the agent ran, cost $0.02, and concluded
    "logged out" anyway -- rediscovering for money what this catches for free.
    """
    found = []
    for selector in _SIGNIN_CTA_SELECTORS:
        try:
            if page.locator(selector).count() > 0:
                found.append(selector)
        except Exception:  # noqa: BLE001 -- a selector engine hiccup must not mask the real failure
            continue
    return found


def _dismiss_survey(page) -> None:
    try:
        page.evaluate("() => { const s = document.getElementById('survey_window'); if (s) s.remove(); }")
    except Exception:
        pass


def _click_continue(page) -> bool:
    """Click Continue on the email screen, re-dismissing the survey modal before each attempt.

    Dismissing the survey ONCE when the page loads is not enough. `#survey_window` renders lazily
    and intermittently, so it can appear between that call and this click; it overlays the button,
    Playwright's actionability check keeps waiting for a clear hit target, and the attempt times out.
    Re-dismissing immediately before each try is what makes this survive the modal.

    The last strategy dispatches the click on the element directly. That bypasses hit-testing
    altogether, so it still works if something we don't know about is covering the button — worth
    having as a fallback because the alternative is losing the whole run.
    """
    strategies = (
        ("css button.cia-form__controls__submit",
         lambda: page.locator("button.cia-form__controls__submit").first.click(timeout=8000)),
        ("role=button[name~=Continue]",
         lambda: page.get_by_role("button", name="Continue", exact=False).first.click(timeout=8000)),
        ("dispatched el.click()",
         lambda: page.eval_on_selector("button.cia-form__controls__submit", "el => el.click()")),
    )
    for label, attempt in strategies:
        _dismiss_survey(page)
        try:
            attempt()
        except Exception as exc:  # noqa: BLE001 — try the next strategy, report only if all fail
            log.debug("Best Buy sign-in: Continue via %s failed: %s", label, exc)
            continue
        log.info("Best Buy sign-in: clicked Continue via %s.", label)
        return True
    return False


#: Hosts whose failure explains an auth rejection. tmx.bestbuy.com is ThreatMetrix — Best Buy's
#: device-fingerprinting script. When it cannot load, the sign-in POST arrives without a valid
#: fingerprint and Best Buy rejects it, which surfaces as the page's own "Failed to fetch" rather
#: than anything our selectors can see.
_AUTH_CRITICAL_HOSTS = ("identity/authenticate", "gateway/graphql", "tmx.bestbuy.com")


def _watch_failed_requests(page) -> list:
    """Collect network-level request failures during sign-in.

    Diagnosed live: every click and fill worked, and sign-in still failed because
    `POST /identity/authenticate` died with ERR_HTTP2_PROTOCOL_ERROR while the ThreatMetrix script
    failed to tunnel at all. None of that is visible from the DOM — the page just says "Failed to
    fetch" — so without this the failure is indistinguishable from a selector problem and sends you
    hunting in the wrong place.
    """
    failed: list = []

    def record(request):
        try:
            if len(failed) < 20:
                failed.append({"url": request.url[:120],
                               "type": request.resource_type,
                               "error": request.failure or ""})
        except Exception:  # noqa: BLE001 — never let telemetry break a login
            pass

    try:
        page.on("requestfailed", record)
    except Exception:  # noqa: BLE001 — a page without event support still logs DOM diagnostics
        pass
    return failed


def _auth_critical(failed_requests: list | None) -> list:
    """The subset of failed requests that explain an auth rejection (see _AUTH_CRITICAL_HOSTS)."""
    return [f for f in (failed_requests or [])
            if any(h in f.get("url", "") for h in _AUTH_CRITICAL_HOSTS)]


class LoginOutcome(NamedTuple):
    """Result of a sign-in attempt.

    `transport_failed` separates the two failures that look identical from the DOM but have opposite
    fixes: a page/selector change (retrying elsewhere won't help) versus auth requests dying at the
    NETWORK layer, which the profile's proxy does intermittently — retrying the login off-proxy fixes
    that one. See reference-isp-proxy-breaks-post.
    """

    ok: bool
    transport_failed: bool = False


def _log_signin_diagnostics(page, what_failed: str, failed_requests: list | None = None) -> None:
    """Say WHY sign-in stalled, since the caller can only return False.

    A bare `return False` costs the whole run for that retailer and tells you nothing — you cannot
    tell a survey overlay from a disabled button from a CAPTCHA interstitial from a changed DOM, and
    those have completely different fixes. Best Buy sessions die in ~20 minutes, so this path runs on
    most scheduled runs and a silent failure is one you'd be guessing at for days.
    """
    try:
        info = page.evaluate(
            """() => {
                const b = document.querySelector('button.cia-form__controls__submit');
                const text = (document.body && document.body.innerText) || '';
                return {
                    url: location.href,
                    title: document.title,
                    button: b ? {
                        text: (b.innerText || '').trim().slice(0, 40),
                        disabled: !!b.disabled,
                        visible: !!(b.offsetWidth || b.offsetHeight),
                    } : null,
                    survey_present: !!document.getElementById('survey_window'),
                    password_radio: !!document.getElementById('password-radio'),
                    looks_like_challenge:
                        /captcha|unusual activity|verify it'?s you|are you a human/i.test(text),
                };
            }"""
        )
        log.warning("Best Buy sign-in: %s. Page state: %s", what_failed, info)
    except Exception:  # noqa: BLE001 — diagnostics must never mask the original failure
        log.warning("Best Buy sign-in: %s (page state unreadable).", what_failed, exc_info=True)

    if not failed_requests:
        return
    critical = [f for f in failed_requests
                if any(h in f.get("url", "") for h in _AUTH_CRITICAL_HOSTS)]
    if critical:
        log.warning(
            "Best Buy sign-in: %d auth-critical request(s) FAILED at the network layer — this is a "
            "connectivity/anti-bot problem, NOT a page-shape one, so the agent fallback cannot fix "
            "it either: %s", len(critical), critical[:6],
        )
    else:
        log.warning("Best Buy sign-in: %d request(s) failed (none auth-critical): %s",
                    len(failed_requests), failed_requests[:6])


def _deterministic_login(page, auth) -> LoginOutcome:
    """Best Buy's 3-screen password login, agent-free (proven live). Handles the fresh
    (#fld-e editable) and remembered (email prefilled as static text, just click Continue) variants.
    Uses `button.cia-form__controls__submit` — NOT the Passkey/Apple/Google buttons — then
    `#password-radio` ("Use password", below the fold).

    Returns a LoginOutcome; `transport_failed` tells the caller the auth requests died at the network
    layer, which is recoverable by retrying off-proxy rather than a reason to give up."""
    if auth is None or auth.method != "password" or not auth.username:
        return LoginOutcome(False)
    failed_requests = _watch_failed_requests(page)
    _dismiss_survey(page)

    # Screen 1: email (typed if editable, else already prefilled) -> Continue.
    try:
        page.wait_for_selector("#fld-e", state="visible", timeout=8000)
        page.fill("#fld-e", auth.username)
    except Exception:
        if page.locator(".prefilled-value, .cia-signin__username").count() == 0:
            log.warning("Best Buy sign-in page has no email field and no prefilled email.")
            return LoginOutcome(False)
    if not _click_continue(page):
        _log_signin_diagnostics(page, "could not click Continue on the email screen", failed_requests)
        return LoginOutcome(False, bool(_auth_critical(failed_requests)))

    # Screen 2: method chooser -> "Use password".
    try:
        page.wait_for_selector("#password-radio", timeout=30000)
        _dismiss_survey(page)
        page.locator("#password-radio").scroll_into_view_if_needed(timeout=8000)
        page.locator("#password-radio").click(timeout=8000)
    except Exception:
        pass  # some accounts go straight to the password field

    # Screen 3: password -> submit.
    try:
        page.wait_for_selector("input[type=password]", state="visible", timeout=30000)
        page.fill("input[type=password]", auth.password)
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
        _log_signin_diagnostics(page, "password field never appeared", failed_requests)
        return LoginOutcome(False, bool(_auth_critical(failed_requests)))

    try:
        page.wait_for_url(lambda u: "bestbuy.com" in u and not any(m in u for m in _SIGNIN_MARKERS),
                          timeout=45000)
    except Exception:
        pass
    if _looks_logged_out(page):
        # Everything clicked and filled, yet we are still on a sign-in URL. Live this was
        # the auth POST being rejected at the network layer, which no selector work can fix.
        _log_signin_diagnostics(page, "submitted the password but stayed logged out", failed_requests)
        return LoginOutcome(False, bool(_auth_critical(failed_requests)))
    return LoginOutcome(True)


_INPAGE_FETCH_JS = """
async (paths) => {
  const out = {};
  for (const p of paths) {
    try {
      const r = await fetch(p, {credentials: 'include', headers: {'accept': 'application/json'}});
      let body = null;
      try { body = await r.json(); } catch (e) { body = null; }
      out[p] = {status: r.status, body: body};
    } catch (e) { out[p] = {status: 'ERR', body: null}; }
  }
  return out;
}
"""


class BestBuyApiClient:
    def __init__(self, profile):
        self.profile = profile

    def fetch_order_payloads(
        self, since_date: str, open_ids, terminal_ids
    ) -> list[dict]:
        """Return the ss-api payload for every order placed on/after `since_date` plus every still-open
        order, minus terminal ones. Discovery + details all happen in one logged-in CDP session.

        If signing in fails because the auth requests died at the NETWORK layer, the sign-in is retried
        once in a separate session with the profile's proxy stripped, and the scrape then runs through
        the proxy as usual — see _login_without_proxy for why.
        """
        try:
            return self._fetch_once(self.profile, since_date, open_ids, terminal_ids, allow_login=True)
        except _AuthTransportError:
            if not self._login_without_proxy():
                raise ApiLoginError(
                    "Best Buy session is logged out; sign-in failed through the proxy (auth requests "
                    "died at the network layer) and the off-proxy retry did not succeed either."
                ) from None
            # The profile now holds a valid session; scrape through the proxy exactly as normal. A
            # session obtained off-proxy works through it — Best Buy does not bind it to the login IP
            # (proven live).
            return self._fetch_once(self.profile, since_date, open_ids, terminal_ids, allow_login=False)

    def _login_without_proxy(self) -> bool:
        """Sign in once with the profile's proxy stripped, persisting the session to the profile.

        The static ISP proxy intermittently breaks HTTP/2 requests that carry a BODY, so the sign-in
        POSTs (/identity/authenticate, /gateway/graphql) die with ERR_HTTP2_PROTOCOL_ERROR while every
        GET — and therefore the whole scrape — sails through. Diagnosed live with an A/B on
        the same flow minutes apart: proxy on = "Failed to fetch" and still logged out; proxy off = 200
        and signed in. Only the sign-in bypasses the proxy, so normal traffic keeps the ISP identity.
        """
        if not (self.profile.proxy and self.profile.proxy.host):
            return False  # no proxy to bypass — the failure is something else
        auth = self.profile.auth.get("bestbuy")
        if auth is None:
            return False
        log.warning(
            "Best Buy [%s]: sign-in failed through the proxy at the network layer; retrying the "
            "sign-in OFF-PROXY (the scrape still runs through the proxy).", self.profile.label,
        )
        direct = self.profile.model_copy(update={"proxy": None})
        try:
            with CdpBrowser(direct) as page:
                page.goto(PURCHASE_HISTORY_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                if not _looks_logged_out(page):
                    log.info("Best Buy [%s]: session already valid off-proxy.", self.profile.label)
                    return True
                outcome = _deterministic_login(page, auth)
        except Exception:  # noqa: BLE001 — the caller reports the original auth failure
            log.warning("Best Buy [%s]: off-proxy sign-in attempt errored.", self.profile.label,
                        exc_info=True)
            return False
        log.info("Best Buy [%s]: off-proxy sign-in %s.", self.profile.label,
                 "succeeded" if outcome.ok else "did not succeed")
        return outcome.ok

    def _fetch_once(
        self, profile, since_date: str, open_ids, terminal_ids, allow_login: bool
    ) -> list[dict]:
        open_ids = set(open_ids or [])
        terminal_ids = set(terminal_ids or [])

        with CdpBrowser(profile) as page:
            page.goto(PURCHASE_HISTORY_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

            def sign_in_here() -> None:
                """Self-heal a lapsed session, raising the right error if it can't."""
                auth = self.profile.auth.get("bestbuy")
                outcome = _deterministic_login(page, auth)
                if not outcome.ok:
                    log.warning("Best Buy [%s]: deterministic self-login did not succeed.",
                                self.profile.label)
                    # A network-layer auth failure is the proxy's doing and is retryable off-proxy;
                    # anything else (page shape, bad credentials) is not, so it fails outright.
                    if outcome.transport_failed and allow_login:
                        raise _AuthTransportError()
                    raise ApiLoginError(
                        "Best Buy session is logged out and deterministic login did not succeed."
                    )
                log.info("Best Buy [%s]: deterministic self-login succeeded.", self.profile.label)
                page.goto(PURCHASE_HISTORY_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)

            attempted_login = False
            if _looks_logged_out(page):
                # Best Buy sessions die ~20-25 min, so a scheduled run routinely lands here and must
                # self-heal. Log it so the self-login is visible in run logs (and distinguishable from
                # a warm session, which skips this block entirely).
                log.info("Best Buy [%s]: session logged out; attempting deterministic self-login.",
                         self.profile.label)
                sign_in_here()
                attempted_login = True

            dates = _discover_orders(page)

            # DISCOVERY CAME BACK EMPTY. Two causes, opposite responses, and they are indistinguishable
            # from the parse alone -- which is what the old "(shape changed?)" hedge was admitting.
            # A logout must never reach the agent (it cannot fix an auth failure and costs ~$0.02 to
            # confirm what we already know); a real markup change is precisely what the agent is for.
            if not dates and (affordances := _signin_affordances(page)):
                log.info("Best Buy [%s]: no orders parsed and the page is offering to sign in (%s) -- "
                         "treating as a lapsed session, not a shape change.",
                         self.profile.label, ", ".join(affordances))
                if not attempted_login:
                    # The session expired without tripping _looks_logged_out. Try to recover it here
                    # rather than skipping: a successful login turns a lost run into a normal one.
                    sign_in_here()
                    attempted_login = True
                    dates = _discover_orders(page)
                if not dates:
                    raise ApiLoginError(
                        "Best Buy purchase history is empty and still showing a sign-in prompt; the "
                        "session is logged out. Not running the agent -- it cannot fix an auth failure."
                    )

            if not dates:
                raise BestBuyApiError("No orders found in the purchase-history page (shape changed?).")

            # Fetch details for: orders in the date window (new) + still-open orders (re-check),
            # never terminal ones. A blank discovered date can't be filtered out, so fetch it and let
            # the mapping/date filter below decide.
            to_fetch = [
                oid for oid, date in dates.items()
                if oid not in terminal_ids and (oid in open_ids or not date or date >= since_date)
            ]
            # Always include known-open orders even if they scrolled off the page.
            for oid in open_ids:
                if oid not in to_fetch and oid not in terminal_ids:
                    to_fetch.append(oid)

            log.info("Best Buy [%s]: %d order(s) on page, fetching %d detail(s) via ss-api.",
                     self.profile.label, len(dates), len(to_fetch))
            if not to_fetch:
                return []

            payloads: list[dict] = []
            results = page.evaluate(_INPAGE_FETCH_JS,
                                    [ORDER_DETAIL_PATH.format(oid) for oid in to_fetch])
            for path, res in (results or {}).items():
                oid = path.rsplit("/", 1)[-1]
                if res.get("status") == 200 and isinstance(res.get("body"), dict) and res["body"].get("order"):
                    payloads.append(res["body"])
                else:
                    log.warning("Best Buy [%s]: detail fetch for %s returned status=%s (skipped).",
                                self.profile.label, oid, res.get("status"))

        # Keep only in-window or still-open orders (using the authoritative order.created date).
        kept = []
        for payload in payloads:
            order = payload.get("order") or {}
            oid = str(order.get("userOrderId") or "")
            created = (order.get("created") or "")[:10]
            if oid in open_ids or not created or created >= since_date:
                kept.append(payload)
        return kept
