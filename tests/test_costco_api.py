"""Costco API client — proxy routing.

The Costco path is the only retailer that calls an API over plain HTTP instead of driving a browser,
so it's the only one that has to apply the profile's static ISP proxy itself. If it silently stops
doing that, Costco sees the account from the host's IP (a datacenter address once this runs in
Docker) while the agent fallback still comes from the proxy — an account-health risk no test would
otherwise catch. These pin that BOTH outbound calls (token exchange + GraphQL) carry the proxy.

Fully offline: curl_cffi is swapped for a recorder.
"""

import json
import time

import jwt
import pytest

from models.profile import ProxyConfig
from scrapers import costco_api
from scrapers.costco_api import CostcoApiClient

PROXY = ProxyConfig(host="200.0.0.1", port=50100, username="user", password="pass")
EXPECTED = {"http": "http://user:pass@200.0.0.1:50100", "https": "http://user:pass@200.0.0.1:50100"}


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class RecordingHttp:
    """Stands in for curl_cffi.requests, recording every call's kwargs."""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return FakeResponse(self.payload)


def _token(exp_offset=3600):
    # Signature is never verified (the client decodes with verify_signature=False); the key is only
    # long enough to keep PyJWT from warning.
    return jwt.encode({"exp": time.time() + exp_offset}, "x" * 32, algorithm="HS256")


@pytest.fixture
def stored_token(tmp_path, monkeypatch):
    """A stored token with a still-valid id_token, so calls don't trigger a refresh.

    Writes `.state.json` — where the tokens live now — rather than a config file: they ROTATE on
    every refresh, which is exactly why they are state and not configuration.
    """
    from config import loader

    state = tmp_path / ".state.json"
    monkeypatch.setattr(loader, "STATE_FILE", state)
    state.write_text(
        json.dumps({"costco": {"p1": {"refresh_token": "rt", "id_token": _token()}}}),
        encoding="utf-8",
    )
    return state


@pytest.fixture
def http(monkeypatch):
    recorder = RecordingHttp({"data": {"getOnlineOrders": {"bcOrders": [], "totalNumberOfRecords": 0}}})
    monkeypatch.setattr(costco_api, "curl_requests", recorder)
    return recorder


def test_graphql_call_goes_through_the_proxy(stored_token, http):
    client = CostcoApiClient("p1", proxy=PROXY)

    client.list_order_numbers("2026-08-01", "2026-08-13")

    assert http.calls, "expected a GraphQL POST"
    assert all(c["proxies"] == EXPECTED for c in http.calls)


def test_token_exchange_goes_through_the_proxy(tmp_path, monkeypatch):
    # No cached id_token -> the client must refresh, and that call must be proxied too (it's the
    # identity-minting request, so it especially must not leak the host IP).
    from config import loader

    state = tmp_path / ".state.json"
    monkeypatch.setattr(loader, "STATE_FILE", state)
    state.write_text(json.dumps({"costco": {"p1": {"refresh_token": "rt"}}}), encoding="utf-8")
    recorder = RecordingHttp({"id_token": _token(), "refresh_token": "rt2"})
    monkeypatch.setattr(costco_api, "curl_requests", recorder)

    client = CostcoApiClient("p1", proxy=PROXY)
    client._bearer_token()

    assert len(recorder.calls) == 1
    assert recorder.calls[0]["url"] == costco_api.TOKEN_ENDPOINT
    assert recorder.calls[0]["proxies"] == EXPECTED


def test_no_proxy_configured_sends_none(stored_token, http):
    client = CostcoApiClient("p1", proxy=None)

    client.list_order_numbers("2026-08-01", "2026-08-13")

    assert all(c["proxies"] is None for c in http.calls)


def test_blank_host_counts_as_no_proxy(stored_token, http):
    # Matches the browser paths' `if proxy and proxy.host` guard — a blank host must not become
    # a bogus "http://:0" URL that would break every request.
    client = CostcoApiClient("p1", proxy=ProxyConfig(host="", port=0))

    client.list_order_numbers("2026-08-01", "2026-08-13")

    assert all(c["proxies"] is None for c in http.calls)
