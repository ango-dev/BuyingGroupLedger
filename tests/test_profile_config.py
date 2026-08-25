"""Profile config + per-retailer auth round-trip.

`auth` carries the credentials the Best Buy paths use to log themselves back in — username and
password, the only supported method since 2026-08-13. It must survive save_profiles ->
load_profiles unchanged (that pair is how create_profile persists edits), and default to empty for
the many profiles that don't set it.
"""

import json

import pytest
from pydantic import ValidationError

import config.profiles as profiles_mod
from models.profile import ProfileConfig, ProxyConfig, RetailerAuth


class TestProxyAsUrl:
    """`as_url()` is what direct-HTTP retailers (Costco's API) hand to curl_cffi so their traffic
    leaves from the same static ISP IP the browser paths use."""

    def test_with_credentials(self):
        p = ProxyConfig(host="1.2.3.4", port=50100, username="user", password="pass")
        assert p.as_url() == "http://user:pass@1.2.3.4:50100"

    def test_without_credentials(self):
        assert ProxyConfig(host="1.2.3.4", port=8080).as_url() == "http://1.2.3.4:8080"

    def test_username_only(self):
        p = ProxyConfig(host="1.2.3.4", port=8080, username="user")
        assert p.as_url() == "http://user@1.2.3.4:8080"

    def test_special_characters_are_percent_encoded(self):
        # A password containing @ or : would otherwise split the URL in the wrong place.
        p = ProxyConfig(host="1.2.3.4", port=8080, username="u@ser", password="p:a@ss/word")
        assert p.as_url() == "http://u%40ser:p%3Aa%40ss%2Fword@1.2.3.4:8080"


def test_auth_defaults_to_empty():
    p = ProfileConfig(label="p", retailers=["amazon"])
    assert p.auth == {}


def test_auth_block_validates_from_json():
    p = ProfileConfig.model_validate(
        {
            "label": "profile-alpha",
            "retailers": ["bestbuy"],
            "auth": {"bestbuy": {"method": "password", "username": "me@example.com",
                                 "password": "hunter2"}},
        }
    )
    assert p.auth["bestbuy"].method == "password"
    assert p.auth["bestbuy"].username == "me@example.com"


def test_password_is_the_only_method():
    """Google SSO, Apple and TOTP were removed. Rejecting them at VALIDATION
    rather than ignoring them at runtime is the point: an old profiles.json still naming
    method="google" would otherwise load fine and then silently produce a sign-in block that no
    longer exists, i.e. a profile that can never log itself back in."""
    assert RetailerAuth().method == "password"
    for dropped in ("google", "apple"):
        with pytest.raises(ValidationError):
            RetailerAuth(method=dropped)


def test_the_totp_secret_is_configurable_again():
    """REVERSED 2026-08-25: 2-Step Verification is now REQUIRED on the Best Buy account, because
    leaving it off just meant Best Buy escalated to an identity challenge offering only "text me a
    code" — which nothing here can answer. An authenticator code is the one challenge the script CAN
    answer unattended, and it needs the enrolled seed. Pydantic ignores unknown fields, so a config
    carrying totp_secret against a model without it would look accepted while doing nothing."""
    assert "totp_secret" in RetailerAuth.model_fields
    assert RetailerAuth().totp_secret == "", "absent secret is blank, not None"


def test_neither_secret_shows_up_in_a_repr():
    """A profile gets printed in logs and diagnostics. The password was already hidden; the TOTP seed
    is strictly worse to leak, being a permanent key rather than one 30-second code."""
    text = repr(RetailerAuth(method="password", username="u@e.com",
                             password="hunter2-hunter2", totp_secret="JBSWY3DPEHPK3PXP"))
    assert "hunter2-hunter2" not in text
    assert "JBSWY3DPEHPK3PXP" not in text


def test_auth_round_trips_through_save_and_load(config_file):
    original = [
        ProfileConfig(
            label="profile-alpha",
            profile_id="pid",
            retailers=["bestbuy"],
            auth={"bestbuy": RetailerAuth(
                method="password", username="me@example.com", password="hunter2")},
        )
    ]

    profiles_mod.save_profiles(original)
    loaded = profiles_mod.load_profiles()

    assert len(loaded) == 1
    assert loaded[0].auth["bestbuy"].method == "password"
    assert loaded[0].auth["bestbuy"].username == "me@example.com"


# --- amazon vs amazon-business mutual exclusion --------------------------------------------------
def _write_profiles(config_file, entries):
    config_file(profiles=entries)


def test_profile_with_both_amazon_keys_is_rejected(config_file):
    _write_profiles(config_file, [
        {"label": "profile-oops", "retailers": ["amazon", "amazon-business"]},
    ])
    with pytest.raises(ValueError, match="profile-oops"):
        profiles_mod.load_profiles()


def test_separate_amazon_and_business_profiles_load_fine(config_file):
    _write_profiles(config_file, [
        {"label": "profile-bravo", "retailers": ["amazon"]},
        {"label": "profile-biz", "retailers": ["amazon-business"]},
    ])
    loaded = profiles_mod.load_profiles()
    assert [p.label for p in loaded] == ["profile-bravo", "profile-biz"]


def test_amazon_alongside_other_retailers_is_fine(config_file):
    # amazon + bestbuy on one profile is legitimate (different sites, different accounts allowed).
    _write_profiles(config_file, [
        {"label": "profile-multi", "retailers": ["amazon", "bestbuy", "costco"]},
    ])
    loaded = profiles_mod.load_profiles()
    assert loaded[0].retailers == ["amazon", "bestbuy", "costco"]
