"""The object-storage half: staying inert when unconfigured, and telling a miss from a failure.

No boto3 is installed for these — a fake client is injected — which is also the point: the whole
upload path is lazily imported so `import main` and this suite stay free of the dependency.
"""

import pytest

from receipts import store


@pytest.fixture(autouse=True)
def _clean_client():
    """Every test re-points the settings, so the cached client must not leak between them."""
    store._reset_client_for_tests()
    yield
    store._reset_client_for_tests()


def _configure(monkeypatch, **overrides):
    """Swap in a fully-populated Settings. `Settings` is a frozen dataclass — deliberately, so a
    credential can't be mutated mid-run — so this replaces the object rather than its fields."""
    import dataclasses

    values = {
        "receipt_capture_enabled": True,
        "oci_bucket": "ledger-receipts",
        "oci_s3_endpoint_url": "https://ns.compat.objectstorage.us-ashburn-1.oraclecloud.com",
        "oci_s3_region": "us-ashburn-1",
        "oci_s3_access_key_id": "AKIA",
        "oci_s3_secret_access_key": "secret",
        "oci_par_url_prefix": "https://objectstorage.us-ashburn-1.oraclecloud.com/p/tok/n/ns/b/b/o",
    }
    values.update(overrides)
    monkeypatch.setattr(store, "settings", dataclasses.replace(store.settings, **values))


class FakeS3:
    """Records calls; raises botocore-shaped errors so _is_not_found is exercised for real."""

    def __init__(self, present=(), head_error=None):
        self.present = set(present)
        self.head_error = head_error
        self.puts = []

    def head_object(self, Bucket, Key):  # noqa: N803 — boto3's own kwarg spelling
        if self.head_error is not None:
            raise self.head_error
        if Key not in self.present:
            raise _client_error(404, "404")
        return {"ContentLength": 1}

    def put_object(self, Bucket, Key, Body, ContentType):  # noqa: N803
        self.puts.append({"Bucket": Bucket, "Key": Key, "Body": Body, "ContentType": ContentType})
        return {}


def _client_error(status, code):
    exc = Exception("boom")
    exc.response = {"ResponseMetadata": {"HTTPStatusCode": status}, "Error": {"Code": code}}
    return exc


class TestInertWhenUnconfigured:
    """Receipts are additive. A host with no bucket must record orders exactly as it did before —
    blank Receipt Link, no exception, no browser, nothing to notice."""

    def test_a_blank_bucket_disables_everything(self, monkeypatch):
        _configure(monkeypatch, oci_bucket="")

        assert not store.is_configured()
        assert store.exists("receipts/amazon/2026-08/1.pdf") is False
        assert store.put("receipts/amazon/2026-08/1.pdf", b"x", "pdf") == ""
        assert store.link_for("receipts/amazon/2026-08/1.pdf") == ""

    def test_a_partial_config_is_treated_as_unconfigured_not_half_working(self, monkeypatch):
        """A bucket without a PAR prefix would upload objects and then write no link — paying to
        render receipts nobody can reach, on every run, forever."""
        _configure(monkeypatch, oci_par_url_prefix="")

        assert not store.is_configured()
        assert store.missing_settings() == ["OCI_PAR_URL_PREFIX"]

    def test_the_off_switch_leaves_credentials_alone(self, monkeypatch):
        _configure(monkeypatch, receipt_capture_enabled=False)

        assert not store.is_configured()
        assert store.missing_settings() == []  # nothing is MISSING; it is switched off

    def test_missing_settings_names_every_blank_one(self, monkeypatch):
        _configure(monkeypatch, oci_bucket="", oci_s3_access_key_id="")

        assert store.missing_settings() == ["OCI_BUCKET", "OCI_S3_ACCESS_KEY_ID"]


class TestExists:
    def test_a_stored_object_is_found(self, monkeypatch):
        _configure(monkeypatch)
        monkeypatch.setattr(store, "_s3", lambda: FakeS3(present={"receipts/a/2026-08/1.pdf"}))

        assert store.exists("receipts/a/2026-08/1.pdf") is True

    def test_a_404_is_a_miss_not_an_error(self, monkeypatch):
        _configure(monkeypatch)
        monkeypatch.setattr(store, "_s3", lambda: FakeS3())

        assert store.exists("receipts/a/2026-08/1.pdf") is False

    def test_any_other_failure_raises_rather_than_reporting_a_miss(self, monkeypatch):
        """Reporting 'not there' on a permissions or network failure would silently re-render and
        re-upload every order on every run — a recurring bandwidth bill with no symptom."""
        _configure(monkeypatch)
        monkeypatch.setattr(store, "_s3", lambda: FakeS3(head_error=_client_error(403, "AccessDenied")))

        with pytest.raises(store.ReceiptStoreError):
            store.exists("receipts/a/2026-08/1.pdf")

    def test_a_non_botocore_exception_also_raises(self, monkeypatch):
        _configure(monkeypatch)
        monkeypatch.setattr(store, "_s3", lambda: FakeS3(head_error=RuntimeError("dns")))

        with pytest.raises(store.ReceiptStoreError):
            store.exists("receipts/a/2026-08/1.pdf")


class TestPut:
    def test_the_object_and_its_content_type_are_sent(self, monkeypatch):
        _configure(monkeypatch)
        fake = FakeS3()
        monkeypatch.setattr(store, "_s3", lambda: fake)

        link = store.put("receipts/amazon/2026-08/1.pdf", b"%PDF-1.4", "pdf")

        assert fake.puts == [{
            "Bucket": "ledger-receipts", "Key": "receipts/amazon/2026-08/1.pdf",
            "Body": b"%PDF-1.4", "ContentType": "application/pdf",
        }]
        assert link.endswith("/receipts/amazon/2026-08/1.pdf")

    def test_a_png_gets_the_image_content_type(self, monkeypatch):
        _configure(monkeypatch)
        fake = FakeS3()
        monkeypatch.setattr(store, "_s3", lambda: fake)

        store.put("receipts/amazon/2026-08/1.png", b"\x89PNG", "png")

        assert fake.puts[0]["ContentType"] == "image/png"

    def test_an_empty_body_is_refused(self, monkeypatch):
        """A zero-byte object still makes exists() true, so it would permanently mask a real receipt."""
        _configure(monkeypatch)
        monkeypatch.setattr(store, "_s3", lambda: FakeS3())

        with pytest.raises(store.ReceiptStoreError):
            store.put("receipts/amazon/2026-08/1.pdf", b"", "pdf")

    def test_an_upload_failure_is_wrapped(self, monkeypatch):
        _configure(monkeypatch)

        class Exploding(FakeS3):
            def put_object(self, **kw):
                raise RuntimeError("bucket is full")

        monkeypatch.setattr(store, "_s3", lambda: Exploding())

        with pytest.raises(store.ReceiptStoreError, match="Could not upload"):
            store.put("receipts/amazon/2026-08/1.pdf", b"x", "pdf")


class TestLinkFor:
    """The sheet link is string concatenation because a PAR cannot be minted through the S3 API, and
    a boto3 presigned URL expires within 7 days while a ledger row is read months later."""

    def test_a_par_prefix_without_a_trailing_slash(self, monkeypatch):
        _configure(monkeypatch, oci_par_url_prefix="https://oci/p/tok/n/ns/b/bkt/o")

        assert store.link_for("receipts/a/1.pdf") == "https://oci/p/tok/n/ns/b/bkt/o/receipts/a/1.pdf"

    def test_a_par_prefix_with_a_trailing_slash_does_not_double_it(self, monkeypatch):
        """Copying the PAR out of the OCI console may or may not include the slash, and a double
        slash yields a 404 on every row rather than an error anyone would see."""
        _configure(monkeypatch, oci_par_url_prefix="https://oci/p/tok/n/ns/b/bkt/o/")

        assert store.link_for("receipts/a/1.pdf") == "https://oci/p/tok/n/ns/b/bkt/o/receipts/a/1.pdf"

    def test_a_leading_slash_on_the_key_is_absorbed_too(self, monkeypatch):
        _configure(monkeypatch, oci_par_url_prefix="https://oci/o/")

        assert store.link_for("/receipts/a/1.pdf") == "https://oci/o/receipts/a/1.pdf"


class TestLazyImport:
    def test_boto3_is_not_imported_at_module_scope(self):
        """Same rule curl_cffi and PyJWT follow: `import main` and this suite must not need it, or a
        missing dependency turns into an import-time crash on every run instead of a degraded one."""
        import ast
        import pathlib

        source = pathlib.Path(store.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        top_level = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        names = {a.name.split(".")[0] for n in top_level if isinstance(n, ast.Import) for a in n.names}
        names |= {(n.module or "").split(".")[0] for n in top_level if isinstance(n, ast.ImportFrom)}

        assert "boto3" not in names and "botocore" not in names
