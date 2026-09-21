"""A dated log of amounts: what was added or taken back, when, and a note.

The dashboard's hand-entered running totals -- a card's spend the ledger never sees, a program's
cashback, a cashback site's payouts -- change often, and a single number forgets how it got there.

So each of them is a list of entries `{date, amount, note}`; a NEGATIVE amount takes spend back,
which is why the log needs no deletion history. This module is the pure part: reading a stored
value in any of its shapes (`coerce`), the total, the form fields the widget posts (`parse`), and
the order the widget shows (`display`). The widget is web/templates/_amount_log.html; the period a
cap sums over lives on models.card.CashbackCap.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Callable, Mapping
from datetime import date

__all__ = ["clean", "coerce", "display", "has_fields", "in_month", "money", "month_label", "parse", "total"]

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_YEAR = re.compile(r"^\d{4}$")


def money(value) -> float:
    """"$4,000", "-50", 12.5 -> a float; anything else raises ValueError naming it."""
    if isinstance(value, bool):
        raise ValueError(f"{value!r} is not an amount")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip().replace("$", "").replace(",", "")
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"{str(value).strip()!r} is not an amount") from None


def clean(entry, default_date: str) -> dict | None:
    """One entry as stored: an ISO date (else `default_date`), a float amount, a text note; None for
    an entry without an amount. A bad amount raises ValueError."""
    if not isinstance(entry, Mapping):
        return None
    raw = entry.get("amount")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    when = str(entry.get("date") or "").strip()[:10]
    if not _ISO.match(when):
        when = default_date
    return {"date": when, "amount": round(money(raw), 2), "note": str(entry.get("note") or "").strip()}


def coerce(value, default_date: str | Callable[[str], str] = "") -> list[dict]:
    """A stored value in any shape -> entries, oldest first.

    - a list of entries: cleaned;
    - a number (or a numeric string): one entry dated `default_date`;
    - the older per-period dict `{"2026": 4000, "all": 500}` or its text form "2026: 4,000, 2027: 0":
      one entry per period, dated by `default_date(period)` when that is callable (a cap dates a
      period at its start), else "YYYY-01-01" for a year and `default_date` for anything else.
    """
    if value is None or value == "" or value == [] or value == {}:
        return []
    if isinstance(value, str) and ":" in value:
        pairs = {}
        for part in re.split(r"[;,](?=\s*[^,;:]+:)", value):  # "2026: 4,000, 2027: 0": the thousands comma stays
            if ":" in part:
                period, amount = part.split(":", 1)
                if period.strip():
                    pairs[period.strip()] = amount
        value = pairs
    if isinstance(value, Mapping):
        entries = []
        for period, amount in value.items():
            key = str(period).strip()
            if callable(default_date):
                when = default_date(key)
            else:
                when = f"{key}-01-01" if _YEAR.match(key) else (key if _ISO.match(key) else default_date)
            entries.append({"date": when, "amount": amount, "note": ""})
    elif isinstance(value, (list, tuple)):
        entries = list(value)
    else:
        entries = [{"date": default_date if not callable(default_date) else default_date("all"), "amount": value, "note": ""}]
    fallback = default_date if not callable(default_date) else default_date("all")
    out = [e for e in (clean(entry, fallback) for entry in entries) if e is not None]
    out.sort(key=lambda e: e["date"])
    return out


def total(entries) -> float:
    return round(sum(float(e.get("amount") or 0) for e in entries or []), 2)


def has_fields(form: Mapping, prefix: str) -> bool:
    """Did the form post the widget's rows for `prefix` (`<prefix>.<i>.amount`, `<prefix>.new.amount`)?"""
    head = prefix + "."
    return any(str(name).startswith(head) for name in form.keys())


def parse(form: Mapping, prefix: str, today: str | None = None) -> list[dict]:
    """The widget's rows -> entries, oldest first: `<prefix>.<i>.date|amount|note|remove` for the
    stored rows and `<prefix>.new.*` for the row being added. A row ticked `remove` or left without
    an amount is dropped; a blank date is today. A bad amount raises ValueError naming the row."""
    today = today or date.today().isoformat()
    head = prefix + "."
    rows: dict[str, dict] = {}
    for name in form.keys():
        text = str(name)
        if not text.startswith(head):
            continue
        rest = text[len(head):]
        if "." not in rest:
            continue
        index, field = rest.rsplit(".", 1)
        if field not in ("date", "amount", "note", "remove"):
            continue
        rows.setdefault(index, {})[field] = form.get(name)

    def order(index: str) -> tuple:
        return (1, 0) if index == "new" else (0, int(index)) if index.isdigit() else (2, 0)

    entries = []
    for index in sorted(rows, key=order):
        row = rows[index]
        if str(row.get("remove") or "").strip().lower() in ("on", "true", "1", "yes"):
            continue
        try:
            entry = clean(row, today)
        except ValueError as exc:
            raise ValueError(f"log row {index}: {exc}") from None
        if entry is not None:
            entries.append(entry)
    entries.sort(key=lambda e: e["date"])
    return entries


def display(entries) -> list[dict]:
    """The rows the widget shows: newest first, amounts as typed-looking numbers (4000, 12.5),
    each with its year, its month ("2026-09") and the month's name, for the widget's folds."""
    out = []
    for e in sorted(entries or [], key=lambda e: str(e.get("date") or ""), reverse=True):
        amount = e.get("amount", "")
        if isinstance(amount, float) and amount.is_integer():
            amount = int(amount)
        when = str(e.get("date") or "")
        out.append({"date": when, "amount": amount, "note": str(e.get("note") or ""),
                    "year": when[:4], "month": when[:7], "month_label": month_label(when[:7])})
    return out


def month_label(month: str) -> str:
    """"2026-09" -> "September 2026"; anything else as given."""
    try:
        year, number = month.split("-")
        return f"{calendar.month_name[int(number)]} {year}"
    except (ValueError, IndexError):
        return month


def in_month(entries, month: str) -> float:
    """The entries dated in a month ("2026-09"), summed."""
    return round(sum(float(e.get("amount") or 0) for e in entries or [] if str(e.get("date") or "").startswith(month)), 2)
