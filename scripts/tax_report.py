"""A quick cash-basis tax report for one year, read straight off the ledger. WRITES NOTHING.

    python -m scripts.tax_report                 # asks which year
    python -m scripts.tax_report 2026
    python -m scripts.tax_report 2026 --json
    python -m scripts.tax_report 2026 --from-snapshot before.json   # offline, from an audit snapshot

TWO DATES DRIVE THE YEAR, and that is the whole reason this is a script rather than a column. On a
cash basis, money IN counts when it arrives and money OUT counts when it is spent:

  - receipts    = Actual Payout, on rows whose **Payout Date** falls in the year
  - COGS        = the COGS cell, on rows whose **Order Date** falls in the year (cancelled and
                  superseded rows carry no money and are excluded)
  - insurance   = Insurance, on rows whose **Order Date** falls in the year -- the premium is charged
                  at filing, which happens at ship time; the sheet keeps no filing date, and an order
                  ships within days of being placed, so Order Date is the honest proxy

So a December order paid in January is a cost in one year and income in the next -- and the report
says how much of each year's money is "straddling" like that, because that is the number a preparer
asks about. Insurance stays out of COGS on purpose: a buying-group premium is an ordinary expense
(Schedule C Part II), not a cost of the goods.

A BOUGHT gift card (Buying Group `Gift Card`) is a real cost that never gets a payout of its own --
the income arrives through the orders it funds, whose Gift Card cell netted their COGS -- so it is
counted in COGS but reported on its own line, NOT as money still owed (the audit's
cogs_inputs_complete draws the same distinction).

Reads the sheet through scripts/audit_ledger's READ-ONLY path (read-only OAuth scope), so it cannot
write even by accident. Cashback is already netted into COGS by the sheet formula; the report shows
the gross cost and the cashback separately as well, so the netting is visible rather than implied.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, timedelta

from config.warehouses import is_deliberately_unrouted
from models.order import MONEY_FREE_STATUSES
from scripts.audit_ledger import (
    Grids,
    Sheet,
    _parse_display_number,
    _snapshot_path,
    open_ledger_readonly,
    read_grids,
)

_SHEETS_EPOCH = date(1899, 12, 30)


def _year_of(value) -> int | None:
    """The year of a date cell: ISO text, or a Sheets serial if the column was ever typed as a Date."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return (_SHEETS_EPOCH + timedelta(days=int(value))).year
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10]).year
    except ValueError:
        return None


def _money(value) -> float:
    parsed = _parse_display_number(value)
    return float(parsed) if parsed is not None else 0.0


def _blank_bucket() -> dict:
    return {"rows": 0, "orders": set(), "gross_cost": 0.0, "shipping": 0.0, "returns": 0.0,
            "gift_card": 0.0, "sales_tax": 0.0, "rewards_used": 0.0, "cashback": 0.0,
            "cogs": 0.0, "insurance": 0.0, "payouts": 0.0, "payout_rows": 0}


def build_report(sheet: Sheet, year: int) -> dict:
    """Everything the report needs, as plain data. Pure: no I/O."""
    f, u = sheet.grids.formatted, sheet.grids.unformatted
    total = _blank_bucket()
    by_retailer: dict[str, dict] = defaultdict(_blank_bucket)
    by_group: dict[str, dict] = defaultdict(_blank_bucket)
    cost_rows: list[dict] = []
    payout_rows: list[dict] = []
    unpaid_cost = {"rows": 0, "cogs": 0.0}
    funding_rows = {"rows": 0, "cogs": 0.0}  # bought gift cards: cost, never a payout
    paid_from_other_years = {"rows": 0, "payouts": 0.0}
    paid_in_later_year = {"rows": 0, "payouts": 0.0}

    for row_number, _ in sheet.ledger_rows(f):
        cell = lambda name, grid=u: sheet.cell(grid, row_number, name)  # noqa: E731
        status = str(cell("Status", f)).strip().lower()
        order_year = _year_of(cell("Order Date"))
        payout_year = _year_of(cell("Payout Date"))
        payout = _money(cell("Actual Payout"))
        retailer = str(cell("Retailer", f)).strip() or "(no retailer)"
        group = str(cell("Buying Group", f)).strip() or "(untagged)"
        order_id = str(cell("Order ID", f)).strip()

        cost_side = order_year == year and status not in MONEY_FREE_STATUSES
        income_side = payout_year == year and payout

        if cost_side:
            total_cost = _money(cell("Total Cost"))
            shipping = _money(cell("Shipping"))
            rate = _money(cell("Cashback Rate"))
            # The cost basis, mirroring the sheet's own COGS formula term for term (Total Cost and
            # Quantity are GROSS; returns/gift card/tax are netted here, not in the stored cells).
            returns = _money(cell("Return Qty")) * _money(cell("Cost Per Item"))
            gift_card = _money(cell("Gift Card"))
            sales_tax = _money(cell("Sales Tax"))
            # Rewards spent stay IN the cost (netted from COGS at year end outside the sheet) and
            # only leave the cashback basis — the same shape as the sheet's formula.
            rewards_used = _money(cell("Rewards Used"))
            basis = total_cost - returns - gift_card + shipping + sales_tax - rewards_used
            cogs_cell = _parse_display_number(cell("COGS"))
            cogs = float(cogs_cell) if cogs_cell is not None else basis * (1 - rate) + rewards_used
            cashback = basis - (cogs - rewards_used)
            insurance = _money(cell("Insurance"))
            for bucket in (total, by_retailer[retailer], by_group[group]):
                bucket["rows"] += 1
                bucket["orders"].add(order_id)
                bucket["gross_cost"] += total_cost
                bucket["shipping"] += shipping
                bucket["returns"] += returns
                bucket["gift_card"] += gift_card
                bucket["sales_tax"] += sales_tax
                bucket["rewards_used"] += rewards_used
                bucket["cashback"] += cashback
                bucket["cogs"] += cogs
                bucket["insurance"] += insurance
            cost_rows.append({
                "row": row_number, "order_date": str(cell("Order Date", f)).strip(),
                "retailer": retailer, "order_id": order_id,
                "item": str(cell("Item Name", f)).strip(), "quantity": str(cell("Quantity", f)).strip(),
                "status": status, "cogs": round(cogs, 2), "insurance": round(insurance, 2),
                "payout_date": str(cell("Payout Date", f)).strip(), "payout": round(payout, 2),
            })
            if is_deliberately_unrouted(group):
                funding_rows["rows"] += 1
                funding_rows["cogs"] += cogs
            elif payout_year is None or not payout:
                unpaid_cost["rows"] += 1
                unpaid_cost["cogs"] += cogs
            elif payout_year > year:
                paid_in_later_year["rows"] += 1
                paid_in_later_year["payouts"] += payout

        if income_side:
            for bucket in (total, by_retailer[retailer], by_group[group]):
                bucket["payouts"] += payout
                bucket["payout_rows"] += 1
            payout_rows.append({
                "row": row_number, "payout_date": str(cell("Payout Date", f)).strip(),
                "retailer": retailer, "order_id": order_id, "order_date": str(cell("Order Date", f)).strip(),
                "payout": round(payout, 2),
            })
            if order_year is not None and order_year != year:
                paid_from_other_years["rows"] += 1
                paid_from_other_years["payouts"] += payout

    def finish(bucket: dict) -> dict:
        out = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in bucket.items() if k != "orders"}
        out["orders"] = len(bucket["orders"])
        out["net"] = round(bucket["payouts"] - bucket["cogs"] - bucket["insurance"], 2)
        return out

    cost_rows.sort(key=lambda r: (r["order_date"], r["order_id"]))
    payout_rows.sort(key=lambda r: (r["payout_date"], r["order_id"]))
    return {
        "year": year,
        "basis": "cash: receipts by Payout Date; COGS and insurance by Order Date; cancelled and superseded excluded",
        "totals": finish(total),
        "by_retailer": {k: finish(v) for k, v in sorted(by_retailer.items())},
        "by_buying_group": {k: finish(v) for k, v in sorted(by_group.items())},
        "straddling": {
            "ordered_this_year_not_yet_paid": {**unpaid_cost, "cogs": round(unpaid_cost["cogs"], 2)},
            "ordered_this_year_paid_in_a_later_year": {**paid_in_later_year, "payouts": round(paid_in_later_year["payouts"], 2)},
            "paid_this_year_for_orders_from_other_years": {**paid_from_other_years, "payouts": round(paid_from_other_years["payouts"], 2)},
            "gift_card_purchases_never_paid": {**funding_rows, "cogs": round(funding_rows["cogs"], 2)},
        },
        "cost_rows": cost_rows,
        "payout_rows": payout_rows,
    }


def _fmt(n: float) -> str:
    return f"({abs(n):,.2f})" if n < 0 else f"{n:,.2f}"


def render_text(report: dict, *, list_rows: bool = True) -> str:
    t = report["totals"]
    out = [
        f"Tax report -- {report['year']} (cash basis)",
        f"  {report['basis']}",
        "",
        f"  Receipts (payouts dated {report['year']})        {_fmt(t['payouts']):>14}   {t['payout_rows']} row(s)",
        f"  COGS (orders dated {report['year']})             {_fmt(-t['cogs']):>14}   {t['rows']} row(s), {t['orders']} order(s)",
        f"      gross cost + shipping                  {_fmt(t['gross_cost'] + t['shipping']):>14}",
        *([f"      less returned units                    {_fmt(-t['returns']):>14}"] if t["returns"] else []),
        *([f"      less gift-card tenders                 {_fmt(-t['gift_card']):>14}"] if t["gift_card"] else []),
        *([f"      plus sales tax                         {_fmt(t['sales_tax']):>14}"] if t["sales_tax"] else []),
        f"      less cashback netted into cost         {_fmt(-t['cashback']):>14}",
        *([f"      (of which paid with Amazon rewards     {_fmt(t['rewards_used']):>14}   kept in cost; "
           "net ALL Amazon rewards from COGS at year end)"] if t["rewards_used"] else []),
        f"  Insurance (Schedule C expense)            {_fmt(-t['insurance']):>14}",
        f"  {'-' * 56}",
        f"  Net                                       {_fmt(t['net']):>14}",
        "",
    ]
    s = report["straddling"]
    out += [
        "  Straddling the year boundary:",
        f"    ordered {report['year']}, not yet paid out:        {s['ordered_this_year_not_yet_paid']['rows']} row(s), COGS {_fmt(s['ordered_this_year_not_yet_paid']['cogs'])}",
        f"    ordered {report['year']}, paid in a later year:    {s['ordered_this_year_paid_in_a_later_year']['rows']} row(s), payouts {_fmt(s['ordered_this_year_paid_in_a_later_year']['payouts'])}",
        f"    paid {report['year']}, ordered in another year:    {s['paid_this_year_for_orders_from_other_years']['rows']} row(s), payouts {_fmt(s['paid_this_year_for_orders_from_other_years']['payouts'])}",
        *([f"    gift cards bought (income arrives via the orders they fund): {s['gift_card_purchases_never_paid']['rows']} row(s), COGS {_fmt(s['gift_card_purchases_never_paid']['cogs'])}"]
          if s["gift_card_purchases_never_paid"]["rows"] else []),
        "",
    ]
    for title, table in (("By retailer", report["by_retailer"]), ("By buying group", report["by_buying_group"])):
        out.append(f"  {title}:")
        out.append(f"    {'':<20}{'orders':>7}{'COGS':>14}{'insurance':>12}{'payouts':>14}{'net':>14}")
        for name, b in table.items():
            out.append(f"    {name[:20]:<20}{b['orders']:>7}{_fmt(b['cogs']):>14}{_fmt(b['insurance']):>12}{_fmt(b['payouts']):>14}{_fmt(b['net']):>14}")
        out.append("")
    if list_rows:
        out.append(f"  Orders dated {report['year']} ({len(report['cost_rows'])} row(s)):")
        out.append(f"    {'order date':<11}{'retailer':<16}{'order id':<22}{'qty':>4}{'COGS':>12}{'paid':<12}{'payout':>12}  item")
        for r in report["cost_rows"]:
            out.append(f"    {r['order_date']:<11}{r['retailer'][:15]:<16}{r['order_id'][:21]:<22}{r['quantity']:>4}"
                       f"{_fmt(r['cogs']):>12}  {r['payout_date'] or '-':<10}{_fmt(r['payout']):>12}  {r['item'][:40]}")
        out.append("")
    return "\n".join(out)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Cash-basis tax report for one year, read-only.")
    parser.add_argument("year", nargs="?", type=int, help="the tax year (asked for if omitted)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--no-rows", action="store_true", help="totals and breakdowns only")
    parser.add_argument("--from-snapshot", metavar="PATH",
                        help="read an audit_ledger --save-snapshot file instead of the live sheet")
    args = parser.parse_args(argv)

    year = args.year
    while year is None:
        typed = input("Tax year (e.g. 2026): ").strip()
        if typed.isdigit() and 2000 <= int(typed) <= 2100:
            year = int(typed)
        else:
            print("  a four-digit year, please", file=sys.stderr)

    if args.from_snapshot:
        with open(_snapshot_path(args.from_snapshot), encoding="utf-8") as fh:
            grids = Grids.from_snapshot(json.load(fh))
    else:
        worksheet, title = open_ledger_readonly()
        grids = read_grids(worksheet, title)
    if not grids.formatted:
        raise SystemExit("The worksheet is empty.")

    report = build_report(Sheet(grids), year)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_text(report, list_rows=not args.no_rows))


if __name__ == "__main__":
    main()
