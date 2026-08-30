"""Bisect candidate GraphQL field names against a live Costco order. READ-ONLY.

    python -m scripts.costco_order_probe                        # probe the most recent order
    python -m scripts.costco_order_probe 1234567890             # probe a specific order number
    python -m scripts.costco_order_probe --scan-days 180        # ...then sweep for gift-card tenders
    python -m scripts.costco_order_probe --label profile-alpha

WHY THIS EXISTS. The gateway DISABLES GraphQL introspection ("Introspection is not allowed.") and an
unknown field fails the WHOLE query with no partial results — so the only way to learn a field name
is to guess one candidate per call against a real order and read the validation error, exactly how
`discountAmount` was found on 2026-08-13. This script
automates that bisection for the Sales Tax and gift-card-tender amounts the 2026-08-30 columns need.

Nothing here writes anywhere: every call is a GraphQL read (the token exchange rewriting
`.state.json` is the client's normal rotation). Each rejected candidate costs one free API call.
"""

from __future__ import annotations

import argparse
import json
import sys

# Candidate field names, one probe call each. Order-level candidates sit beside
# `shippingAndHandling`; payment-level ones inside `orderPayment { ... }`; line-level ones beside
# `discountAmount`. Listed most-likely first (the API's own style: camelCase, "Amount" suffixes —
# discountAmount / shippingAndHandling are the two known money fields).
CANDIDATES = {
    "order": ["taxAmount", "totalTax", "salesTax", "taxTotal", "totalTaxAmount", "tax",
              "salesTaxAmount", "estimatedTax"],
    "payment": ["amount", "amountCharged", "chargedAmount", "totalCharged", "paymentAmount",
                "authorizedAmount", "totalAmount", "transactionAmount"],
    "line_item": ["taxAmount", "salesTax", "lineTaxAmount"],
}

_ORDER_TEMPLATE = """
query getOrderDetails($orderNumbers: [String]) {
    getOrderDetails(orderNumbers: $orderNumbers) {
        orderNumber: sourceOrderNumber
        %s
    }
}
"""
_SHAPES = {
    "order": "%s",
    "payment": "orderPayment { paymentType %s }",
    "line_item": "shipToAddress: orderShipTos { orderLineItems { itemNumber %s } }",
}


def _client(label: str | None):
    from config.profiles import load_profiles
    from scrapers.costco_api import CostcoApiClient, load_costco_auth

    profiles = load_profiles()
    if label:
        profile = next((p for p in profiles if p.label == label), None)
        if profile is None:
            sys.exit(f"No profile '{label}' in config.json `profiles`.")
    else:
        profile = next((p for p in profiles if load_costco_auth(p.label)), None)
        if profile is None:
            sys.exit("No profile has stored Costco tokens (.state.json). Pass --label.")
    print(f"Using profile '{profile.label}'.")
    return CostcoApiClient(profile.label, proxy=profile.proxy)


def probe_candidates(client, order_number: str) -> dict[str, dict]:
    """Try every candidate against one order; {level: {name: value | '<REJECTED> ...'}}."""
    from scrapers.costco_api import CostcoApiError

    results: dict[str, dict] = {}
    for level, names in CANDIDATES.items():
        results[level] = {}
        for name in names:
            query = _ORDER_TEMPLATE % (_SHAPES[level] % name)
            try:
                data = client._post_graphql(query, {"orderNumbers": [order_number]})
            except CostcoApiError as exc:
                results[level][name] = f"<REJECTED> {str(exc)[:160]}"
                continue
            raw = data.get("getOrderDetails")
            if isinstance(raw, list):
                raw = raw[0] if raw else {}
            results[level][name] = _extract(level, raw or {}, name)
    return results


def _extract(level: str, detail: dict, name: str):
    if level == "order":
        return detail.get(name)
    if level == "payment":
        return [p.get(name) for p in detail.get("orderPayment") or []]
    return [li.get(name)
            for st in detail.get("shipToAddress") or []
            for li in st.get("orderLineItems") or []]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only Costco GraphQL field-name bisection.")
    ap.add_argument("orders", nargs="*", help="order number(s) to probe; default: the most recent")
    ap.add_argument("--label", help="profile label (default: first with stored Costco tokens)")
    ap.add_argument("--days", type=int, default=90, help="discovery window when no order is given")
    ap.add_argument("--scan-days", type=int, default=0,
                    help="after probing, sweep this many days of orders and print every "
                         "non-credit-card tender (to find the gift-card order)")
    args = ap.parse_args(argv)

    from datetime import date, timedelta

    client = _client(args.label)
    order_numbers = list(args.orders)
    if not order_numbers:
        end = (date.today() + timedelta(days=1)).isoformat()
        start = (date.today() - timedelta(days=args.days)).isoformat()
        found = client.list_order_numbers(start, end)
        if not found:
            sys.exit(f"No orders found in the last {args.days} day(s); pass an order number.")
        order_numbers = [found[0]]
        print(f"{len(found)} order(s) in the window; probing the first: {order_numbers[0]}")

    for number in order_numbers:
        print(f"\n=== order {number} ===")
        for level, outcomes in probe_candidates(client, number).items():
            print(f"  [{level}]")
            for name, value in outcomes.items():
                mark = "xx" if isinstance(value, str) and value.startswith("<REJECTED>") else "OK"
                print(f"    {mark} {name}: {value}")

    if args.scan_days:
        end = (date.today() + timedelta(days=1)).isoformat()
        start = (date.today() - timedelta(days=args.scan_days)).isoformat()
        numbers = client.list_order_numbers(start, end)
        print(f"\n=== tender scan: {len(numbers)} order(s) over {args.scan_days} day(s) ===")
        for detail in client.get_order_details(numbers):
            payments = detail.get("orderPayment") or []
            types = [p.get("paymentType") for p in payments]
            flag = " <-- non-card tender" if any(
                "card" not in str(t).lower() or "shop" in str(t).lower() for t in types if t
            ) else ""
            print(f"  {detail.get('orderNumber')}: {json.dumps(types)}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
