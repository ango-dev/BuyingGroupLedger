"""Spend caps on a card's cashback.

THE CAP IS DECIDED BY SPEND, NEVER BY THE RATE. A card's `caps` (models.card.CashbackCap) each name
a scope -- some retailers, or the catch-all -- a spend limit per period, a fallback rate and how the
period resets. What counts as spend is what the card was charged for a row, the COGS formula's own
basis: Total Cost + Shipping + Sales Tax - Gift Card - Rewards Used. A return gives its amount
(Return Qty x Cost Per Item) back to the allowance in the period of its Return Date (the issuer's
timing), or of the Order Date when there is none. Spend outside the ledger --
personal purchases on the same card -- is a per-period offset on the cap (`outside_spend`).

The rate a row gets: the card's rate at that retailer (config.cards.resolve_card's tiers) while the
period's spend before this row is under the limit, the fallback once it is over, and the exact
blend -- (room x rate + rest x fallback) / amount -- for the row that crosses it. Rows are walked in
Order Date order (Order ID, Shipment, Item Name within a day), so the same ledger always gives the
same answer.

TWO CALLERS. `apply_to_items` runs at scrape time (main._tag_cards): the batch's rows are placed
among the ledger's rows for that card and period and rated. `recompute` runs after every ledger
sync for rows PAST SHIPPED (delivered, paid, return): a late-arriving order or a return can shift
which later rows straddle the cap, so their rate cells are re-derived from the spend and rewritten
when they differ -- ordered / shipped rows keep their scrape-time rate until they get there, a
hand-edited rate cell is never touched. That is a deliberate exception to the ledger's era rule
(a rate cell records the rate at purchase time): it applies only to cards WITH a cap, only inside
the cap's scope, and derives from the card's configured rates, so keep those current.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ledger.sync import HEADER
from models.card import Card, CashbackCap, normalize_last4, parse_rate
from models.order import FIELDNAMES

__all__ = ["SpendEvent", "allowance", "allowances", "apply_to_items", "basis", "capped_rate", "events_from_rows",
           "period_key", "recompute", "rows_by_field", "spend_before"]

#: Rows the post-sync recompute may rewrite: past shipped. Cancelled and
#: superseded rows carry no money; ordered and shipped rows are still the scrapers' to re-tag.
RECOMPUTE_STATUSES = ("delivered", "paid", "return")
_HEADER_TO_FIELD = {header: field for field, header in zip(FIELDNAMES, HEADER)}


def _num(value) -> float:
    """A ledger amount as a number: "$1,259.99", "1259.99", 1259.99 or blank (0)."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip().replace("$", "").replace(",", "")
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def basis(cells) -> float:
    """What the card was charged for this row: the COGS basis, never below zero."""
    amount = (_num(cells.get("total_cost")) + _num(cells.get("shipping")) + _num(cells.get("sales_tax"))
              - _num(cells.get("gift_card")) - _num(cells.get("rewards_used")))
    return round(max(0.0, amount), 2)


def period_key(cap: CashbackCap, when: str) -> str:
    """Which allowance a date falls in: "all" for a cap that never resets, the calendar year, or
    the year an anniversary period started (a cap resetting 03-15 puts 2026-03-01 in "2025")."""
    when = str(when or "")[:10]
    if cap.resets == "never":
        return "all"
    if len(when) < 10 or not when[:4].isdigit():
        return when[:4] or "?"
    if cap.resets == "calendar-year":
        return when[:4]
    year = int(when[:4])
    return str(year if when[5:10] >= cap.resets else year - 1)


@dataclass(frozen=True)
class SpendEvent:
    """One movement of the allowance: a purchase (+basis) or a return (-amount), with the order it
    takes among a day's events and the ledger key of the row it came from."""
    when: str
    rank: tuple  # (0 for a purchase / 1 for a return, order_id, shipment, item_name)
    amount: float
    retailer: str
    card_last4: str
    profile: str
    key: tuple  # (order_id, order_date, item_name, shipment)

    @property
    def order(self) -> tuple:
        return (self.when, self.rank)


def _key_of(cells) -> tuple:
    return (str(cells.get("order_id") or ""), str(cells.get("order_date") or "")[:10],
            str(cells.get("item_name") or ""), str(cells.get("shipment") or ""))


def _shipment_rank(value) -> tuple:
    text = str(value or "").strip()
    try:
        return (0, float(text))
    except ValueError:
        return (1, text)


def events_of(cells, *, shipping=None, sales_tax=None, gift_card=None, rewards_used=None) -> list[SpendEvent]:
    """The purchase, and the return when there is one, of a row given by field name. The keyword
    amounts override the row's own order-level cells (the batch gives each row its share)."""
    status = str(cells.get("status") or "").strip().lower()
    last4 = normalize_last4(str(cells.get("card_last4") or ""))
    if status in ("cancelled", "superseded") or not last4:
        return []
    row = dict(cells)
    for name, value in (("shipping", shipping), ("sales_tax", sales_tax), ("gift_card", gift_card),
                        ("rewards_used", rewards_used)):
        if value is not None:
            row[name] = value
    key = _key_of(row)
    order_date = key[1]
    retailer = str(row.get("retailer") or "")
    profile = str(row.get("profile_label") or "")
    events = [SpendEvent(order_date, (0, key[0], _shipment_rank(key[3]), key[2]), basis(row),
                         retailer, last4, profile, key)]
    returned = _num(row.get("return_quantity")) * _num(row.get("cost_per_item"))
    if returned > 0:
        when = str(row.get("return_date") or "")[:10] or order_date
        events.append(SpendEvent(when, (1, key[0], _shipment_rank(key[3]), key[2]), -round(returned, 2),
                                 retailer, last4, profile, key))
    return events


def rows_by_field(values: list[list]) -> list[dict]:
    """The ledger's rows (header first, as get_all_values gives them) as dicts by field name."""
    if not values:
        return []
    fields = [_HEADER_TO_FIELD.get(str(h).strip(), str(h).strip()) for h in values[0]]
    rows = []
    for raw in values[1:]:
        cells = {fields[i]: (raw[i] if i < len(raw) else "") for i in range(len(fields))}
        rows.append(cells)
    return rows


def events_from_rows(rows: list[dict]) -> list[SpendEvent]:
    out: list[SpendEvent] = []
    for cells in rows:
        out.extend(events_of(cells))
    return out


def _card_of(cards: list[Card], last4: str, profile: str) -> Card | None:
    matches = [c for c in cards if c.matches(last4, profile)]
    if not matches:
        return None
    matches.sort(key=lambda c: -c.specificity())
    return matches[0]


def _root_of(cards: list[Card], last4: str, profile: str) -> Card | None:
    """The card whose caps a purchase counts against: a virtual number's parent, else the card itself."""
    card = _card_of(cards, last4, profile)
    if card is not None and card.virtual_of:
        parent = _card_of(cards, card.virtual_of, profile)
        if parent is not None:
            return parent
    return card


def spend_before(events, cards: list[Card], card: Card, cap: CashbackCap, upto: SpendEvent) -> float:
    """The period's spend on this card -- its virtual numbers included -- within this cap's scope,
    before `upto` in date order, plus the period's outside-ledger offset."""
    period = period_key(cap, upto.when)
    total = float(cap.outside_spend.get(period, 0.0))
    for e in events:
        if _root_of(cards, e.card_last4, e.profile) is not card or card.cap_for(e.retailer) is not cap:
            continue
        if period_key(cap, e.when) != period or e.order >= upto.order:
            continue
        total += e.amount
    return round(total, 2)


def capped_rate(rate: float, cap: CashbackCap, used: float, amount: float, fallback: float | None = None) -> float:
    """The rate a purchase of `amount` earns with `used` of the allowance already spent. `fallback`
    is the rate past the limit (Card.fallback_for); a cap's own when not given."""
    if fallback is None:
        fallback = cap.fallback_rate if cap.fallback_rate is not None else rate
    room = cap.spend_limit - used
    if amount <= 0 or room >= amount:
        return rate
    if room <= 0:
        return fallback
    return round((room * rate + (amount - room) * fallback) / amount, 4)


def allowance(events, cards: list[Card], card: Card, cap: CashbackCap, today: str) -> dict:
    """Where a cap stands in the period `today` falls in: {period, used, limit, left, fraction} --
    every event of the period on this card (its virtual numbers included) within the cap's scope,
    plus the period's outside-ledger offset."""
    period = period_key(cap, today)
    used = float(cap.outside_spend.get(period, 0.0))
    for e in events:
        if _root_of(cards, e.card_last4, e.profile) is not card or card.cap_for(e.retailer) is not cap:
            continue
        if period_key(cap, e.when) == period:
            used += e.amount
    used = round(used, 2)
    left = round(cap.spend_limit - used, 2)
    return {"period": period, "used": used, "limit": cap.spend_limit, "left": max(0.0, left),
            "fraction": min(1.0, max(0.0, used / cap.spend_limit)) if cap.spend_limit else 0.0}


def allowances(rows: list[dict], cards: list[Card], today: str) -> dict:
    """{(card index, cap index): allowance} for every cap on every card, over the ledger's rows."""
    events = events_from_rows(rows)
    out = {}
    for i, card in enumerate(cards):
        for j, cap in enumerate(card.caps):
            out[(i, j)] = allowance(events, cards, card, cap, today)
    return out


# --------------------------------------------------------------------------------------------------
# Scrape time: the batch among the ledger's rows
# --------------------------------------------------------------------------------------------------


def apply_to_items(items: list, cards: list[Card], ledger_rows: list[dict], *, default_rate: float | None = None) -> list[tuple]:
    """Rate the batch's items against their cards' caps, in place. `items` carry the ORDER-LEVEL
    shipping / tax / gift card / rewards repeated on every row (models.order), so each row takes
    its cost-weighted share here, as ledger.sync will. Ledger rows with a batch item's key are the
    item's older self and are left out. Returns (item, rate before, rate after) for every change."""
    if not any(c.caps for c in cards):
        return []
    batch_keys = {_key_of(_item_cells(it)) for it in items}
    events = [e for e in events_from_rows(ledger_rows) if e.key not in batch_keys]
    # each order's rows share the order-level amounts by Total Cost, as _reprorate_order_level does
    totals: dict[str, float] = {}
    for it in items:
        totals[it.order_id] = totals.get(it.order_id, 0.0) + _num(it.total_cost)
    batch: list[tuple[SpendEvent, object]] = []
    for it in items:
        cells = _item_cells(it)
        weight = (_num(it.total_cost) / totals[it.order_id]) if totals.get(it.order_id) else 0.0
        shares = {name: round(_num(getattr(it, name, None)) * weight, 2)
                  for name in ("shipping", "sales_tax", "gift_card", "rewards_used")}
        for e in events_of(cells, **shares):
            batch.append((e, it))
    batch.sort(key=lambda pair: pair[0].order)
    all_events = events + [e for e, _ in batch]
    changes = []
    for e, it in batch:
        if e.rank[0] != 0 or it.cashback_rate is None:
            continue  # a return, or a row with no resolvable rate
        card = _root_of(cards, e.card_last4, e.profile)
        cap = card.cap_for(e.retailer) if card else None
        if cap is None:
            continue
        used = spend_before(all_events, cards, card, cap, e)
        new = capped_rate(float(it.cashback_rate), cap, used, e.amount, card.fallback_for(cap, default_rate))
        if abs(new - float(it.cashback_rate)) > 5e-5:
            changes.append((it, it.cashback_rate, new))
            it.cashback_rate = new
    return changes


def _item_cells(item) -> dict:
    return {name: getattr(item, name, "") for name in (
        "order_id", "order_date", "item_name", "shipment", "status", "retailer", "profile_label",
        "card_last4", "cost_per_item", "total_cost", "shipping", "sales_tax", "gift_card",
        "rewards_used", "return_quantity", "return_date")}


# --------------------------------------------------------------------------------------------------
# After a sync: rows past shipped follow the spend
# --------------------------------------------------------------------------------------------------


def recompute(values: list[list], cards: list[Card], protected: dict | None = None,
              *, default_rate: float | None = None) -> list[tuple[int, float, float | None]]:
    """(ledger row number, new rate, old rate) for every past-shipped row whose rate the spend
    now puts on another tier. `protected` is ledger_db.hand_edits.protected_fields' map; a row
    whose Cashback Rate was typed by hand is skipped."""
    if not any(c.caps for c in cards):
        return []
    rows = rows_by_field(values)
    events = events_from_rows(rows)
    protected = protected or {}
    changes: list[tuple[int, float, float | None]] = []
    for number, cells in enumerate(rows, start=2):
        status = str(cells.get("status") or "").strip().lower()
        if status not in RECOMPUTE_STATUSES:
            continue
        purchase = next((e for e in events_of(cells) if e.rank[0] == 0), None)
        if purchase is None:
            continue
        if "cashback_rate" in protected.get(purchase.key, ()):
            continue
        own = _card_of(cards, purchase.card_last4, purchase.profile)
        card = _root_of(cards, purchase.card_last4, purchase.profile)
        cap = card.cap_for(purchase.retailer) if card else None
        if cap is None or own is None:
            continue
        rate = card.rate_for(purchase.retailer)  # a virtual number earns its card's rates
        if rate is None:
            rate = default_rate
        if rate is None:
            continue
        used = spend_before(events, cards, card, cap, purchase)
        promo = _num(cells.get("promo_rate"))  # Amazon's extra rides on top, outside the cap
        new = round(capped_rate(float(rate), cap, used, purchase.amount, card.fallback_for(cap, default_rate)) + promo, 4)
        old = parse_rate(cells.get("cashback_rate")) if str(cells.get("cashback_rate") or "").strip() else None
        try:
            old = float(old) if old is not None else None
        except (TypeError, ValueError):
            old = None
        if old is None or abs(new - old) > 5e-5:
            changes.append((number, new, old))
    return changes
