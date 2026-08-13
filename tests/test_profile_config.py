"""Profile config + per-retailer auth round-trip.

`auth` carries the auto-auth config the Best Buy scraper reads to log itself back in via Google.
It must survive save_profiles -> load_profiles unchanged (that pair is how create_profile persists
edits), and default to empty for the many profiles that don't set it.
"""

import json

import pytest

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
            "auth": {"bestbuy": {"method": "google", "google_email": "you@gmail.com"}},
        }
    )
    assert p.auth["bestbuy"].method == "google"
    assert p.auth["bestbuy"].google_email == "you@gmail.com"


def test_retailer_auth_method_defaults_to_google():
    assert RetailerAuth().method == "google"


def test_auth_round_trips_through_save_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles_mod, "PROFILES_FILE", tmp_path / "profiles.json")
    original = [
        ProfileConfig(
            label="profile-alpha",
            profile_id="pid",
            retailers=["bestbuy"],
            auth={"bestbuy": RetailerAuth(method="google", google_email="you@gmail.com")},
        )
    ]

    profiles_mod.save_profiles(original)
    loaded = profiles_mod.load_profiles()

    assert len(loaded) == 1
    assert loaded[0].auth["bestbuy"].method == "google"
    assert loaded[0].auth["bestbuy"].google_email == "you@gmail.com"


# --- amazon vs amazon-business mutual exclusion --------------------------------------------------
def _write_profiles(tmp_path, monkeypatch, entries):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    monkeypatch.setattr(profiles_mod, "PROFILES_FILE", path)


def test_profile_with_both_amazon_keys_is_rejected(tmp_path, monkeypatch):
    _write_profiles(tmp_path, monkeypatch, [
        {"label": "profile-oops", "retailers": ["amazon", "amazon-business"]},
    ])
    with pytest.raises(ValueError, match="profile-oops"):
        profiles_mod.load_profiles()


def test_separate_amazon_and_business_profiles_load_fine(tmp_path, monkeypatch):
    _write_profiles(tmp_path, monkeypatch, [
        {"label": "profile-bravo", "retailers": ["amazon"]},
        {"label": "profile-biz", "retailers": ["amazon-business"]},
    ])
    loaded = profiles_mod.load_profiles()
    assert [p.label for p in loaded] == ["profile-bravo", "profile-biz"]


def test_amazon_alongside_other_retailers_is_fine(tmp_path, monkeypatch):
    # amazon + bestbuy on one profile is legitimate (different sites, different accounts allowed).
    _write_profiles(tmp_path, monkeypatch, [
        {"label": "profile-multi", "retailers": ["amazon", "bestbuy", "costco"]},
    ])
    loaded = profiles_mod.load_profiles()
    assert loaded[0].retailers == ["amazon", "bestbuy", "costco"]
