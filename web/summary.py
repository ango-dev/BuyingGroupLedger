"""The overview page's numbers, derived from a Snapshot. Pure functions, no I/O."""

from __future__ import annotations

import calendar
import re
from collections import Counter, defaultdict
from datetime import date

from models.order import STATUSES

from web.ledger_reader import LedgerRow, Snapshot

#: How many order ids a gap list names before it says "and N more".
_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _status_order(statuses) -> list[str]:
    """STATUSES' own order first, then anything out of vocabulary (the audit's column_shape fails
    those; the dashboard still shows them rather than hiding a row)."""
    known = [s for s in STATUSES if s in statuses]
    unknown = sorted(s for s in statuses if s not in STATUSES)
    return known + unknown


def _projected_block(rows: list[LedgerRow]) -> dict:
    """The committed rows' money: the commitment as the payout, the projected profit as profit."""
    payout = sum(r.projected_payout or 0.0 for r in rows)
    profit = sum(r.projected_profit or 0.0 for r in rows if r.projected_profit is not None)
    cogs = sum(r.cogs or 0.0 for r in rows if r.cogs is not None)
    orders = {r.order_id for r in rows}
    return {"rows": len(rows), "orders": len(orders), "payout": round(payout, 2),
            "cogs": round(cogs, 2), "profit": round(profit, 2)}


def _money_block(rows: list[LedgerRow]) -> dict:
    payout = sum(r.payout_amount or 0.0 for r in rows)
    profit = sum(r.profit or 0.0 for r in rows if r.profit is not None)
    cogs = sum(r.cogs or 0.0 for r in rows if r.cogs is not None)
    orders = {r.order_id for r in rows}
    return {"rows": len(rows), "orders": len(orders), "payout": round(payout, 2),
            "cogs": round(cogs, 2), "profit": round(profit, 2)}


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

        sum(Actual Payout - COGS - Insurance) / sum(Total Cost)  =  sum(Total Profit) / sum(Total Cost)

    over the SETTLED rows (the payout is in; a committed one is a promise). Cost-weighted by
    construction: dollars over dollars, so a $2,000 order pulls harder than a $300 one. COGS is
    the ledger's own formula, so the cashback on shipping and tax, the gift card and rewards
    netting and a return's share are all already in it; insurance is the BFMR premium; the payout
    is what the group paid after its commission. Returns (rate as a fraction or None, rows)."""
    counted = [r for r in rows if r.is_settled and r.total_cost and r.profit is not None]
    cost = sum(r.total_cost for r in counted)
    if not cost:
        return None, 0
    return round(sum(r.profit for r in counted) / cost, 4), len(counted)


def extras_lifetime(inputs_by_year: dict) -> dict:
    """The money the Taxes page holds that the ledger's rows do not: every year's sign-up bonuses, program
    cashback, cashback-site payouts and other income as `income`, every year's expenses (the
    list, plus any expense-kind "other" row) as `expenses`, and the parts for the tooltip."""
    parts = {"bonuses": 0.0, "programs": 0.0, "sites": 0.0, "other": 0.0}
    expenses = 0.0
    for inputs in (inputs_by_year or {}).values():
        parts["bonuses"] += inputs.bonus_total
        parts["programs"] += inputs.program_total
        parts["sites"] += inputs.site_total
        parts["other"] += inputs.other_income
        expenses += inputs.expense_total + inputs.other_expense
    parts = {k: round(v, 2) for k, v in parts.items()}
    return {"income": round(sum(parts.values()), 2), "expenses": round(expenses, 2), "parts": parts,
            "years": sorted(inputs_by_year or {})}


def expenses_in_month(inputs_by_year: dict, month: str) -> float:
    """The Taxes page's expenses dated in the month (an expense carries its date; the yearly
    lump sums do not, so they belong to Lifetime alone)."""
    total = 0.0
    for inputs in (inputs_by_year or {}).values():
        total += sum(float(e.get("amount") or 0) for e in inputs.expenses
                     if str(e.get("date") or "").startswith(month))
    return round(total, 2)


def period_tiles(placed: list[LedgerRow], paid: list[LedgerRow], scope: str,
                 link, *, extra_income: dict | None = None, expenses: float = 0.0,
                 expenses_scope: str = "", income_scope: str = "every year") -> list[dict]:
    """The SAME tiles for any period. `placed` are the period's rows (all of them for Lifetime; by
    Order Date for a month), `paid` the settled ones among them; `scope` is the phrase the hints
    end with ("of the ledger" / "placed in September 2026"); `link(**filters)` builds the tile's
    Orders-page href for that period. `extra_income` (extras_lifetime) adds the Taxes page's
    income -- Lifetime only, since it is entered per year -- and `expenses` its expenses for the
    period; Net profit is realized profit plus that income less those expenses."""
    open_rows = [r for r in placed if r.is_open]
    unpaid = [r for r in placed if r.is_unpaid]
    rate, rated = actual_return(paid)
    projected = _projected_block([r for r in placed if r.is_committed])
    realized = _money_block(paid)
    income = extra_income["income"] if extra_income else 0.0
    net = round(realized["profit"] + income - expenses, 2)
    if extra_income:
        p = extra_income["parts"]
        income_detail = (f"the Taxes page's income, {income_scope}: sign-up bonuses {p['bonuses']:,.2f}, "
                         f"program cashback {p['programs']:,.2f}, cashback sites {p['sites']:,.2f}, "
                         f"other income {p['other']:,.2f}")
        net_detail = (f"realized profit {realized['profit']:,.2f} + other income {income:,.2f} "
                      f"\u2212 expenses {expenses:,.2f}: what the business actually made, the "
                      f"Taxes page's entries ({income_scope}) included")
    else:
        net_detail = (f"realized profit {realized['profit']:,.2f} \u2212 expenses dated in the month "
                      f"{expenses:,.2f}; sign-up bonuses, program cashback and cashback sites are "
                      "entered per year and count under Lifetime")
    tiles = [
        _tile("Rows / orders", (len(placed), len({r.order_id for r in placed})), "pair",
              "all rows", link(), detail=f"every row {scope}"),
        _tile("Open rows", len(open_rows), "count", "not paid yet", link(state="open"),
              detail=f"rows {scope} still ordered, shipped or delivered: the buying group has "
                     "not paid yet"),
        _tile("Spend", _spend(placed), "money", "Total Cost", link(sort="total_cost", dir="desc"),
              detail=f"Total Cost over every row {scope} that carries money (cancelled / "
                     "superseded excluded)"),
        # Renamed from "Actual return".
        _tile("Cashback rate", rate, "percent", f"weighted, {rated} settled rows",
              link(state="settled", sort="total_profit", dir="desc"),
              detail=f"the average cashback rate per settled order, weighted by cost: (Payout "
                     f"\u2212 COGS \u2212 Insurance) / Total Cost over the {rated} settled row(s) "
                     f"{scope} -- cashback after shipping, tax, gift cards and rewards, less "
                     "insurance, against what the group actually paid"),
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
              detail=f"{projected['rows']} row(s), {projected['orders']} order(s) {scope} with an "
                     "Expected Payout the buying group has not paid yet"),
        _tile("Realized profit", realized["profit"], "money",
              f"{realized['rows']} settled rows", link(state="settled"), tone="settled",
              detail=f"Total Profit of the {realized['rows']} settled row(s), "
                     f"{realized['orders']} order(s) {scope}"),
    ]
    if extra_income:
        tiles.append(_tile("Other income", income, "money", "bonuses, cashback, other", "/taxes",
                           tone="settled", detail=income_detail))
    tiles.append(_tile("Expenses", expenses, "money", expenses_scope or "Taxes page", "/taxes",
                       tone="floating",
                       detail=(f"the Taxes page's expenses, {expenses_scope or 'every year'}"
                               " -- supplies, memberships, card annual fees")))
    tiles.append(_tile("Net profit", net, "money", "income in, expenses out", "/taxes",
                       tone="settled", detail=net_detail))
    return tiles


def lifetime_tiles(rows: list[LedgerRow], inputs_by_year: dict | None = None) -> list[dict]:
    """Every row the ledger holds, as clickable tiles (each opens the Orders page filtered the
    same way the number was counted), plus the Taxes page's income and expenses, every year."""
    extras = extras_lifetime(inputs_by_year or {})
    return period_tiles(rows, [r for r in rows if r.is_settled], "of the ledger", _orders_link,
                        extra_income=extras, expenses=extras["expenses"], expenses_scope="every year")


def year_tiles(rows: list[LedgerRow], year: str, inputs_by_year: dict | None = None) -> list[dict]:
    """One calendar year by Order Date, with that year's Taxes-page income and expenses -- the
    period the Taxes page and the yearly lump sums are keyed on."""
    placed = [r for r in rows if r.order_date.startswith(year)]
    inputs = (inputs_by_year or {}).get(int(year))
    extras = extras_lifetime({int(year): inputs} if inputs else {})

    def link(**filters) -> str:
        return _orders_link(month=year, **filters)  # "Placed in" takes a bare year

    return period_tiles(placed, [r for r in placed if r.is_settled], f"placed in {year}", link,
                        extra_income=extras, expenses=extras["expenses"], expenses_scope=f"in {year}",
                        income_scope=f"in {year}")


#: The Profit & Loss statement's rows, top to bottom: Net profit first, its three parts indented
#: under it ("sub"), then the rates and the counts.
STATEMENT_ROWS: tuple[tuple[str, str], ...] = (
    ("Net profit", "net"), ("Realized profit", "sub"), ("Other income", "sub"), ("Expenses", "sub"),
    ("Cashback rate", ""), ("Paid out", ""), ("Floating", ""), ("Projected profit", ""),
    ("Spend", ""), ("Rows / orders", ""), ("Open rows", ""),
)


def statement(columns: list[tuple[str, str, list[dict]]]) -> dict:
    """The tiles of several periods as one table: `columns` are (key, heading, tiles); a row is
    a tile label, its cells the matching tile per column (None where a period has no such
    figure -- the month has no Other income, which is entered per year)."""
    rows = []
    for label, level in STATEMENT_ROWS:
        cells = [next((t for t in tiles if t["label"] == label), None) for _key, _heading, tiles in columns]
        rows.append({"label": label, "level": level, "cells": cells})
    return {"columns": [{"key": key, "label": heading} for key, heading, _tiles in columns], "rows": rows}


def monthly_series(rows: list[LedgerRow], inputs_by_year: dict | None, end_month: str,
                   count: int = 12) -> list[dict]:
    """`count` months ending at `end_month`, each by Order Date: realized profit (the settled
    rows placed that month), the Taxes page's expenses dated in it, and the net of the two."""
    out = []
    for i in range(count - 1, -1, -1):
        month = shift_month(end_month, -i)
        placed = [r for r in rows if r.order_date.startswith(month)]
        realized = _money_block([r for r in placed if r.is_settled])["profit"]
        expenses = expenses_in_month(inputs_by_year or {}, month)
        number = int(month[5:])
        label = calendar.month_abbr[number] + (f" '{month[2:4]}" if number == 1 or i == count - 1 else "")
        out.append({"month": month, "label": label, "realized": realized, "expenses": expenses,
                    "net": round(realized - expenses, 2), "href": f"/?month={month}"})
    return out


def _money_text(value: float) -> str:
    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


def _compact_money(value: float) -> str:
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 10_000:
        return f"{sign}${value / 1000:,.0f}k"
    if value >= 1_000:
        return f"{sign}${value / 1000:.1f}k"
    return f"{sign}${value:,.0f}"


def month_chart(series: list[dict], selected: str) -> dict:
    """SVG geometry for the 12-month chart: realized profit up from the baseline (down, in the
    bad colour, when negative), expenses down from it, the net over each column; the selected
    month outlined. All in view-box units; the template only draws."""
    width, height, top, bottom, gap = 720.0, 135.0, 20.0, 34.0, 8.0  # flat, so the page fits a screen; bottom: a loss label, then the month
    n = max(len(series), 1)
    slot = width / n
    w = slot - gap
    pos = max([max(s["realized"], 0.0) for s in series] + [0.0])
    neg = max([max(-s["realized"], 0.0) + s["expenses"] for s in series] + [0.0])
    span = height - top - bottom
    scale = span / (pos + neg) if pos + neg > 0 else 0.0
    base = top + pos * scale if scale else top + span / 2
    bars = []
    for i, s in enumerate(series):
        x = i * slot + gap / 2
        rh = abs(s["realized"]) * scale
        eh = s["expenses"] * scale
        if s["realized"] >= 0:
            ry, ey, peak = base - rh, base, base - rh
        else:
            ry, ey, peak = base, base + rh, base
        # The net's label: over the column, or under it when nothing stands above the baseline
        # (a month of expenses alone); none at all for an empty month.
        below = s["realized"] <= 0 and (rh or eh)
        net_y = min(ey + eh + 13, height - 22) if below else (peak - 6)  # never on the month name
        bars.append({
            "month": s["month"], "label": s["label"], "href": s["href"], "selected": s["month"] == selected,
            "realized": s["realized"], "expenses": s["expenses"], "net": s["net"],
            "net_label": _compact_money(s["net"]),
            "title": (f"{month_label(s['month'])}: realized profit {_money_text(s['realized'])}, "
                      f"expenses {_money_text(s['expenses'])}, net {_money_text(s['net'])}"),
            "x": round(x, 1), "w": round(w, 1), "cx": round(x + w / 2, 1),
            "realized_y": round(ry, 1), "realized_h": round(rh, 1),
            "expenses_y": round(ey, 1), "expenses_h": round(eh, 1),
            "net_y": round(net_y, 1), "labelled": bool(s["realized"] or s["expenses"]),
        })
    return {"width": width, "height": height, "base": round(base, 1), "bars": bars,
            "empty": not any(s["realized"] or s["expenses"] for s in series)}


def month_label(month: str) -> str:
    """"2026-09" -> "September 2026"."""
    year, number = month.split("-")
    return f"{calendar.month_name[int(number)]} {year}"


def shift_month(month: str, delta: int) -> str:
    year, number = (int(part) for part in month.split("-"))
    index = year * 12 + (number - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def month_section(rows: list[LedgerRow], month: str, today_month: str,
                  inputs_by_year: dict | None = None) -> dict:
    """One calendar month: the rows whose ORDER DATE falls in it, and nothing else. Paid out /
    realized are the settled rows among them, whenever the payout landed; the cash-basis view by
    Payout Date is the Orders page's "Paid in" filter and the tax report."""
    placed = [r for r in rows if r.order_date.startswith(month)]
    paid = [r for r in placed if r.is_settled]

    def link(**filters) -> str:
        return _orders_link(month=month, **filters)

    tiles = period_tiles(placed, paid, f"placed in {month_label(month)}", link,
                         expenses=expenses_in_month(inputs_by_year or {}, month), expenses_scope="dated in the month")
    dated = sorted({r.order_date[:7] for r in rows if len(r.order_date) >= 7})
    first = min(dated[0], today_month) if dated else today_month
    return {
        "month": month, "label": month_label(month), "tiles": tiles,
        "prev": shift_month(month, -1) if month > first else "",
        "next": shift_month(month, 1) if month < today_month else "",
        "current": month == today_month, "today": today_month,
    }


def overview(snapshot: Snapshot, month: str = "", today: date | None = None,
             inputs_by_year: dict | None = None) -> dict:
    """`month` is the calendar month the month section shows (YYYY-MM; blank = the current one,
    from `today`, which the app takes from its clock). `inputs_by_year` is the Taxes page's
    store (tax_inputs.load_all): its income and expenses join the tiles."""
    rows = snapshot.rows
    open_rows = [r for r in rows if r.is_open]
    today_month = (today or date.today()).strftime("%Y-%m")
    month = month if _MONTH.match(month or "") else today_month
    year = month[:4]  # the year column follows the month shown

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
    projected = _projected_block([r for r in rows if r.is_committed])
    realized = _money_block([r for r in rows if r.is_settled])
    # What SUM() over the ledger's Total Profit column gives: every row whose formula shows a
    # number -- realized + projected + anything else with a payout cell. Shown so the page can be
    # reconciled against the ledger at a glance.
    column = [r for r in rows if r.profit is not None]
    column_sum = {"rows": len(column), "profit": round(sum(r.profit for r in column), 2),
                  "other": round(sum(r.profit for r in column if not r.is_settled
                                     and not r.is_committed), 2)}

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
        "lifetime": lifetime_tiles(rows, inputs_by_year),
        "month": month_section(rows, month, today_month, inputs_by_year),
        "statement": statement([
            ("lifetime", "Lifetime", lifetime_tiles(rows, inputs_by_year)),
            ("year", year, year_tiles(rows, year, inputs_by_year)),
            ("month", month_label(month), month_section(rows, month, today_month, inputs_by_year)["tiles"]),
        ]),
        "months": month_chart(monthly_series(rows, inputs_by_year, month), month),
        "donuts": donuts,
        # The open-rows matrix as stacked bars, drawn in the same row as the donuts.
        # A narrow label column, so the bars start where a donut ring does in the card beside
        # them.
        "open_bars": open_rows_bars(open_table, label_width=46, bar_height=22, gap=8),
        "rows": len(rows),
        "orders": len({r.order_id for r in rows}),
        "open_rows": len(open_rows),
        "open_table": open_table,
        "status_counts": status_counts,
        "projected": projected,
        "realized": realized,
        "column_sum": column_sum,
        "by_retailer": sorted(by_retailer.items()),
        "by_group": sorted(by_group.items()),
    }
