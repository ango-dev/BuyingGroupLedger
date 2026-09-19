"""Bring the receipts home: download every OCI-hosted receipt the ledger links to into the local
store (receipts/store.py, `receipts.dir`) and rewrite each Receipt Link to the dashboard-relative
form. Dry run by default; one-time, after the OCI removal (2026-09-18).

    python -m scripts.migrate_receipts_local            # show what would move
    python -m scripts.migrate_receipts_local --apply    # download and rewrite

HOW IT FINDS THEM. A Receipt Link written by the old store is the PAR prefix joined to the object
key, so it ends `.../receipts/<retailer>/<YYYY-MM>/<order id>.<ext>`; the key is read back off the
URL from its `receipts/` segment and the file is stored under exactly that key, so nothing else
changes identity. The PAR URL itself is the read credential, so no OCI setting is needed to fetch
-- run this BEFORE revoking the PAR in the console. A link that is already relative is skipped; a
link that does not look like a receipt object is reported and left alone; a download that fails
leaves the row untouched so a re-run picks it up. Rows of one order share the link and are
rewritten together.
"""
from __future__ import annotations

import argparse
import sys
import urllib.request
from urllib.parse import urlsplit

from receipts import store
from ledger.sync import HEADER, _col_letter, _get_worksheet


def key_from_link(link: str) -> str | None:
    """`.../o/receipts/bestbuy/2026-09/BBY01-1.pdf` -> `receipts/bestbuy/2026-09/BBY01-1.pdf`."""
    text = str(link or "").strip()
    if not text.lower().startswith(("http://", "https://")):
        return None
    path = urlsplit(text).path
    marker = "/receipts/"
    if marker not in path:
        return None
    key = "receipts/" + path.split(marker, 1)[1]
    parts = key.split("/")
    if len(parts) != 4 or not all(parts) or "." not in parts[3]:
        return None
    return key


def plan_migration(grid: list[list]) -> tuple[dict[str, tuple[str, list[int]]], list[str]]:
    """{link: (key, [row numbers])} for every hosted link, and the rows left alone as text."""
    header = [str(h).strip() for h in grid[0]] if grid else []
    if "Receipt Link" not in header or "Order ID" not in header:
        raise ValueError("the ledger's header has no Receipt Link column")
    link_i, oid_i = header.index("Receipt Link"), header.index("Order ID")
    moves: dict[str, tuple[str, list[int]]] = {}
    notes: list[str] = []
    for n, row in enumerate(grid[1:], start=2):
        oid = str(row[oid_i]).strip() if oid_i < len(row) else ""
        link = str(row[link_i]).strip() if link_i < len(row) and row[link_i] is not None else ""
        if not oid or not link or link.startswith(store.LINK_PREFIX):
            continue
        key = key_from_link(link)
        if key is None:
            notes.append(f"row {n}: order {oid}: Receipt Link {link[:60]!r} is not a stored receipt "
                         "object; left alone")
            continue
        moves.setdefault(link, (key, []))[1].append(n)
    return moves, notes


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as resp:
        return resp.read()


def apply_migration(worksheet, moves: dict[str, tuple[str, list[int]]], fetch_fn=fetch) -> tuple[int, list[str]]:
    """Download each object, store it under its key, rewrite the rows. Returns (moved, failures)."""
    col = _col_letter(HEADER.index("Receipt Link"))
    moved, failures = 0, []
    for link, (key, rows) in moves.items():
        try:
            if not store.exists(key):
                body = fetch_fn(link)
                if not body:
                    raise ValueError("empty download")
                store.put(key, body, key.rsplit(".", 1)[-1])
            new_link = store.link_for(key)
        except Exception as exc:  # noqa: BLE001 -- one failure must not stop the rest
            failures.append(f"{key}: {type(exc).__name__}: {exc}")
            continue
        worksheet.batch_update([{"range": f"{col}{n}", "values": [[new_link]]} for n in rows],
                               value_input_option="RAW")
        moved += 1
        print(f"{key}: stored; rows {rows} now {new_link}")
    return moved, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="download and rewrite (dry run without it)")
    args = parser.parse_args(argv)
    if not store.is_configured():
        print("Receipt capture is off (RECEIPT_CAPTURE_ENABLED); turn it on to store receipts locally.",
              file=sys.stderr)
        return 2
    worksheet = _get_worksheet()
    try:
        moves, notes = plan_migration(worksheet.get_all_values())
    except ValueError as exc:
        print(f"Cannot plan: {exc}", file=sys.stderr)
        return 2
    for note in notes:
        print(note)
    if not moves:
        print("Nothing to migrate: every Receipt Link is already local (or blank).")
        return 0
    for link, (key, rows) in moves.items():
        print(f"{key}: rows {rows}  <- {link[:80]}")
    if not args.apply:
        print(f"\nDry run: {len(moves)} receipt(s) would be downloaded into {store.receipts_dir()} and "
              f"{sum(len(r) for _k, r in moves.values())} row(s) rewritten. Re-run with --apply.")
        return 0
    moved, failures = apply_migration(worksheet, moves)
    print(f"\nMoved {moved} receipt(s) into {store.receipts_dir()}.")
    for failure in failures:
        print(f"FAILED {failure}", file=sys.stderr)
    if failures:
        print(f"{len(failures)} receipt(s) could not be fetched; their rows keep the old link -- "
              "re-run after checking the PAR is still valid.", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
