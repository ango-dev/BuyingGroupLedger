"""Costco.com order data over its private GraphQL API — the cheap, deterministic primary path.

No browser and no AI agent at run time: a long-lived **refresh token** (grabbed once from a browser
session, see scripts/costco_token.py) is exchanged for a short-lived id_token, which authorizes two
GraphQL calls — `getOnlineOrders` (discover order numbers in a date range) and `getOrderDetails`
(everything the ledger needs per order). `scrapers/costco.py` falls back to the Browser-Use agent if
this path errors (auth dead, schema changed, network), so a broken API degrades instead of missing
orders.

curl_cffi impersonates a real Chrome TLS fingerprint because Costco's edge blocks vanilla clients.

Requests go through the profile's static ISP proxy when one is configured (`CostcoApiClient(...,
proxy=profile.proxy)`), so Costco sees this account from the same IP as the browser paths (agent
fallback / CDP) rather than the host's own IP — which also matters once this runs in Docker/a server,
where the host IP would be a datacenter address.

The queries here are trimmed to only the fields `costco_mapping.build_order_items` consumes. The full
schema (captured working against costco.com) has ~100 more fields per line item; adding one back is
just pasting it into QUERY_ORDER_DETAILS.
"""

import json
import logging
import re
import time
from pathlib import Path

import jwt
from curl_cffi import requests as curl_requests

import diagnostics
from config.loader import load_state, save_state
from scrapers.base import ApiLoginError

log = logging.getLogger(__name__)

# --- Endpoints / client identifiers (from Costco's own web app; not secrets) --------------------
_IMPERSONATE = "chrome131"

TENANT_ID = "e0714dd4-784d-46d6-a278-3e29553483eb"
POLICY_NAME = "b2c_1a_sso_wcs_signup_signin_209"
TOKEN_ENDPOINT = f"https://signin.costco.com/{TENANT_ID}/{POLICY_NAME}/oauth2/v2.0/token"
MSAL_CLIENT_ID = "a3a5186b-7c89-4b4c-93a8-dd604e930757"
# GLOBAL, not per-account. In the id_token JWT this appears as an app-level `issuer` (the per-user
# value is `sub`/`issuerUserId`), and a live probe confirmed the ecom API IGNORES this header entirely:
# a bogus value — or omitting `costco-x-wcs-clientid` — still returns the same account's orders, because
# identity comes solely from the id_token bearer. Costco's web app reads it from localStorage, but the
# value is shared. So hardcoding is safe for any consumer account. (A Costco *Business Center* account
# could use a different WCS app; capture it per-account then, but it's irrelevant to the API anyway.)
WCS_CLIENT_ID = "4900eb1f-0c10-4bd9-99c3-c59e6c1ecebf"
# Static client identifier the ecom API gateway REQUIRES and VALIDATES — a bogus value or omitting it
# 401s "Missing credentials" even with a valid bearer token (live-probed). But it's a fixed GLOBAL
# constant (the reference script hardcodes the same value), so it's correct for every account.
CLIENT_IDENTIFIER = "481b1aec-aa3b-454b-b81b-48187e28f205"
GRAPHQL_ENDPOINT = "https://ecom-api.costco.com/ebusiness/order/v1/orders/graphql"

# Per-profile token cache lives here (gitignored). One file per profile label so several Costco
# memberships can run side by side.
#: Costco's tokens live in `.state.json` under `costco.<profile label>`, NOT in the config file.
#:
#: THEY ROTATE. Costco issues a NEW refresh token on every refresh and invalidates the old one, so
#: this is written several times a day — which is exactly why it is state rather than configuration,
#: and why its file is the one thing mounted writable in docker-compose.yml while config.json stays
#: read-only. Losing it costs nothing permanent: re-run `scripts.costco_token`.


def load_costco_auth(profile_label: str) -> dict:
    """This profile's stored Costco tokens, or {} if it has never authenticated."""
    return (load_state().get("costco") or {}).get(profile_label) or {}


def save_costco_auth(profile_label: str, auth: dict) -> None:
    """Persist this profile's tokens, leaving every other profile's entry untouched.

    Read-modify-write rather than a whole-file rewrite: several profiles share `.state.json`, and a
    blind overwrite during a multi-profile run would drop the tokens of whichever profile wrote first.
    """
    state = load_state()
    state.setdefault("costco", {})[profile_label] = auth
    save_state(state)
DEFAULT_WAREHOUSES = ["847"]

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_TOKEN_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://www.costco.com",
    "Referer": "https://www.costco.com/",
    "User-Agent": _USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

QUERY_ONLINE_ORDERS = """
query getOnlineOrders($startDate: String!, $endDate: String!, $pageNumber: Int, $pageSize: Int, $warehouseNumber: String!) {
    getOnlineOrders(startDate: $startDate, endDate: $endDate, pageNumber: $pageNumber, pageSize: $pageSize, warehouseNumber: $warehouseNumber) {
        pageNumber
        pageSize
        totalNumberOfRecords
        bcOrders {
            orderNumber: sourceOrderNumber
        }
    }
}
"""

# Trimmed to exactly the fields costco_mapping.build_order_items reads.
QUERY_ORDER_DETAILS = """
query getOrderDetails($orderNumbers: [String]) {
    getOrderDetails(orderNumbers: $orderNumbers) {
        orderNumber: sourceOrderNumber
        orderPlacedDate: orderedDate
        status
        shippingAndHandling
        orderPayment {
            paymentType
            cardNumber
        }
        shipToAddress: orderShipTos {
            firstName
            lastName
            line1
            line2
            line3
            city
            state
            postalCode
            countryCode
            orderLineItems {
                itemNumber
                itemDescription: sourceItemDescription
                price: unitPrice
                quantity: orderedTotalQuantity
                discountAmount
                isFeeItem
                carrierItemCategory
                orderedShipMethod
                itemStatus {
                    cancelled {
                        quantity
                    }
                    delivered {
                        quantity
                    }
                }
                shipment {
                    trackingNumber
                    trackingSiteUrl
                    carrierName
                    packageNumber
                    shippedDate
                    estimatedArrivalDate
                    deliveredDate
                }
            }
        }
    }
}
"""


class CostcoAuthError(ApiLoginError):
    """No usable refresh token, or the token exchange failed — the profile needs re-authorizing.

    Subclasses ApiLoginError so costco.scrape() treats it as a login failure (alert + skip, NO agent)
    rather than a DOM-change fallback."""


class CostcoApiError(Exception):
    """The GraphQL API returned an error or an unexpected shape."""


def _operation_name(query: str) -> str:
    """'getOnlineOrders' / 'getOrderDetails' from the query text, for labelling a dossier entry."""
    m = re.search(r"\b(getOnlineOrders|getOrderDetails|\w+)\s*\(", query or "")
    return m.group(1) if m else "query"


def _is_token_expired(id_token: str, buffer_seconds: int = 120) -> bool:
    try:
        payload = jwt.decode(id_token, options={"verify_signature": False})
        return time.time() >= (payload.get("exp", 0) - buffer_seconds)
    except jwt.PyJWTError:
        return True


class CostcoApiClient:
    def __init__(self, profile_label: str, proxy=None):
        """`proxy` is the profile's `ProxyConfig` (config.json). Passing it routes BOTH the token
        exchange and the GraphQL calls through that static ISP proxy, so Costco sees this account from
        the same IP as the browser paths (agent fallback / CDP), instead of the host's own IP. Optional
        so a proxy-less profile still works; None = direct, the pre-2026-08-13 behavior."""
        self.profile_label = profile_label
        self._auth = load_costco_auth(profile_label)
        self.warehouse_numbers = [
            str(w) for w in (self._auth.get("warehouse_numbers") or DEFAULT_WAREHOUSES)
        ]
        # Same guard the browser paths use (`if proxy and proxy.host`) — an entry with a blank host
        # counts as "no proxy" rather than producing a bogus "http://:0" URL.
        self._proxies: dict[str, str] | None = None
        if proxy is not None and getattr(proxy, "host", ""):
            url = proxy.as_url()
            self._proxies = {"http": url, "https": url}
            log.info("Costco [%s]: API requests routed through proxy %s:%s",
                     profile_label, proxy.host, proxy.port)

    # --- token cache -----------------------------------------------------------------------------
    def _save_auth(self) -> None:
        save_costco_auth(self.profile_label, self._auth)

    def _refresh(self, refresh_token: str) -> dict:
        resp = curl_requests.post(
            TOKEN_ENDPOINT,
            data={
                "client_id": MSAL_CLIENT_ID,
                "scope": "openid profile offline_access",
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers=_TOKEN_HEADERS,
            impersonate=_IMPERSONATE,
            proxies=self._proxies,
            timeout=30,
        )
        if not resp.ok:
            diagnostics.record_response("MSAL refresh-token exchange", resp.status_code, resp.text,
                                        request={"url": TOKEN_ENDPOINT, "grant_type": "refresh_token"})
            raise CostcoAuthError(
                f"Refresh-token exchange failed (HTTP {resp.status_code}). Re-run "
                f"scripts.costco_token for profile '{self.profile_label}'. Body: {resp.text[:300]}"
            )
        result = resp.json()
        # Whatever tokens came back must never be written into a dossier.
        diagnostics.add_secrets(result.get("id_token"), result.get("refresh_token"),
                                result.get("access_token"))
        return result

    def _bearer_token(self) -> str:
        refresh_token = self._auth.get("refresh_token")
        diagnostics.add_secrets(refresh_token, self._auth.get("id_token"))
        if not refresh_token:
            raise CostcoAuthError(
                f"No Costco refresh token for profile '{self.profile_label}'. Run "
                f"`python -m scripts.costco_token --label {self.profile_label} --token <REFRESH_TOKEN>`."
            )
        id_token = self._auth.get("id_token")
        if id_token and not _is_token_expired(id_token):
            return id_token

        log.info("Costco id_token missing/expired for '%s'; refreshing.", self.profile_label)
        result = self._refresh(refresh_token)
        id_token = result.get("id_token")
        if not id_token:
            raise CostcoAuthError("Token refresh response had no id_token.")
        self._auth["id_token"] = id_token
        # MSAL rotates refresh tokens — persist the new one so the next run isn't stranded.
        self._auth["refresh_token"] = result.get("refresh_token", refresh_token)
        self._save_auth()
        return id_token

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json-patch+json",
            "Accept": "*/*",
            "Origin": "https://www.costco.com",
            "Referer": "https://www.costco.com/",
            "User-Agent": _USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "costco-x-authorization": f"Bearer {self._bearer_token()}",
            "costco-x-wcs-clientid": WCS_CLIENT_ID,
            "client-identifier": CLIENT_IDENTIFIER,
            "costco.env": "ecom",
            "costco.service": "restOrders",
        }

    # --- GraphQL ---------------------------------------------------------------------------------
    def _post_graphql(self, query: str, variables: dict) -> dict:
        """POST a query, retrying once on a 401 by forcing a token refresh."""
        for attempt in range(2):
            resp = curl_requests.post(
                GRAPHQL_ENDPOINT,
                json={"query": query, "variables": variables},
                headers=self._headers(),
                impersonate=_IMPERSONATE,
                proxies=self._proxies,
                timeout=30,
            )
            if resp.status_code == 401 and attempt == 0:
                log.info("Costco GraphQL 401; forcing token refresh and retrying.")
                self._auth.pop("id_token", None)
                continue
            op = _operation_name(query)
            request = {"operation": op, "variables": variables, "query": query}
            if not resp.ok:
                diagnostics.record_response(f"GraphQL {op}", resp.status_code, resp.text, request)
                raise CostcoApiError(f"GraphQL HTTP {resp.status_code}: {resp.text[:300]}")
            payload = resp.json()
            if payload.get("errors"):
                diagnostics.record_response(f"GraphQL {op} (errors)", resp.status_code, payload, request)
                raise CostcoApiError(f"GraphQL errors: {json.dumps(payload['errors'])[:300]}")
            data = payload.get("data")
            if not isinstance(data, dict):
                diagnostics.record_response(f"GraphQL {op} (bad shape)", resp.status_code, payload,
                                            request)
                raise CostcoApiError(f"Unexpected GraphQL response shape: {json.dumps(payload)[:300]}")
            diagnostics.note("costco", f"GraphQL {op} ok (keys: {', '.join(sorted(data))})")
            return data
        raise CostcoAuthError("Authentication failed after a token refresh retry.")

    def list_order_numbers(self, start_date: str, end_date: str, page_size: int = 25) -> list[str]:
        """Every online order number placed in [start_date, end_date], across the configured
        warehouse(s). Costco requires a single warehouseNumber per call and has no 'all' value, so we
        page through each warehouse and merge, de-duplicating by order number (first seen wins)."""
        seen: dict[str, None] = {}
        for warehouse in self.warehouse_numbers:
            page_number = 1
            while True:
                data = self._post_graphql(
                    QUERY_ONLINE_ORDERS,
                    {
                        "startDate": start_date,
                        "endDate": end_date,
                        "warehouseNumber": warehouse,
                        "pageNumber": page_number,
                        "pageSize": page_size,
                    },
                )
                payload = data.get("getOnlineOrders")
                if isinstance(payload, list):
                    payload = payload[0] if payload else None
                if not isinstance(payload, dict):
                    break
                orders = payload.get("bcOrders") or []
                for order in orders:
                    number = order.get("orderNumber") if isinstance(order, dict) else None
                    if number:
                        seen[str(number)] = None
                total = payload.get("totalNumberOfRecords") or 0
                if not orders or page_number * page_size >= total:
                    break
                page_number += 1
        return list(seen.keys())

    def get_order_details(self, order_numbers: list[str]) -> list[dict]:
        """Full detail for each order number. getOrderDetails returns a single object even though its
        argument is a list (confirmed against the live API), and it's unclear whether a multi-number
        call returns all matches or just the first — so we fetch one order per call (safe if slower)."""
        details: list[dict] = []
        for number in order_numbers:
            data = self._post_graphql(QUERY_ORDER_DETAILS, {"orderNumbers": [number]})
            raw = data.get("getOrderDetails")
            if isinstance(raw, dict):
                details.append(raw)
            elif isinstance(raw, list):
                details.extend(d for d in raw if isinstance(d, dict))
        return details
