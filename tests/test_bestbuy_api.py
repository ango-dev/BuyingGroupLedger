"""Offline tests for the pure flight-data parsing in scrapers/bestbuy_api.py (no browser).

Guards the reassembly + order-id/date extraction from Best Buy's Next.js purchase-history page, which
is the fragile part of the deterministic discovery step. The live browser mechanics are proven by a
real run instead.
"""

import json

from scrapers.bestbuy_api import _order_ids_and_dates, _reassemble_flight


def _flight_html(*chunks: str) -> str:
    """Wrap raw flight-text chunks the way Next.js embeds them in the page."""
    return "".join(f"<script>self.__next_f.push([1,{json.dumps(c)}])</script>" for c in chunks)


def test_reassembles_chunks_in_order():
    html = _flight_html("hello ", "world")
    assert _reassemble_flight(html) == "hello world"


def test_extracts_order_ids_and_dates_from_flight():
    orders = {
        "purchaseHistoryOrdersExperience": {
            "openOrders": {
                "entries": [
                    # open entries are OpenOrderEntry objects keyed by "…-group_N" (native shipment)
                    [{"id": "BBY01-809900000006-group_1", "created": "2026-08-10T14:27:15-05:00"}],
                    [
                        {"id": "BBY01-809900000005-group_1", "created": "2026-08-10T10:29:13-05:00"},
                        {"id": "BBY01-809900000005-group_2", "created": "2026-08-10T10:29:13-05:00"},
                    ],
                ]
            },
            "closedOrdersAndTransactions": {
                "entries": [{"id": "BBY01-809900000001", "created": "2026-08-05T23:29:08-05:00"}]
            },
        }
    }
    html = _flight_html("2:", json.dumps(orders))
    dates = _order_ids_and_dates(html)
    assert dates == {
        "BBY01-809900000006": "2026-08-10",
        "BBY01-809900000005": "2026-08-10",  # group suffix stripped, one entry per bare order id
        "BBY01-809900000001": "2026-08-05",
    }


def test_falls_back_to_bare_ids_when_flight_key_absent():
    # No purchaseHistoryOrdersExperience object -> still discover ids from the raw HTML (dateless).
    html = "<div>order BBY01-999 and BBY01-888 and BBY01-999 again</div>"
    dates = _order_ids_and_dates(html)
    assert dates == {"BBY01-999": "", "BBY01-888": ""}
