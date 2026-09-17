"""The overview page's numbers, derived from a Snapshot. Pure functions, no I/O."""

from __future__ import annotations

from collections import Counter, defaultdict

from models.order import STATUSES

from web.ledger_reader import LedgerRow, Snapshot

#: How many order ids a gap list names before it says "and N more".
GAP_SAMPLE = 12


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


def overview(snapshot: Snapshot) -> dict:
    rows = snapshot.rows
    open_rows = [r for r in rows if r.is_open]

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

    # Projected = committed payouts (amount, no date, no buying-group outcome); realized = settled.
    projected = _money_block([r for r in rows if r.is_committed])
    realized = _money_block([r for r in rows if r.is_settled])

    # The audit's cogs_inputs_complete gap: a row carrying cost whose COGS cannot net a rebate.
    costed = [r for r in rows if not r.is_money_free and r.total_cost is not None]
    no_card = _gap([r for r in costed if not r.text("card_last4")])
    no_rate = _gap([r for r in costed if r.number("cashback_rate") is None])

    by_retailer = Counter(r.retailer or "(blank)" for r in rows)
    by_group = Counter(r.buying_group or "(blank)" for r in rows)

    return {
        "rows": len(rows),
        "orders": len({r.order_id for r in rows}),
        "open_rows": len(open_rows),
        "open_table": open_table,
        "status_counts": status_counts,
        "projected": projected,
        "realized": realized,
        "gaps": {"card_last4": no_card, "cashback_rate": no_rate},
        "by_retailer": sorted(by_retailer.items()),
        "by_group": sorted(by_group.items()),
    }
