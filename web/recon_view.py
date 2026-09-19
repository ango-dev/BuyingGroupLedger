"""The Reconciliation page: every order the buying group paid MORE or LESS than it committed to.


What is compared: for each order, the SETTLED rows that carry an Expected Payout -- the promise
the sync recorded from BFMR's tracker (`expected_payout`) against the money that landed
(`payout_amount`, settled = Payout Date set or a paid / return status). Both figures were
prorated across the order's rows by Total Cost, so they are compared as ORDER TOTALS over the
same rows: per-row cent drift from the two prorations never counts as a difference, and a partly
settled order compares only the part that has settled. A row with no commitment (MOD publishes
none; a purchase BFMR never priced) is not a mismatch, it is simply not compared. The tolerance
is the sync's own: two cents, plus a cent per row for rounding.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from web.ledger_reader import LedgerRow

RowKey = tuple[str, str, str, str]


def _money(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def tolerance(rows: int) -> float:
    return max(0.02, 0.01 * rows)


@dataclass
class OrderRecon:
    order_id: str
    order_date: str
    retailer: str
    buying_group: str
    rows: int
    expected: float
    paid: float

    @property
    def difference(self) -> float:
        """Paid minus expected: negative when the group paid LESS than it committed to."""
        return round(self.paid - self.expected, 2)

    @property
    def kind(self) -> str:
        return "short" if self.difference < 0 else "over"

    @property
    def line(self) -> str:
        word = "short-paid" if self.kind == "short" else "over-paid"
        return (f"expected {_money(self.expected)}, paid {_money(self.paid)}: "
                f"{word} by {_money(abs(self.difference))}")


@dataclass
class ReconReport:
    orders: list[OrderRecon] = field(default_factory=list)
    #: How many orders were compared at all (settled rows with a commitment), matched or not.
    compared: int = 0

    def kind_of(self, order_id: str) -> str:
        """"short" | "over" | "" for an order the report knows (the tiles filter on it)."""
        for o in self.orders:
            if o.order_id == order_id:
                return o.kind
        return ""

    @property
    def keys(self) -> set[str]:
        return {o.order_id for o in self.orders}

    @property
    def short(self) -> list[OrderRecon]:
        return [o for o in self.orders if o.kind == "short"]

    @property
    def over(self) -> list[OrderRecon]:
        return [o for o in self.orders if o.kind == "over"]

    @property
    def short_total(self) -> float:
        return round(sum(-o.difference for o in self.short), 2)

    @property
    def over_total(self) -> float:
        return round(sum(o.difference for o in self.over), 2)

    def by_order(self) -> dict[str, OrderRecon]:
        return {o.order_id: o for o in self.orders}


def row_line(row: LedgerRow, order: OrderRecon) -> str:
    """This row's own two figures, then the order's verdict."""
    expected = row.expected_payout or 0.0
    paid = row.payout_amount or 0.0
    word = "short-paid" if order.kind == "short" else "over-paid"
    return (f"expected {_money(expected)}, paid {_money(paid)} on this row; the order is {word} "
            f"by {_money(abs(order.difference))}")


def reconcile(rows: list[LedgerRow]) -> ReconReport:
    """The orders whose settled payout disagrees with their commitment, biggest gap first."""
    buckets: dict[str, dict] = {}
    for row in rows:
        if row.is_money_free or not row.is_settled or row.expected_payout is None:
            continue
        bucket = buckets.get(row.order_id)
        if bucket is None:
            bucket = buckets[row.order_id] = {
                "order_id": row.order_id, "order_date": row.order_date, "retailer": row.retailer,
                "buying_group": row.buying_group, "rows": 0, "expected": 0.0, "paid": 0.0,
            }
        bucket["rows"] += 1
        bucket["expected"] += row.expected_payout
        bucket["paid"] += row.payout_amount or 0.0
    report = ReconReport(compared=len(buckets))
    for bucket in buckets.values():
        order = OrderRecon(**{**bucket, "expected": round(bucket["expected"], 2),
                              "paid": round(bucket["paid"], 2)})
        if abs(order.difference) > tolerance(order.rows):
            report.orders.append(order)
    report.orders.sort(key=lambda o: (-abs(o.difference), o.order_id))
    return report


def findings_for(rows: list[LedgerRow], report: ReconReport) -> dict[RowKey, list[tuple[str, str]]]:
    """(label, line) per affected settled row, keyed like the audit's findings so the Orders
    partials render both the same way."""
    orders = report.by_order()
    out: dict[RowKey, list[tuple[str, str]]] = {}
    for row in rows:
        order = orders.get(row.order_id)
        if order is None or row.is_money_free or not row.is_settled or row.expected_payout is None:
            continue
        key = (row.order_id, row.order_date, row.item_name, row.shipment)
        out[key] = [(order.kind, row_line(row, order))]
    return out
