"""OCI Object Storage through its S3 Compatibility API — the three calls receipt capture needs.

    exists(key)   -> is this order's receipt already stored? (decides whether a browser opens at all)
    put(key, ...) -> upload one rendered receipt
    link_for(key) -> the URL that goes in the sheet's Receipt Link column

WHY BOTO3 AGAINST A CUSTOM ENDPOINT. OCI exposes an S3-compatible endpoint at
`https://<namespace>.compat.objectstorage.<region>.oraclecloud.com`, which boto3 speaks unchanged
given `endpoint_url` and a customer secret key. That keeps the whole upload path a standard S3
client with no OCI SDK, no request-signing code, and no second dependency.

WHY THE LINK IS BUILT BY STRING CONCATENATION AND NOT SIGNED. A Pre-Authenticated Request is an OCI
NATIVE concept — it cannot be created through the S3 API, which is why there is no `create_par()`
here. `generate_presigned_url` does work against the compat endpoint, but SigV4 presigned URLs
expire after at most 7 days, and a ledger row is read months later. So the deployment creates ONE
read PAR by hand in the console (Target: Bucket; Access type: Permit object reads; object listing
left OFF, since receipts are PII and listing would let the URL's holder enumerate every order) and
puts its URL in OCI_PAR_URL_PREFIX; every object's link is that prefix plus the object key. One
manual step buys a link that does not rot, and revoking it is one click.

("Objects with prefix" is a separate PAR target type and also works, but its URL already ends in the
prefix while link_for appends the full key — so that value must be trimmed back to the `/o` part.)

INERT WHEN UNCONFIGURED. A blank OCI_BUCKET makes every method a no-op that raises nothing, so a
host without a bucket records orders exactly as before with a blank Receipt Link — receipts are
additive, and losing an order to a storage misconfiguration would be a far worse trade.
"""

from __future__ import annotations

import logging
import threading

from config.settings import settings
from receipts.sources import CONTENT_TYPES

log = logging.getLogger(__name__)

_client = None
_client_lock = threading.Lock()
_warned = False


class ReceiptStoreError(Exception):
    """The object store is configured but could not be used."""


def is_configured() -> bool:
    """Is receipt capture switched on AND given everything it needs to store something?

    All-or-nothing on purpose. A half-filled config (a bucket but no PAR prefix) would upload
    objects and then write a broken link — or no link — into the ledger, which reads as "this order
    has no receipt" while quietly billing bandwidth to produce one every run.
    """
    return bool(
        settings.receipt_capture_enabled
        and settings.oci_bucket
        and settings.oci_s3_endpoint_url
        and settings.oci_s3_access_key_id
        and settings.oci_s3_secret_access_key
        and settings.oci_par_url_prefix
    )


def missing_settings() -> list[str]:
    """Which OCI settings are blank — for preflight, so a partial config is named, not guessed at."""
    required = {
        "OCI_BUCKET": settings.oci_bucket,
        "OCI_S3_ENDPOINT_URL": settings.oci_s3_endpoint_url,
        "OCI_S3_ACCESS_KEY_ID": settings.oci_s3_access_key_id,
        "OCI_S3_SECRET_ACCESS_KEY": settings.oci_s3_secret_access_key,
        "OCI_PAR_URL_PREFIX": settings.oci_par_url_prefix,
    }
    return [name for name, value in required.items() if not str(value or "").strip()]


def _warn_once() -> None:
    global _warned
    if not _warned:
        _warned = True
        if not settings.receipt_capture_enabled:
            log.info("Receipt capture is disabled (RECEIPT_CAPTURE_ENABLED).")
        else:
            log.info(
                "Receipt capture is not configured (%s unset); orders record normally with a blank "
                "Receipt Link.", ", ".join(missing_settings()) or "OCI settings",
            )


def _s3():
    """The boto3 S3 client, built once per process.

    boto3 is imported HERE rather than at module scope for the same reason curl_cffi and PyJWT are
    lazy: `import main` and the entire offline test suite must not require it. An ImportError is
    turned into ReceiptStoreError so the caller reports "receipts unavailable" instead of a bare
    dependency traceback.
    """
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise ReceiptStoreError(
                "boto3 is not installed, so receipts cannot be uploaded (`pip install -r "
                "requirements.txt`). Orders are still recorded; only the Receipt Link is lost."
            ) from exc

        _client = boto3.client(
            "s3",
            endpoint_url=settings.oci_s3_endpoint_url,
            aws_access_key_id=settings.oci_s3_access_key_id,
            aws_secret_access_key=settings.oci_s3_secret_access_key,
            # OCI's compat endpoint is namespace-scoped and does NOT serve virtual-host-style bucket
            # subdomains, so path-style addressing is required rather than merely safer. SigV4 is
            # what it authenticates with.
            region_name=settings.oci_s3_region or "us-ashburn-1",
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        return _client


def _reset_client_for_tests() -> None:
    """Drop the cached client so a test can re-point the settings. Not used in production."""
    global _client, _warned
    _client = None
    _warned = False


def exists(key: str) -> bool:
    """Is an object already stored under `key`?

    This is what makes capture idempotent AND cheap: it runs before any browser is created, so a
    routine re-check run — where every order already has its receipt — answers True for all of them
    and never opens a paid cloud browser at all.

    A missing object is False; anything else (permissions, a wrong bucket, a network failure) is
    raised, because silently answering "not there" would re-render and re-upload every order on
    every run.
    """
    if not is_configured():
        _warn_once()
        return False
    client = _s3()
    try:
        client.head_object(Bucket=settings.oci_bucket, Key=key)
        return True
    except Exception as exc:  # noqa: BLE001 — botocore's exception classes are built at runtime
        if _is_not_found(exc):
            return False
        raise ReceiptStoreError(f"Could not check {key!r} in {settings.oci_bucket!r}: {exc}") from exc


def _is_not_found(exc: Exception) -> bool:
    """Distinguish 'no such object' from every other failure.

    head_object signals a miss as a 404 ClientError, and botocore builds those exception classes at
    runtime, so this reads the response rather than catching a named type.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    code = str(response.get("Error", {}).get("Code", ""))
    return status == 404 or code in {"404", "NoSuchKey", "NotFound"}


def put(key: str, body: bytes, ext: str) -> str:
    """Upload one receipt and return its Receipt Link URL."""
    if not is_configured():
        _warn_once()
        return ""
    if not body:
        raise ReceiptStoreError(f"Refusing to store an empty object at {key!r}")
    client = _s3()
    try:
        client.put_object(
            Bucket=settings.oci_bucket,
            Key=key,
            Body=body,
            ContentType=CONTENT_TYPES.get(ext.lstrip("."), "application/octet-stream"),
        )
    except Exception as exc:  # noqa: BLE001
        raise ReceiptStoreError(f"Could not upload {key!r} to {settings.oci_bucket!r}: {exc}") from exc
    log.info("Stored receipt %s (%d bytes)", key, len(body))
    return link_for(key)


def link_for(key: str) -> str:
    """The sheet-facing URL for a stored object: the PAR prefix joined to the object key.

    Joined defensively because a PAR URL copied out of the OCI console may or may not carry its
    trailing slash, and getting that wrong yields a 404 link on every row rather than an error
    anyone would notice.
    """
    if not is_configured():
        return ""
    return f"{settings.oci_par_url_prefix.rstrip('/')}/{key.lstrip('/')}"
