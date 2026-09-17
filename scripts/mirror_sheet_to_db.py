"""Copy the ledger into the SQLite file (data/ledger.sqlite3). Read-only on the Sheet.

    python -m scripts.mirror_sheet_to_db                       # the live Sheet, read-only scope
    python -m scripts.mirror_sheet_to_db --from-snapshot data/sheet_backup_20260910T105451Z.csv
    python -m scripts.mirror_sheet_to_db --db /somewhere/else.sqlite3

The Sheet is opened through scripts.audit_sheet.open_worksheet_readonly (the spreadsheets.readonly
scope), so this cannot write it. The SQLite file is REPLACED in one transaction with what was read.
The web dashboard's `db` backend runs exactly this on its own cache interval, so on a host where the
dashboard is up there is nothing to schedule; this command is for a one-off, a snapshot, or a host
without the dashboard.
"""

from __future__ import annotations

import argparse
import sys

from ledger_db.mirror import mirror_snapshot
from ledger_db.store import LedgerDb


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.mirror_sheet_to_db",
                                     description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from-snapshot", metavar="CSV", default=None,
                        help="mirror a data/sheet_backup_*.csv instead of the live Sheet")
    parser.add_argument("--db", default=None,
                        help="the SQLite file (default: database.path / LEDGER_DB_PATH, "
                             "i.e. data/ledger.sqlite3)")
    args = parser.parse_args(argv)

    from config.settings import settings
    from web.ledger_reader import SheetReader, SnapshotReader

    reader = SnapshotReader(args.from_snapshot) if args.from_snapshot else SheetReader(ttl_seconds=0)
    db = LedgerDb(args.db or settings.ledger_db_path)
    snapshot = reader.load(force=True)
    summary = mirror_snapshot(snapshot, db)
    print(f"Mirrored {summary['rows']} row(s) from {summary['backend']} ({summary['source']}) "
          f"into {summary['db_path']}"
          + (f"; {summary['skipped']} row(s) without an Order ID skipped" if summary["skipped"] else "")
          + ("" if summary["header_ok"] else "; NOTE: the source's header is not the current column order"),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
