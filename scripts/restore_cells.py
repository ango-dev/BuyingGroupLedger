"""Write specific cells back to specific values, from a plan file. Dry run by default.

    python -m scripts.restore_cells data/restore_plan.json            # show what would change
    python -m scripts.restore_cells data/restore_plan.json --apply    # write it

The plan is a JSON list of entries `[row_number, column_name, value, current_value, note...]` --
an entry whose column is `__delete_row__` DELETES that row instead (value is ignored; the guard is
that the row's Order ID still equals `current_value`); deletions run LAST, bottom-up, so the cell
writes and the other row numbers in the plan stay valid. Sheets shifts the rows below up and
re-anchors the relative formulas itself. --
the shape `audit_sheet`'s before/after comparison can produce (row, column, the value from the
BEFORE snapshot, the value there now). Each write is a single cell by A1 address, RAW (no formula
parsing, no locale interpretation), and only happens if the cell STILL holds `current_value`: a cell
somebody has edited since the plan was made is left alone and reported, never overwritten.

Why it exists: a wide-window scrape re-read rows another profile had imported and overwrote
reconciled costs and rates (2026-08-30). The pre-sweep snapshot held the right values; this puts
them back cell by cell, with the guard above, instead of a hand edit across twenty cells.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sheets.ledger_sync import HEADER, _col_letter, _get_worksheet, _parse_display_number


def _same(a, b) -> bool:
    """Do two cell values mean the same thing? Numbers compare numerically, text after strip()."""
    na, nb = _parse_display_number(a), _parse_display_number(b)
    if na is not None and nb is not None:
        return abs(float(na) - float(nb)) < 1e-9
    return str(a if a is not None else "").strip() == str(b if b is not None else "").strip()


def _typed(value):
    """A plan value as the type the sheet should store: int for a whole number, float otherwise."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else float(value)
    n = _parse_display_number(value)
    if n is not None and str(value).strip().replace(".", "", 1).replace("-", "", 1).isdigit():
        return int(n) if float(n).is_integer() else float(n)
    return value


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Restore specific sheet cells from a plan file. Dry run by default.")
    ap.add_argument("plan", type=Path)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    plan = json.load(args.plan.open(encoding="utf-8"))
    col = {h: _col_letter(i) for i, h in enumerate(HEADER)}
    worksheet = _get_worksheet()
    current_grid = worksheet.get_values(value_render_option="UNFORMATTED_VALUE")

    todo, skipped, deletions = [], [], []
    for entry in plan:
        row, column, value, expected_now = entry[0], entry[1], entry[2], entry[3]
        note = " ".join(str(x) for x in entry[4:])
        if column == "__delete_row__":
            oid_idx = HEADER.index("Order ID")
            live_row = current_grid[row - 1] if row - 1 < len(current_grid) else []
            live_oid = str(live_row[oid_idx] if oid_idx < len(live_row) else "").strip()
            if live_oid == str(expected_now).strip():
                deletions.append((row, live_oid, note))
            else:
                skipped.append(f"  row {row} DELETE: holds order {live_oid!r}, not {expected_now!r} -- left alone")
            continue
        if column not in col:
            print(f"  ?? unknown column {column!r} -- skipped", file=sys.stderr)
            continue
        idx = HEADER.index(column)
        live_row = current_grid[row - 1] if row - 1 < len(current_grid) else []
        live = live_row[idx] if idx < len(live_row) else ""
        if _same(live, value):
            skipped.append(f"  {col[column]}{row} {column:<14} already {value!r}")
        elif not _same(live, expected_now):
            skipped.append(f"  {col[column]}{row} {column:<14} holds {live!r}, not the expected {expected_now!r} -- "
                           f"edited since the plan; left alone")
        else:
            todo.append((row, column, _typed(value), live, note))

    for row, column, value, live, note in todo:
        print(f"  {col[column]}{row} {column:<14} {live!r:<12} -> {value!r:<12} {note}")
    for row, oid, note in sorted(deletions, reverse=True):
        print(f"  DELETE row {row} (order {oid}) {note}")
    for line in skipped:
        print(line)
    print(f"\n{len(todo)} cell(s) to write, {len(deletions)} row(s) to delete, {len(skipped)} skipped")
    if not args.apply:
        print("DRY RUN -- nothing written. Re-run with --apply.")
        return 0
    # A real sheet has a FIXED grid and a write past it 400s ("exceeds grid limits") -- grow it
    # first when the plan touches an appended column the grid does not have yet.
    widest = max((HEADER.index(column) + 1 for _r, column, _v, _l, _n in todo), default=0)
    current_cols = getattr(worksheet, "col_count", widest)
    if widest > current_cols:
        worksheet.add_cols(widest - current_cols)
        print(f"Grew the grid by {widest - current_cols} column(s) to fit {widest}.")
    for row, column, value, _live, _note in todo:
        worksheet.update(range_name=f"{col[column]}{row}", values=[[value]], value_input_option="RAW")
    for row, _oid, _note in sorted(deletions, reverse=True):
        worksheet.delete_rows(row)
    print(f"Wrote {len(todo)} cell(s), deleted {len(deletions)} row(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
