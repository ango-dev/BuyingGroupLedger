"""The cells typed by hand on the dashboard, which no run may overwrite (ledger_db/hand_edits).

    python -m scripts.hand_edits                                  # list them
    python -m scripts.hand_edits --forget 111-9990016-9990016     # release every cell of that order
    python -m scripts.hand_edits --forget 111-9990016-9990016 --field cashback_rate

Listing changes nothing. `--forget` removes the protection only -- the cell keeps its value; the
next run may write it again. Under `ledger.backend` = `db` only: a Sheet has no such record.
"""
from __future__ import annotations

import argparse
import sys

from models.order import FIELDNAMES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--forget", metavar="ORDER_ID", default="",
                        help="release the protected cells of this order's rows")
    parser.add_argument("--field", default="", help="with --forget: only this field (snake_case)")
    args = parser.parse_args(argv)

    from config.settings import settings

    if not settings.ledger_is_db():
        print("ledger.backend is `sheet`: hand edits are recorded for the database ledger only.",
              file=sys.stderr)
        return 2
    from ledger_db.hand_edits import entries, forget_order
    from ledger_db.store import LedgerDb

    db = LedgerDb(settings.ledger_db_path)
    if args.forget:
        if args.field and args.field not in FIELDNAMES:
            print(f"{args.field!r} is not a ledger field", file=sys.stderr)
            return 2
        released = forget_order(db, args.forget, args.field or None)
        print(f"Released {released} protected cell(s) of order {args.forget}"
              + (f" (field {args.field})" if args.field else "") + ".")
        return 0
    rows = entries(db)
    if not rows:
        print("No hand-edited cells are recorded: every cell is the run's to write.")
        return 0
    for e in rows:
        print(f"{e['edited_at']}  order {e['order_id']}  shipment {e['shipment']}  "
              f"{e['field']} = {e['value']!r}")
    print(f"{len(rows)} protected cell(s).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
