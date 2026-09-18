"""The overview page's numbers, derived from a Snapshot. Pure functions, no I/O."""

from __future__ import annotations

import calendar
import re
from collections import Counter, defaultdict
from datetime import date

from models.order import STATUSES

from web.ledger_reader import LedgerRow, Snapshot

#: How many order ids a gap list names before it says "and N more".
GAP_SAMPLE = 12
_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _status_order(statuses) -> list[str]:
    """STATUSES' own order first, then anything out of vocabulary (the audit's column_shape fails
    those; the dashboard still shows them rather than hiding a row)."""
    known = [s for s in STATUSES if s in statuses]
    unknown = sorted(s for s in statuses if s not in STATUSES)
    return known + unknown


def _money_block(rows: list[LedgerRow]) -> dict:
    payout = sum(r.payout_amount or 0.0 for r in rows)
    profit = sum(r.profit or 0.0 for r in rows if r.profit is not None)
    cogs = sum(r.cogs or 0.0 for r in rows if r.cogs is not None)
    orders = {r.order_id for r in rows}
    return {"rows": len(rows), "orders": len(orders), "payout": round(payout, 2),
            "cogs": round(cogs, 2), "profit": round(profit, 2)}


def _gap(rows: list[LedgerRow]) -> dict:
    ids: list[str] = []
    for row in rows:
        if row.order_id not in ids:
            ids.append(row.order_id)
    return {"rows": len(rows), "orders": len(ids), "sample": ids[:GAP_SAMPLE],
            "more": max(0, len(ids) - GAP_SAMPLE)}


def _tile(label: str, value, kind: str, hint: str, href: str, tone: str = "",
          detail: str = "") -> dict:
    """`hint` is the few words under the number (it is an overview); `detail` is the full
    definition, shown as the tile's tooltip."""
    return {"label": label, "value": value, "kind": kind, "hint": hint, "href": href,
            "tone": tone, "detail": detail}


def _orders_link(**params) -> str:
    """A link to the Orders page with these filters, "" values dropped (see queries.Filters)."""
    from web.queries import query_string

    query = query_string({k: v for k, v in params.items() if v})
    return "/orders?" + query if query else "/orders"


def _spend(rows: list[LedgerRow]) -> float:
    return round(sum(r.total_cost or 0.0 for r in rows if not r.is_money_free), 2)


def actual_return(rows: list[LedgerRow]) -> tuple[float | None, int]:
    """What a dollar spent ACTUALLY came back as, after everything:

        sum(Payout Amount - COGS - Insurance) / sum(Total Cost)  =  sum(Total Profit) / sum(Total Cost)

    over the SETTLED rows (the payout is in; a committed one is a promise). Cost-weighted by
    construction: dollars over dollars, so a $2,000 order pulls harder than a $300 one. COGS is
    the sheet's own formula, so the cashback on shipping and tax, the gift card and rewards
    netting and a return's share are all already in it; insurance is the BFMR premium; the payout
    is what the group paid after its commission. Returns (rate as a fraction or None, rows)."""
    counted = [r for r in rows if r.is_settled and r.total_cost and r.profit is not None]
    cost = sum(r.total_cost for r in counted)
    if not cost:
        return None, 0
    return round(sum(r.profit for r in counted) / cost, 4), len(counted)


def period_tiles(placed: list[LedgerRow], paid: list[LedgerRow], scope: str,
                 link) -> list[dict]:
    """The SAME eight tiles for any period. `placed` are the period's rows (all of them for
    Lifetime; by Order Date for a month), `paid` the settled ones among them; `scope` is the
    phrase the hints end with ("of the ledger" / "placed in September 2026"); `link(**filters)`
    builds the tile's Orders-page href for that period."""
    open_rows = [r for r in placed if r.is_open]
    unpaid = [r for r in placed if r.is_unpaid]
    rate, rated = actual_return(paid)
    projected = _money_block([r for r in placed if r.is_committed])
    realized = _money_block(paid)
    return [
        _tile("Rows / orders", (len(placed), len({r.order_id for r in placed})), "pair",
              "all rows", link(), detail=f"every row {scope}"),
        _tile("Open rows", len(open_rows), "count", "not paid yet", link(state="open"),
              detail=f"rows {scope} still ordered, shipped or delivered: the buying group has "
                     "not paid yet"),
        _tile("Spend", _spend(placed), "money", "Total Cost", link(sort="total_cost", dir="desc"),
              detail=f"Total Cost over every row {scope} that carries money (cancelled / "
                     "superseded excluded)"),
        _tile("Actual return", rate, "percent", f"{rated} settled rows",
              link(state="settled", sort="total_profit", dir="desc"),
              detail=f"(Payout \u2212 COGS \u2212 Insurance) / Total Cost over the {rated} settled "
                     f"row(s) {scope}: cashback after shipping, tax, gift cards and rewards, less "
                     "insurance, against what the group actually paid; cost-weighted"),
        _tile("Paid out", realized["payout"], "money",
              f"{realized['rows']} settled rows", link(state="settled"), tone="settled",
              detail=f"{realized['rows']} settled row(s) in {realized['orders']} order(s) "
                     f"{scope}: Payout Date set, or status paid / return"),
        _tile("Floating", _spend(unpaid), "money", "payout not received", link(state="unpaid"),
              tone="floating",
              detail=f"Total Cost of the {len(unpaid)} row(s) in "
                     f"{len({r.order_id for r in unpaid})} order(s) {scope} the buying group has "
                     "not paid yet (no settled payout; gift cards excluded)"),
        _tile("Projected profit", projected["profit"], "money",
              f"{projected['rows']} committed rows", link(state="committed"), tone="committed",
              detail=f"{projected['rows']} row(s), {projected['orders']} order(s) {scope} with a "
                     "committed payout and no Payout Date"),
        _tile("Realized profit", realized["profit"], "money",
              f"{realized['rows']} settled rows", link(state="settled"), tone="settled",
              detail=f"Total Profit of the {realized['rows']} settled row(s), "
                     f"{realized['orders']} order(s) {scope}"),
    ]


def lifetime_tiles(rows: list[LedgerRow]) -> list[dict]:
    """Every row the ledger holds, as clickable tiles (each opens the Orders page filtered the
    same way the number was counted)."""

    return period_tiles(rows, [r for r in rows if r.is_settled], "of the ledger", _orders_link)


def month_label(month: str) -> str:
    """"2026-09" -> "September 2026"."""
    year, number = month.split("-")
    return f"{calendar.month_name[int(number)]} {year}"


def shift_month(month: str, delta: int) -> str:
    year, number = (int(part) for part in month.split("-"))
    index = year * 12 + (number - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def month_section(rows: list[LedgerRow], month: str, today_month: str) -> dict:
    """One calendar month: the rows whose ORDER DATE falls in it, and nothing else. Paid out /
    realized are the settled rows among them, whenever the payout landed; the cash-basis view by
    Payout Date is the Orders page's "Paid in" filter and the tax report."""
    placed = [r for r in rows if r.order_date.startswith(month)]
    paid = [r for r in placed if r.is_settled]

    def link(**filters) -> str:
        return _orders_link(month=month, **filters)

    tiles = period_tiles(placed, paid, f"placed in {month_label(month)}", link)
    dated = sorted({r.order_date[:7] for r in rows if len(r.order_date) >= 7})
    first = min(dated[0], today_month) if dated else today_month
    return {
        "month": month, "label": month_label(month), "tiles": tiles,
        "prev": shift_month(month, -1) if month > first else "",
        "next": shift_month(month, 1) if month < today_month else "",
        "current": month == today_month, "today": today_month,
    }


def overview(snapshot: Snapshot, month: str = "", today: date | None = None) -> dict:
    """`month` is the calendar month the month section shows (YYYY-MM; blank = the current one,
    from `today`, which the app takes from its clock)."""
    rows = snapshot.rows
    open_rows = [r for r in rows if r.is_open]
    today_month = (today or date.today()).strftime("%Y-%m")
    month = month if _MONTH.match(month or "") else today_month

    # Open rows by status x buying group. Blank group is shown as such, never folded into another.
    groups = sorted({r.buying_group or "(blank)" for r in open_rows})
    matrix: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in open_rows:
        matrix[r.status][r.buying_group or "(blank)"] += 1
    statuses = _status_order(matrix.keys())
    open_table = {
        "groups": groups,
        "rows": [
            {"status": s, "cells": [matrix[s][g] for g in groups], "total": sum(matrix[s].values())}
            for s in statuses
        ],
        "column_totals": [sum(matrix[s][g] for s in statuses) for g in groups],
        "total": len(open_rows),
    }

    # Every row by status, so the terminal side is visible too.
    counts = Counter(r.status for r in rows)
    status_counts = [(s, counts[s]) for s in _status_order(counts.keys())]

    # Projected = committed payouts (amount, no date, no buying-group outcome); realized = settled
    # ($0.00 settlements included: a return that paid nothing is a real loss).
    projected = _money_block([r for r in rows if r.is_committed])
    realized = _money_block([r for r in rows if r.is_settled])
    # What SUM() over the sheet's Total Profit column gives: every row whose formula shows a
    # number -- realized + projected + anything else with a payout cell. Shown so the page can be
    # reconciled against the sheet at a glance.
    column = [r for r in rows if r.profit is not None]
    column_sum = {"rows": len(column), "profit": round(sum(r.profit for r in column), 2),
                  "other": round(sum(r.profit for r in column if not r.is_settled
                                     and not r.is_committed), 2)}

    # The audit's cogs_inputs_complete gap: a row carrying cost whose COGS cannot net a rebate.
    costed = [r for r in rows if not r.is_money_free and r.total_cost is not None]
    no_card = _gap([r for r in costed if not r.text("card_last4")])
    no_rate = _gap([r for r in costed if r.number("cashback_rate") is None])

    by_retailer = Counter(r.retailer or "(blank)" for r in rows)
    by_group = Counter(r.buying_group or "(blank)" for r in rows)

    # The three "rows by ..." breakdowns as donuts, every slice a filter link.
    # Retailer / group slices are drawn largest first; a name keeps its colour slot by that order
    # within this snapshot (the categorical rule: fixed order, never re-cycled mid-page).
    from web.charts import donut, open_rows_bars, status_donut

    donuts = [
        status_donut(status_counts),
        donut("Rows by Retailer", by_retailer.most_common(), param="retailer"),
        donut("Rows by Buying Group", by_group.most_common(), param="group"),
    ]

    return {
        # The two stat sections.
        "lifetime": lifetime_tiles(rows),
        "month": month_section(rows, month, today_month),
        "donuts": donuts,
        # The open-rows matrix as stacked bars, drawn in the same row as the donuts.
        "open_bars": open_rows_bars(open_table),
        "rows": len(rows),
        "orders": len({r.order_id for r in rows}),
        "open_rows": len(open_rows),
        "open_table": open_table,
        "status_counts": status_counts,
        "projected": projected,
        "realized": realized,
        "column_sum": column_sum,
        "gaps": {"card_last4": no_card, "cashback_rate": no_rate},
        "by_retailer": sorted(by_retailer.items()),
        "by_group": sorted(by_group.items()),
    }
