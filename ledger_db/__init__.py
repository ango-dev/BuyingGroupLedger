"""THE LEDGER: one SQLite file, `data/ledger.sqlite3`.

Every writer -- the scrapers' upsert, the sort, the buying-group sync, the BFMR auto-reply, the
dashboard's editor, the repair scripts -- addresses it as a positional grid through the worksheet
face in ledger_db/worksheet.py (ledger.sync._get_worksheet hands that out); the audit and
the dashboard read the same view read-only. The grid vocabulary predates the file (it replaced a
Google Sheet on 2026-09-18; the story is in the design notes).

WHY SQLITE: one operator, one Pi, one file that bind-mounts like everything else in `data/`, no
server to run or back up separately, and the standard library speaks it. Postgres would add a
service to a deployment whose whole design is "one container, nothing else to operate".

THE SCHEMA IS DERIVED, NOT RE-TYPED: columns come from models.order.FIELDNAMES, their SQL types
from ledger.sync's own field sets (_NUMERIC_FIELDS / _INT_FIELDS / _BOOL_FIELDS), and the
primary key is the upsert key (Order ID + Order Date + Item Name + Shipment). Adding a column to the
ledger therefore adds it here on the next write -- the table is rebuilt whenever the schema differs.
"""
