"""Downloads (2026-09-21): the Orders page's rows-in-view CSV, and a tax year's bundle -- every
order, expense, income entry and receipt of the year in one organised zip, the binder an audit
would ask for.

Pure builders: the routes in web/app.py hand them the rows, the display formatter and callables
that resolve receipt files; nothing here reads the ledger or the settings itself. The orders CSVs
carry the page's DISPLAY text (what the row shows on screen: money with its sign and cents, dates
as ISO, the two derived columns computed), in the ledger's column order.
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from ledger.sync import HEADER
from models.order import FIELDNAMES

RECEIPT_PREFIX = "/receipts/"

#: Every CSV starts with a byte-order mark so Excel on Windows reads "Zürich" as written, and a
#: cell that a spreadsheet would run as a formula is quoted with a leading apostrophe (review
#: 2026-09-22, Orders bug 9: `=1+1` and `+cmd|' /C calc'!A0` were exported verbatim).
BOM = "\ufeff"
_FORMULA_STARTS = ("=", "@", "\t", "\r")
_DERIVED = ("cogs", "total_profit")


def guard(value) -> str:
    """The cell text a spreadsheet will show as TEXT, never run: a leading = @ tab or CR gets an
    apostrophe, and so does a leading + or - unless the whole cell is a number ("-$20.00" stays)."""
    text = "" if value is None else str(value)
    if not text:
        return text
    if text[0] in _FORMULA_STARTS:
        return "'" + text
    if text[0] in "+-":
        from models.numbers import NumberError, parse_number

        try:
            parse_number(text, percent=True)
        except NumberError:
            return "'" + text
    return text


class _Writer:
    """csv.writer over a StringIO that starts with the BOM and guards every cell."""

    def __init__(self):
        self.out = io.StringIO()
        self.out.write(BOM)
        self._writer = csv.writer(self.out, lineterminator="\n")

    def writerow(self, cells) -> None:
        self._writer.writerow([guard(c) for c in cells])

    def getvalue(self) -> str:
        return self.out.getvalue()


def _stored(row, name: str, cell: Callable) -> str:
    """A cell as the ledger STORES it -- 1259.99, 0.04, TRUE -- so the file round-trips through
    the importer;
    the two derived columns as plain numbers; anything without stored text as the page shows it."""
    if name in _DERIVED:
        value = getattr(row, "cogs" if name == "cogs" else "profit", None)
        return f"{value:.2f}" if isinstance(value, (int, float)) and not isinstance(value, bool) else ""
    text = getattr(row, "text", None)
    return text(name) if callable(text) else cell(row, name)


def orders_csv(rows: Iterable, cell: Callable, extra: tuple[str, Callable] | None = None) -> str:
    """The rows as CSV, one header row, each cell as stored (see `_stored`). `extra` = (heading,
    row -> text) appends one column: the Audit and Recon pages' Finding."""
    writer = _Writer()
    writer.writerow(HEADER + ([extra[0]] if extra else []))
    for row in rows:
        writer.writerow([_stored(row, name, cell) for name in FIELDNAMES] + ([extra[1](row)] if extra else []))
    return writer.getvalue()


def findings_text(findings: dict, key_of: Callable) -> Callable:
    """The Finding column's text for a row: every (check, line) the page shows, joined."""
    def text(row) -> str:
        return "; ".join(f"{check}: {line}" for check, line in (findings or {}).get(key_of(row), []))
    return text


def activity_csv(events: Iterable[dict], kinds: dict[str, str]) -> str:
    """The Activity page's events in view: when, the type as the page names it, the run, what
    happened, the order id if the event names one, and the details as JSON."""
    import json

    writer = _Writer()
    writer.writerow(["When", "Type", "Run", "What happened", "Order ID", "Details"])
    for e in events:
        details = e.get("details") or {}
        if not isinstance(details, dict):
            details = {"value": details}
        writer.writerow([str(e.get("at", "")), kinds.get(e.get("kind"), str(e.get("kind", ""))),
                         str(e.get("run_id") or ""), str(e.get("summary", "")), str(details.get("order_id") or ""),
                         json.dumps(details, ensure_ascii=False, sort_keys=True) if details else ""])
    return writer.getvalue()


def expenses_csv(expenses: Iterable[dict]) -> str:
    """The year's expense list as the grid shows it, a receipt as its link or its stored file."""
    writer = _Writer()
    writer.writerow(["Date", "Description", "Category", "Profile", "Email", "Receipt", "Amount"])
    for e in expenses:
        receipt = e.get("receipt") or {}
        writer.writerow([e.get("date", ""), e.get("description", ""), e.get("category", ""), e.get("profile", ""),
                         e.get("email", ""), receipt.get("url") or receipt.get("file") or "", _amount(e.get("amount"))])
    return writer.getvalue()


def _csv(header: list[str], lines: Iterable[list]) -> str:
    writer = _Writer()
    writer.writerow(header)
    for line in lines:
        writer.writerow(line)
    return writer.getvalue()


def _amount(value) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value or "")


def year_bundle(year: int, *, rows: list, cell: Callable, summary: dict, inputs, labels: dict[str, str],
                program_logs: dict, site_logs: dict, bonus_logs: dict, other_logs: list,
                expense_file: Callable[[str], Path | None], receipt_file: Callable[[str], Path | None],
                generated_at: datetime) -> bytes:
    """One zip, `tax_<year>/...`: README, the Schedule C lines, the orders placed and the orders
    paid out in the year (two CSVs -- the two bases the summary itself keeps apart), the expense
    list with its uploaded receipts, every income entry with its date, the preparer's notes, and
    the order receipts the ledger holds on disk. What is NOT on this machine (a receipt that is a
    web link, a row with no receipt) is listed in the README rather than silently absent."""
    year_text = str(int(year))
    root = f"tax_{year_text}"
    placed = [r for r in rows if str(r.order_date or "")[:4] == year_text]
    paid = [r for r in rows if str(r.payout_date or "")[:4] == year_text]

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        # the summary
        zf.writestr(f"{root}/schedule_c.csv", _csv(
            ["Part", "Line", "Item", "Amount", "What it holds"],
            [[l["part"], l["line"], l["name"], _amount(l["amount"]), l["what"]] for l in summary.get("lines", [])]))
        # the ledger, on both bases
        zf.writestr(f"{root}/orders_placed_{year_text}.csv", orders_csv(placed, cell))
        zf.writestr(f"{root}/orders_paid_out_{year_text}.csv", orders_csv(paid, cell))
        # the expenses and their receipts
        expense_lines, expense_missing = [], []
        for entry in inputs.expenses:
            receipt = entry.get("receipt") or {}
            path = expense_file(entry["id"])
            where = ""
            if path is not None:
                arc = f"{root}/expense_receipts/{path.name}"
                zf.write(path, arc)
                where = arc[len(root) + 1:]
            elif receipt.get("url"):
                where = receipt["url"]
            elif receipt.get("file"):
                expense_missing.append(f"{entry.get('date', '')} {entry.get('description', '')}: {receipt['file']}")
                where = "MISSING: " + receipt["file"]
            expense_lines.append([entry.get("date", ""), entry.get("description", ""), entry.get("category", ""),
                                  entry.get("profile", "") or entry.get("email", ""), _amount(entry.get("amount")), where])
        zf.writestr(f"{root}/expenses.csv", _csv(
            ["Date", "Description", "Category", "Paid by", "Amount", "Receipt"], expense_lines))
        # the income entries, dated
        income = []
        for key, entries in program_logs.items():
            for e in entries:
                income.append(["Program cashback", labels.get(key, key), e.get("date", ""), _amount(e.get("amount")), e.get("note", "")])
        for key, entries in bonus_logs.items():
            for e in entries:
                income.append(["Sign-up bonus", labels.get(key, key), e.get("date", ""), _amount(e.get("amount")), e.get("note", "")])
        for name, entries in site_logs.items():
            for e in entries:
                income.append(["Cashback site", name, e.get("date", ""), _amount(e.get("amount")), e.get("note", "")])
        for other, entries in zip(inputs.other, other_logs):
            for e in entries:
                income.append(["Other income", other.get("label", ""), e.get("date", ""), _amount(e.get("amount")), e.get("note", "")])
        income.sort(key=lambda line: (line[0], line[1], line[2]))
        zf.writestr(f"{root}/income.csv", _csv(["Section", "Name", "Date", "Amount", "Note"], income))
        if (inputs.notes or "").strip():
            zf.writestr(f"{root}/notes.txt", inputs.notes.strip() + "\n")
        # the order receipts the ledger holds on disk
        seen: set[str] = set()
        on_disk, linked, missing, without = 0, [], [], []
        for row in placed + paid:
            link = str(row.text("receipt_url") or "").strip()
            order_id = row.order_id
            if not link:
                if order_id not in seen:
                    seen.add(order_id)
                    without.append(f"{order_id} ({row.status})")
                continue
            if link in seen:
                continue
            seen.add(link)
            if not link.startswith(RECEIPT_PREFIX):
                linked.append(f"{order_id}: {link}")
                continue
            path = receipt_file(link[len(RECEIPT_PREFIX):])
            if path is None or not path.is_file():
                missing.append(f"{order_id}: {link}")
                continue
            zf.write(path, f"{root}/order_receipts/{link[len(RECEIPT_PREFIX):]}")
            on_disk += 1
        # the README, last: it describes what went in
        lines = [
            f"Buying Group Ledger -- tax year {year_text}",
            f"Generated {generated_at.strftime('%Y-%m-%d %H:%M')} UTC.",
            "",
            "What is in this folder:",
            f"  schedule_c.csv               the year's figures laid out on Schedule C (cash basis: income by",
            f"                               payout date, cost of goods and insurance by order date)",
            f"  {f'orders_placed_{year_text}.csv':<29}every ledger row whose order was placed in {year_text} ({len(placed)} rows)",
            f"  {f'orders_paid_out_{year_text}.csv':<29}every ledger row whose payout landed in {year_text} ({len(paid)} rows)",
            f"  expenses.csv                 the year's expense list ({len(inputs.expenses)} entries); its receipts are in expense_receipts/",
            f"  income.csv                   every program cashback, sign-up bonus, cashback-site and other income entry, dated",
            f"  notes.txt                    the notes for the preparer" + ("" if (inputs.notes or "").strip() else " (none this year, so no file)"),
            f"  order_receipts/              the retailers' receipts held on this machine ({on_disk} files), by retailer and month",
            "",
            "A row can appear in both order CSVs: placed in one year, paid out in another -- the",
            "straddle lines under the summary on the Taxes page explain which is which.",
        ]
        straddle = summary.get("straddling") or {}
        if straddle:
            lines += ["", "Straddling the year boundary:"]
            for key, value in straddle.items():
                if isinstance(value, dict) and value.get("rows"):
                    parts = ", ".join(f"{k} {v:,.2f}" if isinstance(v, float) else f"{k} {v}" for k, v in value.items() if k != "rows")
                    lines.append(f"  {key.replace('_', ' ')}: {value['rows']} row(s){' -- ' + parts if parts else ''}")
        if linked:
            lines += ["", "Order receipts that are web links, not files on this machine (open the link):"] + [f"  {x}" for x in linked]
        if missing:
            lines += ["", "Order receipts the ledger names but that are not on disk:"] + [f"  {x}" for x in missing]
        if without:
            lines += ["", "Orders with no receipt recorded:"] + [f"  {x}" for x in without]
        if expense_missing:
            lines += ["", "Expense receipts the list names but that are not on disk:"] + [f"  {x}" for x in expense_missing]
        zf.writestr(f"{root}/README.txt", "\n".join(lines) + "\n")
    return buffer.getvalue()
