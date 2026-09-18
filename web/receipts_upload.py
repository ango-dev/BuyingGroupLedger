"""A receipt uploaded by hand from the dashboard: stored where the captured receipts live, linked
the same way.

The object key is receipts.sources.object_key -- receipts/<retailer>/<YYYY-MM>/<order id>.<ext> --
so a hand-uploaded receipt sits beside the captured ones and the capture's own idempotency (one
document per order, found by key) sees it. The link is receipts.store.link_for, exactly what the
scraper writes into Receipt Link. Nothing here is
retailer-specific beyond mapping the display name to the scrapers' retailer_key.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from receipts import store
from receipts.sources import object_key

#: The scrapers' retailer_key for each Retailer cell value; anything else is slugified.
RETAILER_KEYS = {
    "amazon": "amazon", "amazon business": "amazon-business", "best buy": "bestbuy",
    "bestbuy": "bestbuy", "costco": "costco",
}
ALLOWED_EXTENSIONS = ("pdf", "png", "jpg", "jpeg", "webp")
MAX_BYTES = 25 * 1024 * 1024


class UploadError(ValueError):
    pass


def retailer_key(retailer: str) -> str:
    text = (retailer or "").strip().lower()
    if text in RETAILER_KEYS:
        return RETAILER_KEYS[text]
    slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return slug or "unknown"


def extension_of(filename: str) -> str:
    ext = PurePosixPath(filename or "").suffix.lstrip(".").lower()
    if ext == "jpeg":
        ext = "jpg"
    if ext not in ALLOWED_EXTENSIONS:
        raise UploadError(f"a receipt must be one of {', '.join(ALLOWED_EXTENSIONS)}, not {ext!r}")
    return ext


def store_receipt(*, retailer: str, order_id: str, order_date: str, filename: str,
                  data: bytes) -> str:
    """Upload and return the Receipt Link. Raises UploadError with the reason otherwise."""
    if not store.is_configured():
        raise UploadError("receipt capture is off (capture_enabled under receipts in config.json); "
                          "nothing was stored")
    if not (order_id or "").strip():
        raise UploadError("Order ID is required to file a receipt")
    if not data:
        raise UploadError("the uploaded file is empty")
    if len(data) > MAX_BYTES:
        raise UploadError(f"the file is larger than {MAX_BYTES // (1024 * 1024)} MB")
    ext = extension_of(filename)
    key = object_key(retailer_key(retailer), order_id.strip(), (order_date or "").strip(), ext)
    try:
        link = store.put(key, data, ext)
    except store.ReceiptStoreError as exc:
        raise UploadError(str(exc)) from exc
    if not link:
        raise UploadError("the receipt store returned no link; check receipts.dir")
    return link


def receipt_file_path(path: str):
    """The stored file behind a `/receipts/<path>` request, or None (the store refuses a path
    that climbs out of its directory). The dashboard's one door onto the receipt files."""
    return store.path_for_link("/receipts/" + str(path or "").lstrip("/"))
