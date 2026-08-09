"""Profile config + per-retailer auth round-trip.

`auth` carries the auto-auth config the Best Buy scraper reads to log itself back in via Google.
It must survive save_profiles -> load_profiles unchanged (that pair is how create_profile persists
edits), and default to empty for the many profiles that don't set it.
"""

import config.profiles as profiles_mod
from models.profile import ProfileConfig, RetailerAuth


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
