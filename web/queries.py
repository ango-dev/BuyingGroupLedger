"""Filtering, sorting and per-order grouping for the /orders views. Pure functions, no I/O."""

from __future__ import annotations

from dataclasses import dataclass

from models.order import FIELDNAMES

from web.ledger_reader import FIELD_TO_HEADER, LedgerRow

#: The columns the ledger table shows, in order, by FIELDNAMES name. Each is sortable. The heading
#: is HEADER's own name for the column so the page and the sheet never disagree on a label.
TABLE_COLUMNS = (
    "order_date", "status", "retailer", "item_name", "shipment", "quantity", "order_id",
    "tracking_number", "tracking_submitted", "delivery_date", "buying_group",
    "total_cost", "cashback_rate", "cogs", "insurance", "payout_amount", "payout_date",
    "total_profit", "profile_label", "receipt_url",
)
assert all(c in FIELDNAMES for c in TABLE_COLUMNS)

#: Sorted as numbers (blank last), everything else as text.
NUMERIC_SORT = {
    "quantity", "shipment", "total_cost", "cost_per_item", "shipping", "sales_tax", "gift_card",
    "rewards_used", "cashback_rate", "cogs", "insurance", "payout_amount", "return_quantity",
    "total_profit",
}

#: Which fields a free-text search looks in.
SEARCH_FIELDS = ("order_id", "item_name", "tracking_number", "package_id", "card_last4",
                 "delivery_address")

DEFAULT_SORT = "order_date"


@dataclass(frozen=True)
class Filters:
    retailer: str = ""
    profile: str = ""
    status: str = ""
    group: str = ""
    q: str = ""
    sort: str = DEFAULT_SORT
    desc: bool = True

    @classmethod
    def from_query(cls, params) -> "Filters":
        """From a request's query mapping. An unknown sort key falls back to the default rather
        than raising -- a stale bookmark should still render."""
        sort = str(params.get("sort") or DEFAULT_SORT).strip()
        if sort not in FIELDNAMES:
            sort = DEFAULT_SORT
        direction = str(params.get("dir") or "").strip().lower()
        desc = direction != "asc" if direction else sort in ("order_date", "delivery_date",
                                                              "payout_date", "last_scraped_at")
        return cls(
            retailer=str(params.get("retailer") or "").strip(),
            profile=str(params.get("profile") or "").strip(),
            status=str(params.get("status") or "").strip().lower(),
            group=str(params.get("group") or "").strip(),
            q=str(params.get("q") or "").strip(),
            sort=sort,
            desc=desc,
        )

    def as_query(self, **overrides) -> dict:
        values = {"retailer": self.retailer, "profile": self.profile, "status": self.status,
                  "group": self.group, "q": self.q, "sort": self.sort,
                  "dir": "desc" if self.desc else "asc"}
        values = {**values, **overrides}
        return {k: v for k, v in values.items() if v}


def _value_of(row: LedgerRow, name: str):
    if name in ("cogs", "total_profit"):
        return row.cogs if name == "cogs" else row.profit
    if name in NUMERIC_SORT:
        return row.number(name)
    return row.text(name)


def filter_rows(rows: list[LedgerRow], filters: Filters) -> list[LedgerRow]:
    needle = filters.q.lower()
    out = []
    for row in rows:
        if filters.retailer and row.retailer != filters.retailer:
            continue
        if filters.profile and row.profile != filters.profile:
            continue
        if filters.status and row.status != filters.status:
            continue
        if filters.group and (row.buying_group or "(blank)") != filters.group:
            continue
        if needle and not any(needle in row.text(f).lower() for f in SEARCH_FIELDS):
            continue
        out.append(row)
    return out


def sort_rows(rows: list[LedgerRow], filters: Filters) -> list[LedgerRow]:
    """Blanks always sort LAST, in either direction (Sheets' rule, and the sort_ledger one).
    Ties fall through to Order ID then Shipment so an order's rows stay together."""
    numeric = filters.sort in NUMERIC_SORT

    def primary(row: LedgerRow):
        value = _value_of(row, filters.sort)
        if value is None or value == "":
            return None
        return float(value) if numeric else str(value).lower()

    # Secondary order first, then a STABLE sort on the primary key: ties keep Order ID / Shipment
    # order whichever direction the primary runs, so a split order's rows stay adjacent and in
    # shipment order under a descending sort too.
    ordered = sorted(rows, key=lambda r: (r.order_id, _shipment_key(r)))
    present = [r for r in ordered if primary(r) is not None]
    blanks = [r for r in ordered if primary(r) is None]
    present.sort(key=primary, reverse=filters.desc)
    return present + blanks


def _shipment_key(row: LedgerRow):
    number = row.number("shipment")
    return (number is None, number or 0.0, row.shipment)


def facets(rows: list[LedgerRow]) -> dict:
    """The distinct values the filter selects offer, from the whole ledger (not the filtered
    subset, so narrowing on one facet never hides the others' options)."""
    return {
        "retailers": sorted({r.retailer for r in rows if r.retailer}),
        "profiles": sorted({r.profile for r in rows if r.profile}),
        "statuses": sorted({r.status for r in rows if r.status}),
        "groups": sorted({r.buying_group or "(blank)" for r in rows}),
    }


def column_headings() -> list[tuple[str, str]]:
    return [(name, FIELD_TO_HEADER[name]) for name in TABLE_COLUMNS]


# --------------------------------------------------------------------------------------------------
# One order
# --------------------------------------------------------------------------------------------------


def order_view(rows: list[LedgerRow]) -> dict | None:
    """Everything the order page shows, from that order's rows: shipments (rows grouped by the
    Shipment number, each with its tracking / package / delivery state), the money, and the
    order-level links, which are per order and so identical on every row."""
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: (_shipment_key(r), r.item_name))
    first = rows[0]

    shipments: list[dict] = []
    for row in rows:
        label = row.shipment or "(blank)"
        bucket = next((s for s in shipments if s["shipment"] == label), None)
        if bucket is None:
            bucket = {
                "shipment": label, "rows": [], "status": row.status,
                "tracking_number": row.tracking_number, "tracking_url": row.text("tracking_url"),
                "tracking_submitted": row.tracking_submitted,
                "delivery_date": row.text("delivery_date"), "package_id": row.text("package_id"),
            }
            shipments.append(bucket)
        bucket["rows"].append(row)

    def unique(name: str) -> list[str]:
        seen: list[str] = []
        for row in rows:
            value = row.text(name)
            if value and value not in seen:
                seen.append(value)
        return seen

    money_rows = [r for r in rows if not r.is_money_free]
    payout_states = {r.payout_state for r in money_rows}
    if "settled" in payout_states and "committed" not in payout_states and "none" not in payout_states:
        payout_state = "settled"
    elif "settled" in payout_states:
        payout_state = "partly settled"
    elif "committed" in payout_states:
        payout_state = "committed"
    else:
        payout_state = "none"

    return {
        "order_id": first.order_id,
        "order_date": first.order_date,
        "retailer": first.retailer,
        "profile": first.profile,
        "buying_group": first.buying_group,
        "delivery_address": first.text("delivery_address"),
        "card": first.text("card_name"),
        "card_last4": first.text("card_last4"),
        "statuses": sorted({r.status for r in rows}),
        "order_urls": unique("order_url"),
        "receipt_urls": unique("receipt_url"),
        "shipments": shipments,
        "rows": rows,
        "totals": {
            "quantity": sum(int(r.number("quantity") or 0) for r in money_rows),
            "total_cost": round(sum(r.total_cost or 0.0 for r in money_rows), 2),
            "cogs": round(sum(r.cogs or 0.0 for r in money_rows if r.cogs is not None), 2),
            "insurance": round(sum(r.number("insurance") or 0.0 for r in money_rows), 2),
            "payout": round(sum(r.payout_amount or 0.0 for r in money_rows), 2),
            "profit": round(sum(r.profit or 0.0 for r in money_rows if r.profit is not None), 2),
        },
        "payout_state": payout_state,
        "payout_dates": unique("payout_date"),
        "last_scraped_at": max((r.text("last_scraped_at") for r in rows), default=""),
    }
