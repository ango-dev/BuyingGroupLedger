"""Audit the receipts already in object storage. Read-only by default.

    python -m scripts.receipt_verify                     # audit everything
    python -m scripts.receipt_verify --retailer bestbuy
    python -m scripts.receipt_verify --purge             # remove + re-arm the failures

Costs nothing but HTTP: it fetches each stored object through the PAR and reads its text. No
browser, no scrape.

WHY THIS EXISTS. These receipts substantiate COGS at tax time, and each is stored ONCE and never
refreshed — so a bad one is bad permanently, and nothing else in the system would ever notice. The
capture-time guard (receipts.capture._reject_if_not_final) stops new ones, but it cannot fix what is
already stored, and it fails OPEN by design, so a document it could not read still got kept.

It checks four things per object, each a real failure seen or nearly seen in practice:

  - the document contains ITS OWN order id. The cross-contamination check: Costco's order page is a
    hash-route SPA where navigating A -> B can leave A's render in place, and Amazon Business's
    invoices for same-priced orders are near-identical in size. Both would store a perfect-looking
    duplicate under the wrong key.
  - it is not a PRE-SHIPMENT invoice ("Not Yet Shipped"). Live one was stored because the
    ledger said `shipped` (Amazon assigns TBA tracking at label creation) while the invoice did not.
  - it carries a MONEY total. A receipt with no total is not proof of anything — Costco hides its
    whole Order Summary behind a "Show Details" toggle, which is exactly how that happened once.
  - it carries a PAYMENT method, for the same reason (Best Buy hides that behind its own disclosure).

`--purge` deletes a failing object by VersionId and blanks its Receipt Link, which re-arms capture:
the next run stores a correct one. Exits 1 on any failure, so it can gate a scheduled check.
"""

from __future__ import annotations

import argparse
import sys
import urllib.request

from receipts import store
from receipts.sources import not_final_reason
from sheets.ledger_sync import HEADER, _col_letter, _get_worksheet

# Any one of these is enough to call the document costed. Deliberately loose: four retailers word it
# four ways ("Grand Total", "Order Total", "Total"), and this is a completeness check, not a parse.
_MONEY_MARKERS = ("grand total", "order total", "total")
_PAYMENT_MARKERS = ("payment method", "payment details", "payment information", "ending in")


def _text(body: bytes) -> str:
    from receipts.capture import pdf_text  # lazy: keeps pypdf out of import time

    return pdf_text(body)


def _fetch(key: str) -> bytes:
    with urllib.request.urlopen(store.link_for(key), timeout=60) as resp:
        return resp.read()


def _problems(key: str, order_id: str, retailer_key: str, body: bytes) -> list[str]:
    """Everything wrong with this document, or [] if it is sound."""
    if not body:
        return ["empty object"]
    if key.endswith(".png"):
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
    """Delete every version AND delete marker for `key`. Returns how many were removed.

    A plain delete_object on this VERSIONED bucket is not a delete — it writes a delete marker and
    keeps the object recoverable (and billable) as a non-current version.
    """
    s3 = store._s3()
    bucket = store.settings.oci_bucket
    listing = s3.list_object_versions(Bucket=bucket, Prefix=key)
    # Prefix is a PREFIX: ".../1234.pdf" also matches ".../1234.pdf.bak", so match the key exactly.
    version_ids = [v["VersionId"] for v in listing.get("Versions", []) if v["Key"] == key]
    version_ids += [d["VersionId"] for d in listing.get("DeleteMarkers", []) if d["Key"] == key]
    for version_id in version_ids:
        # One at a time: OCI rejects the batch DeleteObjects call, which needs a checksum header
        # that the S3-compat client deliberately suppresses (see receipts/store.py).
        s3.delete_object(Bucket=bucket, Key=key, VersionId=version_id)
    return len(version_ids)


def _stored_keys(only_retailer=None) -> list[str]:
    s3 = store._s3()
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=store.settings.oci_bucket, Prefix="receipts/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.startswith("receipts/_"):   # self-test probes, not receipts
                continue
            parts = key.split("/")
            if len(parts) != 4:
                continue
            if only_retailer and parts[1] != only_retailer:
                continue
            keys.append(key)
    return sorted(keys)


def run(only_retailer=None, purge=False) -> int:
    if not store.is_configured():
        print("Receipt storage is not configured. Check with "
              "`python -m scripts.receipt_storage_check`.")
        return 1

    keys = _stored_keys(only_retailer)
    if not keys:
        print("No stored receipts to check.")
        return 0

    grid = _get_worksheet().get_all_values()
    rows_by_order = _rows_by_order(grid)

    failures = []
    for key in keys:
        _, retailer_key, _month, filename = key.split("/")
        order_id = filename.rsplit(".", 1)[0]
        try:
            problems = _problems(key, order_id, retailer_key, _fetch(key))
        except Exception as exc:  # noqa: BLE001 — one unreadable object must not end the audit
            problems = [f"could not fetch through the PAR: {exc}"]
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
            print(f"  {order_id}: {removed} version(s) removed, rows {rows or '-'} cleared")
        if updates:
            worksheet.batch_update(updates, value_input_option="RAW")
        print("\nThose orders will be captured again by the next run that sees them shipped.")
    elif failures:
        print("\nRe-run with --purge to remove these and let capture replace them.")

    return 1 if failures else 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — not every stream supports it (pytest capture, pipes)
            pass
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--retailer", help="amazon | amazon-business | bestbuy | costco")
    parser.add_argument("--purge", action="store_true",
                        help="delete failing receipts and blank their Receipt Link so capture "
                             "replaces them (default is read-only)")
    args = parser.parse_args(argv)
    return run(only_retailer=args.retailer, purge=args.purge)


if __name__ == "__main__":
    sys.exit(main())
