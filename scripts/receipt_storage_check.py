"""Prove the receipt storage path end to end, in one command. Costs nothing but a few HTTP calls.

    python -m scripts.receipt_storage_check

No browser, no scrape, no Browser-Use fee. It writes ONE tiny probe object and deletes it again, so
it is safe to run against the production bucket any time.

WHY IT EXISTS. Receipt storage has two INDEPENDENT credentials that fail in opposite directions and
produce opposite symptoms, which makes hand-diagnosis slow (it took a dozen ad-hoc probes the first
time, 2026-08-15):

  - the CUSTOMER SECRET KEY writes objects. Wrong -> uploads fail, but every link already on the
    sheet keeps working, so the ledger looks healthy while silently stopping collecting receipts.
  - the PAR reads them. Expired or revoked -> uploads keep succeeding while every Receipt Link on
    the sheet 404s, which nothing else would ever tell you.

So this checks them SEPARATELY and says which one is broken, rather than reporting "receipts are
down". Run it after setting up, after rotating either credential, and after moving hosts.
"""

from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request

from receipts import store

PROBE_KEY = "receipts/_selftest/storage-check.txt"
PROBE_BODY = b"buying-group-ledger storage check\n"


def _mask(url: str) -> str:
    """Hide the PAR secret so this can be pasted into an issue or a chat."""
    if "/p/" not in url:
        return url
    head, rest = url.split("/p/", 1)
    secret, _, tail = rest.partition("/")
    return f"{head}/p/<{len(secret)}-char-secret>/{tail}"


def _explain(exc: Exception) -> str:
    """Turn OCI's genuinely misleading auth errors into the thing to actually go and check."""
    text = str(exc)
    if "SignatureDoesNotMatch" in text or "secret key required" in text:
        return (
            "The ACCESS KEY is not recognized by this tenancy.\n"
            "      OCI's own hint about regions is a red herring — this is the key, not the region.\n"
            "      - OCI_S3_ACCESS_KEY_ID must be the 40-character lowercase HEX 'Access key', NOT\n"
            "        the credential's OCID (which starts 'ocid1.credential.oc1..').\n"
            "      - The secret is displayed ONCE, at creation. If it was not captured then, the\n"
            "        pair cannot be recovered — delete the key and generate a new one.\n"
            "      - Both must come from a user in the tenancy owning this bucket's namespace.\n"
            "      Console: Identity & Security -> Domains -> your domain -> Users -> your user\n"
            "               -> Customer secret keys"
        )
    if "NotAuthorized" in text or "Unauthorized" in text:
        return ("The credentials are recognized but lack permission. Add an IAM policy such as\n"
                "      `allow group <your-group> to manage objects in compartment <name>`.")
    if "NoSuchBucket" in text or "BucketNotFound" in text:
        return "OCI_BUCKET does not exist in this namespace/region."
    return "Check OCI_S3_ENDPOINT_URL, OCI_S3_REGION and the bucket name."


def main() -> int:
    # The detail strings contain em-dashes, and a legacy Windows console (cp1252) mangles or
    # raises on those. scripts/preflight.py does the same: a diagnostic that crashes while
    # diagnosing -- or prints mojibake over the one line telling you what to fix -- is worse
    # than useless.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - not every stream supports it (pytest, pipes)
            pass

    print("Receipt storage check\n" + "=" * 60)

    if not store.is_configured():
        missing = store.missing_settings()
        if missing:
            print("NOT CONFIGURED — unset:", ", ".join(missing))
        else:
            print("DISABLED via RECEIPT_CAPTURE_ENABLED.")
        print("\nReceipt capture is inert: orders record normally with a blank Receipt Link.")
        return 1

    bucket = store.settings.oci_bucket
    print(f"bucket    {bucket}")
    print(f"endpoint  {store.settings.oci_s3_endpoint_url}")
    print(f"region    {store.settings.oci_s3_region}")
    print(f"PAR       {_mask(store.settings.oci_par_url_prefix)}\n")

    # --- 1. THE PAR, on its own. A 404 for an absent object proves the PAR, bucket, namespace and
    # region are all good WITHOUT involving the secret key at all.
    absent = f"{store.settings.oci_par_url_prefix.rstrip('/')}/receipts/_selftest/{time.time()}.none"
    try:
        urllib.request.urlopen(absent, timeout=30)
        print("[?? ] PAR      unexpected 200 for an object that should not exist")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print("[ OK ] PAR      valid (404 for an absent object = authenticated, nothing there)")
        else:
            print(f"[FAIL] PAR      HTTP {exc.code} — the PAR is rejected, so every Receipt Link on "
                  f"the sheet is dead.\n       Recreate it: bucket -> Pre-Authenticated Requests -> "
                  f"Create (Target: Bucket, Permit object reads).")
            return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] PAR      unreachable: {type(exc).__name__}: {exc}")
        return 1

    # --- 2. The customer secret key: write, then confirm it is really there.
    try:
        link = store.put(PROBE_KEY, PROBE_BODY, "pdf")
        print("[ OK ] upload   wrote the probe object")
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] upload   {exc}\n\n      {_explain(exc)}")
        return 1

    try:
        found = store.exists(PROBE_KEY)
        print(f"[{' OK ' if found else 'FAIL'}] exists   {found}"
              + ("" if found else "   — the upload reported success but the object is not there"))
        if not found:
            return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] exists   {exc}")
        return 1

    # --- 3. The whole point: the link the sheet will hold must actually serve the bytes.
    try:
        with urllib.request.urlopen(link, timeout=30) as resp:
            body = resp.read()
        ok = body == PROBE_BODY
        print(f"[{' OK ' if ok else 'FAIL'}] fetch    HTTP {resp.status}, {len(body)} bytes, "
              f"content matches: {ok}")
        if not ok:
            return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] fetch    the uploaded object is not readable through the PAR: {exc}\n"
              f"       Uploads work but every Receipt Link would 404.")
        return 1

    # --- 4. Leave the bucket as we found it.
    try:
        store._s3().delete_object(Bucket=bucket, Key=PROBE_KEY)
        print("[ OK ] cleanup  probe object removed")
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] cleanup  could not delete {PROBE_KEY}: {exc}")

    print("\nReceipt storage is working end to end — uploads AND the sheet-facing links.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
