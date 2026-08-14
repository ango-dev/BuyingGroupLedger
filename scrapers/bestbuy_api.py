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

If the session has lapsed (Best Buy dies ~20 min), `_ensure_logged_in` logs back in deterministically
with the profile's `auth.bestbuy` password creds — the exact 3-screen flow proven live (prefilled
email -> Continue -> #password-radio -> password). Any failure raises `BestBuyApiError`, and
scrapers/bestbuy.py then falls back to the Browser-Use agent (like Costco).

The flight reassembly / order-id extraction is pure and unit-tested (tests/test_bestbuy_api.py); the
mapping is pure and unit-tested (tests/test_bestbuy_mapping.py). Only the browser mechanics need a live
run to prove.
"""

import json
import logging
import re

from scrapers.base import ApiLoginError
from scrapers.cdp import CdpBrowser

log = logging.getLogger(__name__)

PURCHASE_HISTORY_URL = "https://www.bestbuy.com/purchasehistory/purchases"
ORDER_DETAIL_PATH = "/profile/ss/api/v1/orders/{}"
_SIGNIN_MARKERS = ("identity/signin", "/login", "signin/options")


class BestBuyApiError(Exception):
    """The deterministic path could not run (login failed, page shape changed, network) — the caller
    should fall back to the agent."""


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


def _log_signin_diagnostics(page, what_failed: str) -> None:
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


def _deterministic_login(page, auth) -> bool:
    """Best Buy's 3-screen password login, agent-free (proven live). Returns True if it
    lands authenticated. Handles the fresh (#fld-e editable) and remembered (email prefilled as static
    text, just click Continue) variants. Uses `button.cia-form__controls__submit` — NOT the
    Passkey/Apple/Google buttons — then `#password-radio` ("Use password", below the fold)."""
    if auth is None or auth.method != "password" or not auth.username:
        return False
    _dismiss_survey(page)

    # Screen 1: email (typed if editable, else already prefilled) -> Continue.
    try:
        page.wait_for_selector("#fld-e", state="visible", timeout=8000)
        page.fill("#fld-e", auth.username)
    except Exception:
        if page.locator(".prefilled-value, .cia-signin__username").count() == 0:
            log.warning("Best Buy sign-in page has no email field and no prefilled email.")
            return False
    if not _click_continue(page):
        _log_signin_diagnostics(page, "could not click Continue on the email screen")
        return False

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
        return False

    try:
        page.wait_for_url(lambda u: "bestbuy.com" in u and not any(m in u for m in _SIGNIN_MARKERS),
                          timeout=45000)
    except Exception:
        pass
    return not _looks_logged_out(page)


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
        order, minus terminal ones. Discovery + details all happen in one logged-in CDP session."""
        open_ids = set(open_ids or [])
        terminal_ids = set(terminal_ids or [])

        with CdpBrowser(self.profile) as page:
            page.goto(PURCHASE_HISTORY_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

            if _looks_logged_out(page):
                # Best Buy sessions die ~20-25 min, so a scheduled run routinely lands here and must
                # self-heal. Log it so the self-login is visible in run logs (and distinguishable from
                # a warm session, which skips this block entirely).
                log.info("Best Buy [%s]: session logged out; attempting deterministic self-login.",
                         self.profile.label)
                auth = self.profile.auth.get("bestbuy")
                if not _deterministic_login(page, auth):
                    log.warning("Best Buy [%s]: deterministic self-login did not succeed.",
                                self.profile.label)
                    raise ApiLoginError(
                        "Best Buy session is logged out and deterministic login did not succeed."
                    )
                log.info("Best Buy [%s]: deterministic self-login succeeded.", self.profile.label)
                page.goto(PURCHASE_HISTORY_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)

            # Lazy-load the list so older in-window orders render into the flight data.
            for _ in range(5):
                try:
                    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    pass
                page.wait_for_timeout(1200)

            dates = _order_ids_and_dates(page.content())
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
