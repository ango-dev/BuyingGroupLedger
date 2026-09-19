"""Audit the receipts already stored. Read-only by default.

    python -m scripts.receipt_verify                     # audit everything
    python -m scripts.receipt_verify --retailer bestbuy
    python -m scripts.receipt_verify --purge             # remove + re-arm the failures

Costs nothing: it reads each stored file under `receipts.dir` (receipts/store.py) and extracts its
text. No browser, no scrape, no network.

WHY THIS EXISTS. These receipts substantiate COGS at tax time, and each is stored ONCE and never
refreshed — so a bad one is bad permanently, and nothing else in the system would ever notice. The
capture-time guard (receipts.capture._reject_if_not_final) stops new ones, but it cannot fix what is
already stored, and it fails OPEN by design, so a document it could not read still got kept.

It checks four things per document, each a real failure seen or nearly seen in practice:

  - the document contains ITS OWN order id. The cross-contamination check: Costco's order page is a
    hash-route SPA where navigating A -> B can leave A's render in place, and Amazon Business's
    invoices for same-priced orders are near-identical in size. Both would store a perfect-looking
    duplicate under the wrong key.
  - it is not a PRE-SHIPMENT invoice ("Not Yet Shipped"). Live one was stored because the
    ledger said `shipped` (Amazon assigns TBA tracking at label creation) while the invoice did not.
  - it carries a MONEY total. A receipt with no total is not proof of anything — Costco hides its
    whole Order Summary behind a "Show Details" toggle, which is exactly how that happened once.
  - it carries a PAYMENT method, for the same reason (Best Buy hides that behind its own disclosure).

`--purge` deletes a failing file and blanks its Receipt Link, which re-arms capture: the next run
stores a correct one. Exits 1 on any failure, so it can gate a scheduled check.
"""
from __future__ import annotations

import argparse
import sys

from receipts import store
from receipts.sources import not_final_reason
from ledger.sync import HEADER, _col_letter, _get_worksheet

# Any one of these is enough to call the document costed. Deliberately loose: four retailers word it
# four ways ("Grand Total", "Order Total", "Total"), and this is a completeness check, not a parse.
_MONEY_MARKERS = ("grand total", "order total", "total")
_PAYMENT_MARKERS = ("payment method", "payment details", "payment information", "ending in")


def _text(body: bytes) -> str:
    from receipts.capture import pdf_text  # lazy: keeps pypdf out of import time

    return pdf_text(body)


def _fetch(key: str) -> bytes:
    return store.path_for(key).read_bytes()


def _problems(key: str, order_id: str, retailer_key: str, body: bytes) -> list[str]:
    """Everything wrong with this document, or [] if it is sound."""
    if not body:
        return ["empty object"]
    if not key.lower().endswith(".pdf"):
        # A screenshot fallback: no text to read, so only its existence can be checked. Not a
        # failure — it is a real receipt, just not a verifiable one.
        return []
    text = _text(body)
    if not text.strip():
        return ["no extractable text (cannot be verified)"]

    problems = []
    lowered = text.lower()
    if order_id.lower() not in lowered:
        problems.append(f"does not contain its own order id {order_id}")
    reason = not_final_reason(retailer_key, text)
    if reason:
        problems.append(f"PRE-SHIPMENT invoice ({reason!r}) - not the final document")
    if not any(m in lowered for m in _MONEY_MARKERS):
        problems.append("no total - is a collapsed section not being expanded?")
    if not any(m in lowered for m in _PAYMENT_MARKERS):
        problems.append("no payment method")
    return problems


def _rows_by_order(grid) -> dict:
    """{order_id: [sheet row numbers]} for rows that currently carry a Receipt Link."""
    h = grid[0]
    oid_i, link_i = h.index("Order ID"), h.index("Receipt Link")
    out: dict[str, list[int]] = {}
    for offset, row in enumerate(grid[1:]):
        oid = row[oid_i].strip() if len(row) > oid_i else ""
        if oid and len(row) > link_i and row[link_i].strip():
            out.setdefault(oid, []).append(offset + 2)
    return out


def _purge(key: str) -> int:
    """Delete the stored file. Returns 1 if a file went, else 0."""
    return 1 if store.delete(key) else 0


def _stored_keys(only_retailer=None) -> list[str]:
    return store.stored_keys(only_retailer)


def run(only_retailer=None, purge=False) -> int:
    if not store.is_configured():
        print("Receipt capture is off (RECEIPT_CAPTURE_ENABLED); nothing is stored to check.")
        return 1
    keys = _stored_keys(only_retailer)
    if not keys:
        print(f"No stored receipts to check under {store.receipts_dir()}.")
        return 0
    grid = _get_worksheet().get_all_values()
    rows_by_order = _rows_by_order(grid)
    failures = []
    for key in keys:
        _, retailer_key, _month, filename = key.split("/")
        order_id = filename.rsplit(".", 1)[0]
        try:
            problems = _problems(key, order_id, retailer_key, _fetch(key))
        except Exception as exc:  # noqa: BLE001 — one unreadable file must not end the audit
            problems = [f"could not read the file: {exc}"]
        if problems:
            failures.append((key, order_id, problems))
            print(f"[FAIL] {retailer_key}/{order_id}")
            for problem in problems:
                print(f"       - {problem}")
        else:
            print(f"[ OK ] {retailer_key}/{order_id}")
    print(f"\n{len(keys) - len(failures)} sound, {len(failures)} problem(s), {len(keys)} checked")
    if failures and purge:
        print("\nPurging and re-arming:")
        worksheet = _get_worksheet()
        col = _col_letter(HEADER.index("Receipt Link"))
        updates = []
        for key, order_id, _ in failures:
            removed = _purge(key)
            rows = rows_by_order.get(order_id, [])
            updates += [{"range": f"{col}{row}", "values": [[""]]} for row in rows]
            print(f"  {order_id}: {removed} file(s) removed, rows {rows or '-'} cleared")
        if updates:
            worksheet.batch_update(updates, value_input_option="RAW")
        print("\nThose orders will be captured again by the next run that sees them shipped.")
    elif failures:
        print("\nRe-run with --purge to remove these and let capture replace them.")
    return 1 if failures else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--retailer", help="amazon | amazon-business | bestbuy | costco")
    parser.add_argument("--purge", action="store_true",
                        help="delete each failing receipt and blank its Receipt Link so capture re-arms")
    args = parser.parse_args(argv)
    return run(only_retailer=args.retailer, purge=args.purge)


if __name__ == "__main__":
    sys.exit(main())
