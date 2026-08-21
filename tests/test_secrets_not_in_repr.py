"""No credential may appear in an object's repr.

Found 2026-08-21: a test traceback printed the OCI PAR secret in full, because `Settings` is a
dataclass and its repr lists every field. That object is reachable from nearly every module, so any
unhandled exception carrying it into `logs/run.log` — or into an ALERT EMAIL, which is worse, since
that leaves the host — would have published every API key, the Best Buy password and the PAR URL.

The values are still read normally as attributes; only the repr is masked. This test exists because
the failure is invisible until the day something raises, and by then the secret is already in a log
someone may have pasted somewhere.
"""

import dataclasses

import pytest

from config.settings import Settings, settings
from models.profile import ProfileConfig, ProxyConfig, RetailerAuth

# Anything whose value grants access to money, an account, or the receipts. Not merely "private":
# `gmail_address` and `maxoutdeals_email` are identifiers that appear in normal log lines by design,
# and hiding them would make ordinary debugging harder without protecting anything.
SECRET_FIELDS = (
    "gmail_app_password",
    "discord_webhook_url",       # anyone holding it can post to the channel
    "bfmr_api_key",
    "bfmr_api_secret",
    "maxoutdeals_api_key",
    "oci_s3_access_key_id",
    "oci_s3_secret_access_key",
    "oci_par_url_prefix",        # grants read of EVERY stored receipt
)


class TestSettings:
    @pytest.mark.parametrize("name", SECRET_FIELDS)
    def test_the_field_is_excluded_from_the_repr(self, name):
        field = next(f for f in dataclasses.fields(Settings) if f.name == name)

        assert field.repr is False, f"{name} would be printed by any traceback carrying Settings"

    def test_a_populated_secret_does_not_appear_in_the_repr(self):
        """The field-level check above could pass while the repr still leaked, so assert the value."""
        loaded = dataclasses.replace(
            settings,
            bfmr_api_key="SECRET-BFMR-KEY",
            oci_par_url_prefix="https://oci/p/SECRET-PAR-TOKEN/n/ns/b/bkt/o",
            gmail_app_password="SECRET-GMAIL-PW",
        )
        text = repr(loaded)

        for secret in ("SECRET-BFMR-KEY", "SECRET-PAR-TOKEN", "SECRET-GMAIL-PW"):
            assert secret not in text

    def test_the_values_are_still_readable(self):
        """Masking the repr must not break reading them — that is the whole point of the object."""
        loaded = dataclasses.replace(settings, bfmr_api_key="abc")

        assert loaded.bfmr_api_key == "abc"

    def test_non_secret_settings_are_still_shown(self):
        """Over-masking would make a traceback useless for diagnosing a real misconfiguration."""
        text = repr(dataclasses.replace(settings, google_sheet_worksheet_name="Orders"))

        assert "Orders" in text


class TestProfile:
    """A ProfileConfig is passed to nearly every scraper, so its repr reaches the most places."""

    def test_the_retailer_password_is_not_in_the_repr(self):
        profile = ProfileConfig(label="p", profile_id="x",
                                auth={"bestbuy": RetailerAuth(username="e", password="ACCOUNT-PW")})

        assert "ACCOUNT-PW" not in repr(profile)

    def test_the_proxy_password_is_not_in_the_repr(self):
        profile = ProfileConfig(label="p", profile_id="x",
                                proxy=ProxyConfig(host="h", port=1, username="u",
                                                  password="PROXY-PW"))

        assert "PROXY-PW" not in repr(profile)

    def test_the_proxy_url_still_carries_the_password(self):
        """as_url is what direct-HTTP clients authenticate with; masking the repr must not touch it."""
        proxy = ProxyConfig(host="h", port=8080, username="u", password="PROXY-PW")

        assert proxy.as_url() == "http://u:PROXY-PW@h:8080"

    def test_the_label_is_still_shown(self):
        """Every log line identifies a run by profile label; hiding it would break diagnosis."""
        assert "profile-alpha" in repr(ProfileConfig(label="profile-alpha", profile_id="x"))
