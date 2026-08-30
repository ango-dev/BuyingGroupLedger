"""The CALL SITES of the Best Buy / Costco payload-shape guard, driven for real.

both guards fired on a renamed key, but the handlers that were meant to attach the
payload to the dossier raised NameError (no `diagnostics` import) -- the run still failed loud, with
the wrong error and no payload. Every other test mocks `_scrape_via_api` wholesale, which is exactly
how that slipped through. These drive the real method with a fake client.
"""

import pytest

import diagnostics
from models.profile import ProfileConfig

_EMPTY_STATE = {"open_orders": [], "delivered_ids": [], "cancelled_ids": []}


class TestCostco:
    def test_a_shape_error_attaches_the_payload_and_raises_the_api_error(self, monkeypatch, tmp_path):
        from scrapers import costco
        from scrapers.costco_api import CostcoApiError
        import scrapers.costco_api as api

        bad = {"orderNumber": "1399000007"}  # no shipToAddress -> the mapping's shape guard

        class _Client:
            def __init__(self, *a, **k):
                pass

            def list_order_numbers(self, start, end):
                return ["1399000007"]

            def get_order_details(self, numbers):
                return [bad]

        monkeypatch.setattr(api, "CostcoApiClient", _Client)
        scraper = costco.CostcoScraper(ProfileConfig(label="p", profile_id="x", retailers=["costco"]), lookback_days=1)
        monkeypatch.setattr(scraper, "_load_order_state", lambda: _EMPTY_STATE)

        with diagnostics.collecting("costco", "p", root=tmp_path) as d:
            with pytest.raises(CostcoApiError, match="shipToAddress"):
                scraper._scrape_via_api()
        assert d.responses and d.responses[0]["label"].startswith("getOrderDetails order (shape)")


class TestBestBuy:
    def test_a_shape_error_attaches_the_payload_and_raises_the_api_error(self, monkeypatch, tmp_path):
        from scrapers import bestbuy
        from scrapers.bestbuy_api import BestBuyApiError
        import scrapers.bestbuy_api as api

        bad = {"order": {"userOrderId": "BBY01-1", "items": [{"id": "a"}], "groups": {}}}

        class _Client:
            def __init__(self, *a, **k):
                pass

            def fetch_order_payloads(self, since, open_ids, terminal_ids):
                return [bad]

        monkeypatch.setattr(api, "BestBuyApiClient", _Client)
        scraper = bestbuy.BestBuyScraper(ProfileConfig(label="p", profile_id="x", retailers=["bestbuy"]), lookback_days=1)
        monkeypatch.setattr(scraper, "_load_order_state", lambda: _EMPTY_STATE)

        with diagnostics.collecting("bestbuy", "p", root=tmp_path) as d:
            with pytest.raises(BestBuyApiError, match="fulfillmentGroups"):
                scraper._scrape_via_api()
        assert d.responses and d.responses[0]["label"].startswith("ss-api order payload (shape)")
