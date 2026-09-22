"""Where receipts live: a directory beside the ledger, served by the dashboard.

    exists(key)   -> is this order's receipt already stored? (decides whether a browser opens at all)
    put(key, ...) -> store one rendered receipt, return its Receipt Link
    link_for(key) -> the URL that goes in the ledger's Receipt Link column
    path_for(key) -> the file on disk

WHY A DIRECTORY, NOT OBJECT STORAGE. The bucket existed because the Pi
was not reachable and the ledger could not hold files. Now the dashboard serves files, the user
reaches it over WireGuard, and `data/` is in every backup -- so receipts sit under
`receipts.dir` (default data/receipts) as `<retailer>/<YYYY-MM>/<order id>.<ext>` and the link
the ledger carries is RELATIVE, `/receipts/<retailer>/<YYYY-MM>/<order id>.<ext>`: the dashboard
resolves it wherever it is served from (LAN, WireGuard, a new host after a restore), and no
credential, PAR or hostname ever ends up in a row. The BFMR auto-reply reads such a link straight
off the disk (respond_bfmr._fetch_pdf).

The object KEY is unchanged from the bucket days -- receipts.sources.object_key, starting
`receipts/` -- so every caller (capture, the backfill, the dashboard upload, the verify script,
the migration off OCI) keeps the same idempotent identity: one document per order, found by key.

INERT WHEN OFF. `receipt_capture_enabled` false makes every method a no-op that raises nothing, so
orders record exactly as before with a blank Receipt Link -- receipts are additive, and losing an
order to a storage problem would be the wrong trade.
"""
from __future__ import annotations

import logging
from pathlib import Path

from config.settings import settings

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
LINK_PREFIX = "/receipts/"
KEY_PREFIX = "receipts/"
_warned = False


class ReceiptStoreError(Exception):
    """The store is on but could not be used (a disk problem, a bad key)."""


def is_configured() -> bool:
    """Is receipt capture switched on? The store needs nothing else."""
    return bool(settings.receipt_capture_enabled)


def missing_settings() -> list[str]:
    """Kept for preflight's shape: a directory store has nothing that can be half-configured."""
    return []


def _warn_once() -> None:
    global _warned
    if not _warned:
        _warned = True
        log.info("Receipt capture is disabled (RECEIPT_CAPTURE_ENABLED); orders record normally with "
                 "a blank Receipt Link.")


def receipts_dir() -> Path:
    """`receipts.dir`; a relative path is under the repo root (in Docker, the mounted data/)."""
    raw = Path(str(settings.receipts_dir or "data/receipts"))
    return raw if raw.is_absolute() else ROOT / raw


def _relative(key: str) -> Path:
    """The path under receipts_dir for a key, refusing anything that could climb out of it."""
    key = str(key or "").strip().lstrip("/")
    if key.startswith(KEY_PREFIX):
        key = key[len(KEY_PREFIX):]
    parts = [p for p in key.split("/") if p]
    if not parts or any(p in (".", "..") or "\\" in p for p in parts):
        raise ReceiptStoreError(f"refusing the receipt key {key!r}")
    return Path(*parts)


def path_for(key: str) -> Path:
    return receipts_dir() / _relative(key)


def path_for_link(link: str) -> Path | None:
    """The file a RELATIVE Receipt Link (`/receipts/...`) points at, or None for any other link."""
    text = str(link or "").strip()
    if not text.startswith(LINK_PREFIX):
        return None
    try:
        return path_for(text[1:])
    except ReceiptStoreError:
        return None


def link_for(key: str) -> str:
    """The ledger-facing link: relative to the dashboard, so it never carries a host or a secret."""
    if not is_configured():
        return ""
    return LINK_PREFIX + _relative(key).as_posix()


def exists(key: str) -> bool:
    if not is_configured():
        _warn_once()
        return False
    try:
        return path_for(key).is_file()
    except ReceiptStoreError:
        return False


def put(key: str, body: bytes, ext: str) -> str:
    """Store one receipt and return its Receipt Link. `ext` is kept for the callers' shape; the
    key already names the extension."""
    if not is_configured():
        _warn_once()
        return ""
    if not body:
        raise ReceiptStoreError(f"Refusing to store an empty receipt at {key!r}")
    path = path_for(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(body)
        tmp.replace(path)
    except OSError as exc:
        raise ReceiptStoreError(f"Could not write {key!r} under {receipts_dir()}: {exc}") from exc
    # the order's earlier receipt under another extension (a PDF re-uploaded as a photo) goes, so
    # a replaced receipt never lingers as an orphan
    for other in path.parent.glob(path.stem + ".*"):
        if other != path and other.suffix != ".part" and other.is_file():
            try:
                other.unlink()
                log.info("Removed the replaced receipt %s", other.name)
            except OSError:
                pass
    log.info("Stored %s (%d bytes)", key, len(body))
    return link_for(key)


def delete(key: str) -> bool:
    """Remove a stored receipt. True if a file went."""
    try:
        path = path_for(key)
    except ReceiptStoreError:
        return False
    if path.is_file():
        path.unlink()
        return True
    return False


def stored_keys(only_retailer: str | None = None) -> list[str]:
    """Every stored receipt's key (`receipts/<retailer>/<month>/<file>`), sorted."""
    base = receipts_dir()
    if not base.is_dir():
        return []
    keys = []
    for path in base.rglob("*"):
        if not path.is_file() or path.name.endswith(".part"):
            continue
        rel = path.relative_to(base).as_posix()
        parts = rel.split("/")
        if len(parts) != 3 or parts[0].startswith("_"):
            continue  # not <retailer>/<month>/<file>: a probe, a stray
        if only_retailer and parts[0] != only_retailer:
            continue
        keys.append(KEY_PREFIX + rel)
    return sorted(keys)
