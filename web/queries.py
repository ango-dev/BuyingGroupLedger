"""Filtering, sorting and per-order grouping for the /orders views. Pure functions, no I/O."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from models.order import FIELDNAMES

from web.ledger_reader import FIELD_TO_HEADER, LedgerRow

#: The columns the ledger table shows: EVERY column, in the ledger's own order. Each is sortable. The heading is HEADER's own name
#: for the column so the page and the ledger never disagree on a label.
TABLE_COLUMNS = tuple(FIELDNAMES)

#: Rendered as anchors rather than text.
LINK_FIELDS = ("order_url", "tracking_url", "receipt_url")
#: Columns whose cell editor offers the ledger's previous answers -- and takes a new one, which
#: is a previous answer from then on.
CHOICE_FIELDS = ("status", "retailer", "buying_group", "card_name", "card_last4", "profile_label")


def choice_values(rows) -> dict[str, list[str]]:
    """Every distinct non-blank value per CHOICE_FIELDS column, most used first, then A-Z."""
    counts: dict[str, Counter] = {f: Counter() for f in CHOICE_FIELDS}
    for row in rows:
        for field in CHOICE_FIELDS:
            value = row.text(field).strip()
            if value:
                counts[field][value] += 1
    return {f: [v for v, _ in sorted(c.items(), key=lambda kv: (-kv[1], kv[0].lower()))]
            for f, c in counts.items()}


def card_pairs(rows, cards=()) -> list[list[str]]:
    """Every (Card Name, Card Last 4) pairing the ledger's rows and the settings' cards know, most
    used first, so the editor can narrow one to the other."""
    counts: Counter = Counter()
    for row in rows:
        name, last4 = row.text("card_name").strip(), row.text("card_last4").strip()
        if name and last4:
            counts[(name, last4)] += 1
    for card in cards:
        name, last4 = str(getattr(card, "name", "") or "").strip(), str(getattr(card, "last4", "") or "").strip()
        if name and last4:
            counts[(name, last4)] += 0  # present, ranked below every used pair
    return [[n, l4] for (n, l4), _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0][0].lower(), kv[0][1]))]


def cell_choices(rows, cards=()) -> dict:
    """What #cell-choices carries: the previous answers per choice column, and the card pairs."""
    return {"values": choice_values(rows), "card_pairs": card_pairs(rows, cards)}
#: Right-aligned, money-formatted.
MONEY_FIELDS = ("cost_per_item", "total_cost", "shipping", "sales_tax", "gift_card", "rewards_used",
                "cogs", "insurance", "payout_amount", "total_profit", "expected_payout")

#: Sorted as numbers (blank last), everything else as text.
NUMERIC_SORT = {
    "quantity", "shipment", "total_cost", "cost_per_item", "shipping", "sales_tax", "gift_card",
    "rewards_used", "cashback_rate", "cogs", "insurance", "payout_amount", "return_quantity",
    "total_profit", "expected_payout",
}

#: Which fields a free-text search looks in.
SEARCH_FIELDS = ("order_id", "item_name", "tracking_number", "package_id", "card_name", "card_last4",
                 "delivery_address")  # the card's name too

DEFAULT_SORT = "order_date"
#: The columns a card's per-row mini-table shows (the same editable cells as the big table).
CARD_COLUMNS = ("item_name", "shipment", "status", "quantity", "tracking_number", "delivery_date",
                "insurance", "expected_payout", "payout_amount", "payout_date", "total_profit", "delivery_address",
                "order_url", "tracking_url", "receipt_url")
#: The columns an order page's shipment tables show, every one through the same editable cell.
ORDER_COLUMNS = ("item_name", "status", "quantity", "cost_per_item", "total_cost", "shipping",
                 "sales_tax", "gift_card", "rewards_used", "cashback_rate", "cogs", "insurance",
                 "expected_payout", "payout_amount", "payout_date", "return_quantity", "return_date", "total_profit",
                 "tracking_number", "delivery_date", "package_id", "buying_group", "card_last4")
#: What the cards view can sort by (the table sorts by any column header).
SORT_CHOICES = tuple((f, FIELD_TO_HEADER[f]) for f in (
    "order_date", "order_id", "status", "retailer", "buying_group", "card_name", "card_last4", "item_name",
    "delivery_date", "total_cost", "expected_payout", "payout_amount", "payout_date", "total_profit",
    "last_scraped_at"))  # the card columns: 
#: The two ways the Orders page shows the ledger: the spreadsheet-like table, or one card per ORDER.
VIEWS = ("table", "cards")
PER_PAGE_CHOICES = (12, 24, 48, 96)
DEFAULT_PER_PAGE = 24
#: The payout-state filter (the overview's tiles link with it): the rows still waiting on the
#: buying group (LedgerRow.is_open), the projected ones (is_committed) or the settled ones.
STATES = ("open", "committed", "settled", "unpaid")
_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


_YEAR = re.compile(r"^\d{4}$")


def month_of(value) -> str:
    """A YYYY-MM string, or a bare YYYY (the overview's year column links here, 2026-09-19), or
    "" -- anything else (a stale link, a typo) means no window. Both match the date's prefix."""
    text = str(value or "").strip()
    return text if _MONTH.match(text) or _YEAR.match(text) else ""


def _values(params, name: str) -> tuple[str, ...]:
    """Every value a query/form mapping carries for `name` (repeated params = multi-select),
    trimmed, blanks dropped. Works on starlette's QueryParams / FormData (getlist) and on a plain
    dict whose value is a string or a list."""
    if hasattr(params, "getlist"):
        raw = params.getlist(name)
    else:
        value = params.get(name)
        raw = value if isinstance(value, (list, tuple)) else ([value] if value is not None else [])
    return tuple(dict.fromkeys(str(v).strip() for v in raw if str(v).strip()))


@dataclass(frozen=True)
class Filters:
    """The Orders page's state. The five facets are MULTI-select: an empty tuple means every value. The
    card facet holds Card Last 4 values ("(blank)" for rows without one: "why
    is there no way to search by Card")."""

    retailers: tuple[str, ...] = ()
    profiles: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    cards: tuple[str, ...] = ()
    q: str = ""
    sort: str = DEFAULT_SORT
    desc: bool = True
    #: Whether the sort was ASKED for (a header click, the cards view's Sort by) or is the default
    #: -- Order Date, newest first -- which the table shows without an arrow.
    explicit_sort: bool = False
    view: str = "table"
    per: int = DEFAULT_PER_PAGE
    page: int = 1
    #: Calendar-month windows (YYYY-MM, "" = any): `month` on Order Date, `paid` on Payout Date.
    month: str = ""
    paid: str = ""
    #: One of STATES, or "" for every row.
    state: str = ""

    @classmethod
    def from_query(cls, params) -> "Filters":
        """From a request's query mapping. An unknown sort key, view or page size falls back to
        the default rather than raising -- a stale bookmark should still render."""
        sort = str(params.get("sort") or "").strip()
        explicit = sort in FIELDNAMES
        if not explicit:
            sort = DEFAULT_SORT
        direction = str(params.get("dir") or "").strip().lower() if explicit else ""
        desc = direction != "asc" if direction else sort in ("order_date", "delivery_date",
                                                              "payout_date", "last_scraped_at")
        view = str(params.get("view") or "table").strip().lower()
        if view not in VIEWS:
            view = "table"
        try:
            per = int(str(params.get("per") or DEFAULT_PER_PAGE))
        except ValueError:
            per = DEFAULT_PER_PAGE
        if per not in PER_PAGE_CHOICES:
            per = DEFAULT_PER_PAGE
        try:
            page = max(1, int(str(params.get("page") or 1)))
        except ValueError:
            page = 1
        state = str(params.get("state") or "").strip().lower()
        if state not in STATES:
            state = ""
        return cls(
            month=month_of(params.get("month")),
            paid=month_of(params.get("paid")),
            state=state,
            retailers=_values(params, "retailer"),
            profiles=_values(params, "profile"),
            statuses=tuple(s.lower() for s in _values(params, "status")),
            groups=_values(params, "group"),
            cards=_values(params, "card"),
            q=str(params.get("q") or "").strip(),
            sort=sort,
            desc=desc,
            explicit_sort=explicit,
            view=view,
            per=per,
            page=page,
        )

    # Single-value conveniences for templates and older callers.
    @property
    def retailer(self) -> str:
        return self.retailers[0] if len(self.retailers) == 1 else ""

    @property
    def profile(self) -> str:
        return self.profiles[0] if len(self.profiles) == 1 else ""

    @property
    def status(self) -> str:
        return self.statuses[0] if len(self.statuses) == 1 else ""

    @property
    def group(self) -> str:
        return self.groups[0] if len(self.groups) == 1 else ""

    def as_query(self, **overrides) -> dict:
        """The query mapping for a link; multi-valued facets are lists (encode with doseq)."""
        values = {"retailer": list(self.retailers), "profile": list(self.profiles),
                  "status": list(self.statuses), "group": list(self.groups), "card": list(self.cards), "q": self.q,
                  "month": self.month, "paid": self.paid, "state": self.state,
                  "sort": self.sort if self.explicit_sort else "",
                  "dir": ("desc" if self.desc else "asc") if self.explicit_sort else "",
                  "view": self.view if self.view != "table" else "",
                  "per": str(self.per) if self.per != DEFAULT_PER_PAGE else "",
                  "page": str(self.page) if self.page > 1 else ""}
        values = {**values, **overrides}
        if overrides.get("sort") and "dir" not in overrides:  # a sort named without a direction keeps ours
            values["dir"] = "desc" if self.desc else "asc"
        return {k: v for k, v in values.items() if v not in ("", None, [], ())}


def query_string(mapping: dict) -> str:
    """urlencode with doseq, so a list value becomes repeated parameters."""
    from urllib.parse import urlencode

    return urlencode(mapping, doseq=True)


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
        if filters.retailers and row.retailer not in filters.retailers:
            continue
        if filters.profiles and row.profile not in filters.profiles:
            continue
        if filters.statuses and row.status not in filters.statuses:
            continue
        if filters.cards and (row.text("card_last4").strip() or "(blank)") not in filters.cards:
            continue
        if filters.groups and (row.buying_group or "(blank)") not in filters.groups:
            continue
        if filters.month and not row.order_date.startswith(filters.month):
            continue
        if filters.paid and not row.payout_date.startswith(filters.paid):
            continue
        if filters.state and not _in_state(row, filters.state):
            continue
        if needle and not any(needle in row.text(f).lower() for f in SEARCH_FIELDS):
            continue
        out.append(row)
    return out


def _in_state(row: LedgerRow, state: str) -> bool:
    if state == "open":
        return row.is_open
    if state == "committed":
        return row.is_committed
    if state == "unpaid":
        return row.is_unpaid
    return row.is_settled


def sort_rows(rows: list[LedgerRow], filters: Filters) -> list[LedgerRow]:
    """Blanks always sort LAST, in either direction (the sort_ledger rule).
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
        "cards": card_facet(rows),
    }


def card_facet(rows) -> list[tuple[str, str]]:
    """The Card filter's options: (Card Last 4, "Name …1234") per card the ledger's rows carry,
    the name being the one the rows use most for that number, plus ("(blank)", "(blank)") when
    a row has no card. Sorted by label."""
    names: dict[str, Counter] = {}
    blank = False
    for row in rows:
        last4 = row.text("card_last4").strip()
        if not last4:
            blank = True
            continue
        names.setdefault(last4, Counter())[row.text("card_name").strip()] += 1
    out = []
    for last4, counts in names.items():
        name = max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
        out.append((last4, f"{name} …{last4}" if name else f"…{last4}"))
    out.sort(key=lambda kv: kv[1].lower())
    if blank:
        out.append(("(blank)", "(blank)"))
    return out


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
            "expected": round(sum(r.expected_payout or 0.0 for r in money_rows), 2),
            "profit": round(sum(r.profit_or_projected or 0.0 for r in money_rows
                                if r.profit_or_projected is not None), 2),
        },
        "payout_state": payout_state,
        "payout_dates": unique("payout_date"),
        "last_scraped_at": max((r.text("last_scraped_at") for r in rows), default=""),
    }


# --------------------------------------------------------------------------------------------------
# The cards view: one card per ORDER, in the table's row order, paginated
# --------------------------------------------------------------------------------------------------


def order_cards(rows: list[LedgerRow]) -> list[dict]:
    """Group already-filtered, already-sorted rows by Order ID (first appearance keeps the sort),
    summarising each order for a card: what, where, the money, and every row key (so a card's
    delete can name all of its rows)."""
    cards: dict[str, dict] = {}
    for row in rows:
        card = cards.get(row.order_id)
        if card is None:
            card = cards[row.order_id] = {
                "order_id": row.order_id, "order_date": row.order_date, "retailer": row.retailer,
                "profile": row.profile, "buying_group": row.buying_group or "(blank)",
                "statuses": [], "items": [], "tracking": [], "keys": [], "rows": 0,
                "quantity": 0, "total_cost": 0.0, "payout": 0.0, "expected": 0.0, "profit": 0.0,
                "has_profit": False, "payout_states": set(), "receipt_urls": [],
                "order_url": row.text("order_url"), "card": row.text("card_name"),
                "row_objs": [], "item_lines": [],
            }
        card["rows"] += 1
        card["row_objs"].append(row)
        if row.status and row.status not in card["statuses"]:
            card["statuses"].append(row.status)
        name = row.item_name
        if name and name not in card["items"]:
            card["items"].append(name)
        # One line per distinct item: its quantity summed over its rows, the shipments it is in.
        #
        line = next((l for l in card["item_lines"] if l["name"] == name), None)
        if line is None:
            line = {"name": name, "quantity": 0, "shipments": [], "status": row.status}
            card["item_lines"].append(line)
        line["quantity"] += int(row.number("quantity") or 0)
        if row.shipment and row.shipment not in line["shipments"]:
            line["shipments"].append(row.shipment)
        if row.tracking_number and row.tracking_number not in card["tracking"]:
            card["tracking"].append(row.tracking_number)
        card["keys"].append({"order_id": row.order_id, "order_date": row.order_date,
                             "item_name": row.item_name, "shipment": row.shipment})
        if not row.is_money_free:
            card["quantity"] += int(row.number("quantity") or 0)
            card["total_cost"] += row.total_cost or 0.0
            card["payout"] += row.payout_amount or 0.0
            card["expected"] += row.expected_payout or 0.0
            if row.profit_or_projected is not None:
                card["profit"] += row.profit_or_projected
                card["has_profit"] = True
            card["payout_states"].add(row.payout_state)
        receipt = row.text("receipt_url")
        if receipt and receipt not in card["receipt_urls"]:
            card["receipt_urls"].append(receipt)
    out = []
    for card in cards.values():
        states = card.pop("payout_states")
        if states == {"settled"}:
            card["payout_state"] = "settled"
        elif "settled" in states:
            card["payout_state"] = "partly settled"
        elif "committed" in states:
            card["payout_state"] = "committed"
        else:
            card["payout_state"] = "none"
        card["total_cost"] = round(card["total_cost"], 2)
        card["payout"] = round(card["payout"], 2)
        card["expected"] = round(card["expected"], 2)
        card["profit"] = round(card["profit"], 2) if card["has_profit"] else None
        card["is_open"] = any(s in ("ordered", "shipped", "delivered") for s in card["statuses"])
        out.append(card)
    return out


def paginate(items: list, per: int, page: int) -> dict:
    """One page of `items` plus what the pager needs. A page past the end clamps to the last."""
    total = len(items)
    pages = max(1, (total + per - 1) // per)
    page = min(max(1, page), pages)
    start = (page - 1) * per
    return {"items": items[start:start + per], "total": total, "pages": pages, "page": page,
            "per": per, "start": start + 1 if total else 0, "end": min(start + per, total),
            "has_prev": page > 1, "has_next": page < pages,
            "window": [n for n in range(max(1, page - 3), min(pages, page + 3) + 1)]}
