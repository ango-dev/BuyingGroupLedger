"""Move each OPEN row's committed payout out of Actual Payout into Expected Payout. Dry run by default.

    python -m scripts.migrate_expected_payout            # show what would move
    python -m scripts.migrate_expected_payout --apply    # write it

WHY. From 2026-09-11 to 2026-09-18 the buying-group sync recorded BFMR's COMMITTED payout price
in Actual Payout itself, with a blank Payout Date marking it as a promise rather than money (the
user's ruling at the time: no second column). The
dashboard's Reconciliation page needs the promise kept beside the payment, so the commitment has
its own column, `Expected Payout` (beside Actual Payout), and the sync writes there from now on. Rows that
were open at the changeover still carry their commitment in Actual Payout -- this script moves
them: Expected Payout takes the figure, Actual Payout is cleared, the profit formula is re-stamped
(the adapter computes it). Run once, after deploying the column; running it again
finds nothing to move.

WHAT MOVES, EXACTLY. A ledger row (has an Order ID) with a non-zero Actual Payout and NO Payout
Date whose Status is not `paid` / `return` (a buying-group outcome settles a dateless payout: MOD
pays without a date) and not money-free (cancelled / superseded rows are blanked by rule). A row
that already carries a DIFFERENT Expected Payout is reported and left alone -- two figures need a
human. Every write goes through ledger.sync._get_worksheet, so this edits data/ledger.sqlite3.
"""
from __future__ import annotations

import argparse
import sys

from models.order import MONEY_FREE_STATUSES
from ledger.sync import (
    HEADER, _clear_cells, _col_letter, _get_worksheet, _parse_display_number, _write_profit_formulas,
)

EXPECTED_COL = "Expected Payout"
#: A buying-group outcome status settles a payout even without a date (MOD's rows carry none).
SETTLED_STATUSES = ("paid", "return")


def plan_migration(grid: list[list]) -> tuple[list[tuple[int, str, float]], list[str]]:
    """(row_number, order_id, amount) for every commitment to move, and the rows left alone as
    text. Pure: reads a header + rows grid as get_all_values() hands it back."""
    if not grid:
        return [], []
    header = [str(h).strip() for h in grid[0]]
    idx = {h: i for i, h in enumerate(header)}
    for name in ("Order ID", "Status", "Actual Payout", "Payout Date"):
        if name not in idx:
            raise ValueError(f"the ledger's header has no {name!r} column")
    if EXPECTED_COL not in idx:
        raise ValueError(
            f"the ledger's header has no {EXPECTED_COL!r} column yet: run one sync (it appends the "
            "header), or type the heading into the next free header cell, then run this again")

    def cell(row: list, name: str) -> str:
        i = idx[name]
        return str(row[i]).strip() if i < len(row) and row[i] is not None else ""

    moves: list[tuple[int, str, float]] = []
    notes: list[str] = []
    for n, row in enumerate(grid[1:], start=2):
        order_id = cell(row, "Order ID")
        if not order_id:
            continue
        amount = _parse_display_number(cell(row, "Actual Payout"))
        if not amount:
            continue  # blank, or a zero -- neither is a commitment (the allocator never writes 0)
        status = cell(row, "Status").lower()
        if cell(row, "Payout Date") or status in SETTLED_STATUSES or status in MONEY_FREE_STATUSES:
            continue  # real money (settled), or a row blanked by rule
        expected = _parse_display_number(cell(row, EXPECTED_COL))
        if expected is not None and abs(float(expected) - float(amount)) > 0.01:
            notes.append(f"row {n}: order {order_id} already carries Expected Payout {expected} "
                         f"beside a commitment of {amount} in Actual Payout -- left alone, check it "
                         "by hand")
            continue
        moves.append((n, order_id, round(float(amount), 2)))
    return moves, notes


def apply_migration(worksheet, moves: list[tuple[int, str, float]]) -> None:
    """Write Expected Payout FIRST, then clear Actual Payout, then re-stamp the profit formula.
    An interruption between the two leaves a row carrying both figures, equal -- which the next
    run plans as a move again (idempotent), never as a loss."""
    if not moves:
        return
    from ledger_db.worksheet import ValueInputOption

    column = _col_letter(HEADER.index(EXPECTED_COL))
    worksheet.batch_update(
        [{"range": f"{column}{n}", "values": [[amount]]} for n, _order_id, amount in moves],
        value_input_option=ValueInputOption.raw,
    )
    rows = [n for n, _order_id, _amount in moves]
    _clear_cells(worksheet, rows, ("payout_amount",))
    _write_profit_formulas(worksheet, rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the moves (dry run without it)")
    args = parser.parse_args(argv)

    worksheet = _get_worksheet()
    grid = worksheet.get_all_values()
    try:
        moves, notes = plan_migration(grid)
    except ValueError as exc:
        print(f"Cannot plan the migration: {exc}", file=sys.stderr)
        return 2
    for n, order_id, amount in moves:
        print(f"row {n}: order {order_id}: Actual Payout {amount:,.2f} -> Expected Payout")
    for note in notes:
        print(note)
    if not moves:
        print("Nothing to move: no open row carries a commitment in Actual Payout.")
        return 0
    if not args.apply:
        print(f"\nDry run: {len(moves)} row(s) would move; nothing was written. Re-run with --apply.")
        return 0
    apply_migration(worksheet, moves)
    print(f"\nMoved {len(moves)} commitment(s) into Expected Payout and cleared Actual Payout.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
