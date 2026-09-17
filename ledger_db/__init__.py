"""A SQLite copy of the ledger -- step one of moving off the Google Sheet as the database.

WHAT IT IS TODAY: a MIRROR. The Sheet stays the source of truth and every writer (the scrapers'
upsert, the buying-group sync, the backfill scripts) still writes the Sheet exactly as before; this
package holds a faithful, typed copy of it in one file (`data/ledger.sqlite3`), refreshed three
ways -- by the scheduled run's LAST step (main.run_db_mirror, so the copy holds what the run just
wrote), by `scripts/mirror_sheet_to_db.py`, and by the web dashboard's `db` backend on its own cache
interval. All three only READ the Sheet (the read-only scope). Nothing on a money path reads this
file, so a host running the previous version is not affected by its existence, and a host running
this version behaves identically on every money path.

WHY SQLITE: one operator, one Pi, one file that bind-mounts like everything else in `data/`, no
server to run or back up separately, and the standard library speaks it. Postgres would add a
service to a deployment whose whole design is "one container, nothing else to operate".

THE SCHEMA IS DERIVED, NOT RE-TYPED: columns come from models.order.FIELDNAMES, their SQL types
from sheets.ledger_sync's own field sets (_NUMERIC_FIELDS / _INT_FIELDS / _BOOL_FIELDS), and the
primary key is the upsert key (Order ID + Order Date + Item Name + Shipment). Adding a column to the
ledger therefore adds it here on the next mirror -- the table is rebuilt whenever the schema differs.

WHAT COMES NEXT (the cutover, NOT built -- see the design notes): make this the primary store the writers
target and the Sheet a view exported from it. That touches every money path and is a deliberate,
separately-decided step.
"""
