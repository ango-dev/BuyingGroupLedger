"""One config file, with environment variables as the override.

Setup used to mean six sources — `.env`, `profiles.json`, `warehouses.json`, `cards.json`,
`service_account.json` and `.costco/<label>.json`. It is now `config.json` (what you author) plus
`.state.json` (what the app rewrites), and the environment overrides anything in the config file.

These tests pin the resolution order and the two properties that make the split safe: comments
survive a save, and a value that only exists in the config file is still found.
"""

import json

import pytest

from config import loader
from config.settings import ENV_TO_CONFIG, _get_bool, _get_float, _get_int, _get_rate, _get_str


class TestResolutionOrder:
    """environment -> config.json -> the default in settings.py, for every type of value."""

    def test_the_config_file_beats_the_hardcoded_default(self, config_file, monkeypatch):
        monkeypatch.delenv("LOOKBACK_DAYS", raising=False)
        config_file(scraping={"lookback_days": 7})
        assert _get_int("LOOKBACK_DAYS", 1) == 7

    def test_the_environment_beats_the_config_file(self, config_file, monkeypatch):
        config_file(scraping={"lookback_days": 7})
        monkeypatch.setenv("LOOKBACK_DAYS", "3")
        assert _get_int("LOOKBACK_DAYS", 1) == 3

    def test_the_default_applies_when_neither_is_set(self, config_file, monkeypatch):
        monkeypatch.delenv("LOOKBACK_DAYS", raising=False)
        config_file()
        assert _get_int("LOOKBACK_DAYS", 1) == 1

    def test_a_blank_environment_variable_does_not_shadow_the_config(self, config_file, monkeypatch):
        """`FOO=` in a .env is how people comment a value out, not how they mean "empty" — so it
        must fall through rather than blanking a perfectly good config value."""
        config_file(google={"sheet_id": "from-config"})
        monkeypatch.setenv("GOOGLE_SHEET_ID", "")
        assert _get_str("GOOGLE_SHEET_ID") == "from-config"

    def test_a_zero_in_the_config_is_a_value_not_an_absence(self, config_file, monkeypatch):
        """0 is falsy, and the insurance floor is a number where 0 MEANS something — insure
        everything. Reading it as "unset" would silently apply a different policy."""
        monkeypatch.delenv("BFMR_MIN_INSURANCE_VALUE", raising=False)
        config_file(buying_groups={"bfmr": {"min_insurance_value": 0}})
        assert _get_float("BFMR_MIN_INSURANCE_VALUE", 500.0) == 0.0


class TestBooleansFailClosed:
    """A switch that spends money must fail closed in EITHER source, not just the string one.

    JSON has a real boolean so config.json can say `true`, while an environment variable is always a
    string. Both go through the same affirmative-spellings test, so a typo cannot enable spending
    from whichever source happens to be typed.
    """

    @pytest.mark.parametrize("value", [True, "true", "1", "yes", "on", "TRUE"])
    def test_affirmative_spellings_are_true(self, config_file, monkeypatch, value):
        monkeypatch.delenv("BUYING_GROUP_SYNC_ENABLED", raising=False)
        config_file(buying_groups={"sync_enabled": value})
        assert _get_bool("BUYING_GROUP_SYNC_ENABLED", False) is True

    @pytest.mark.parametrize("value", [False, "false", "no", "off", "ture", "enabled", "0"])
    def test_everything_else_including_a_typo_is_false(self, config_file, monkeypatch, value):
        monkeypatch.delenv("BUYING_GROUP_SYNC_ENABLED", raising=False)
        config_file(buying_groups={"sync_enabled": value})
        assert _get_bool("BUYING_GROUP_SYNC_ENABLED", False) is False

    def test_the_environment_can_turn_it_off_again(self, config_file, monkeypatch):
        config_file(buying_groups={"sync_enabled": True})
        monkeypatch.setenv("BUYING_GROUP_SYNC_ENABLED", "false")
        assert _get_bool("BUYING_GROUP_SYNC_ENABLED", False) is False


class TestRates:
    """Getting a rate wrong by 100x misstates every profit number on the sheet."""

    @pytest.mark.parametrize("written, expected", [(0.02, 0.02), ("2%", 0.02), ("0.02", 0.02)])
    def test_both_spellings_resolve_from_the_config_file(
        self, config_file, monkeypatch, written, expected
    ):
        monkeypatch.delenv("DEFAULT_CASHBACK_RATE", raising=False)
        config_file(scraping={"default_cashback_rate": written})
        assert _get_rate("DEFAULT_CASHBACK_RATE", 0.0) == expected

    def test_a_bare_number_above_one_is_rejected_from_the_config_too(self, config_file, monkeypatch):
        """"2" reads equally as 2% or 200%, so it is refused wherever it is written — the file is
        no safer a place to be ambiguous than the environment."""
        monkeypatch.delenv("DEFAULT_CASHBACK_RATE", raising=False)
        config_file(scraping={"default_cashback_rate": 2})
        with pytest.raises(ValueError, match="outside 0-1"):
            _get_rate("DEFAULT_CASHBACK_RATE", 0.0)


class TestEveryVariableHasAHome:
    def test_the_mapping_covers_every_variable_the_settings_read(self):
        """ENV_TO_CONFIG is the single source both settings.py and migrate_config.py use. A variable
        missing from it raises KeyError at import — this asserts the table stays complete."""
        import config.settings as settings_module

        for name in ENV_TO_CONFIG:
            assert settings_module._config_path(name)

    def test_the_migrator_reads_the_same_table(self):
        """A separate table in the migrator would drift, and a migrated value would land somewhere
        the settings never look — invisible until something quietly used its default."""
        from scripts.migrate_config import ENV_TO_CONFIG as migrator_table

        assert migrator_table is ENV_TO_CONFIG


class TestCommentsSurvive:
    """JSON has no comments, so the config file uses `"// note"` keys."""

    def test_comment_keys_are_stripped_before_a_model_sees_them(self, config_file):
        """`insurance_addresses` is typed `dict[str, InsuranceAddress]`, so a comment beside the
        addresses would fail validation."""
        config_file(warehouses=[{
            "buying_group": "BFMR",
            "// note": "why these exist",
            "insurance_addresses": {
                "// note": "state is NH, not US-NH",
                "sample": {"address_1": "13 Sample Drive", "city": "Testville",
                              "state": "NH", "country": "USA", "zip": "03050-0000"},
            },
            "jigs": [{"label": "j", "zip": "03050", "insure_as": "sample"}],
        }])
        from config.warehouses import load_warehouses

        warehouse = load_warehouses()[0]
        assert sorted(warehouse.insurance_addresses) == ["sample"]

    def test_saving_profiles_preserves_comments_key_order_and_other_sections(self, config_file):
        """`create_profile` writes a profile_id back into the file a human authored. If that
        regenerated the file, every comment and every unrelated section would be lost.
        """
        path = config_file()
        path.write_text(json.dumps({
            "//": "top-level note",
            "google": {"sheet_id": "keep-me"},
            "profiles": [{"label": "p", "retailers": ["amazon"]}],
            "cards": [{"last4": "4321", "name": "Freedom"}],
        }, indent=2), encoding="utf-8")
        loader.reload_config()

        from config.profiles import load_profiles, save_profiles

        profiles = load_profiles()
        profiles[0].profile_id = "assigned-by-browser-use"
        save_profiles(profiles)

        written = json.loads(path.read_text(encoding="utf-8"))
        assert written["//"] == "top-level note", "the comment survived"
        assert written["google"] == {"sheet_id": "keep-me"}, "an unrelated section survived"
        assert written["cards"], "another unrelated section survived"
        assert written["profiles"][0]["profile_id"] == "assigned-by-browser-use"
        assert list(written) == ["//", "google", "profiles", "cards"], "key order survived"


class TestMalformedConfig:
    def test_a_typo_in_the_config_raises_rather_than_reading_as_absent(self, config_file):
        """The quiet failure this prevents: an empty config looks exactly like a fresh install, so
        the run would scrape nothing and exit 0 rather than stopping."""
        path = config_file()
        path.write_text("{not json", encoding="utf-8")
        loader.reload_config()

        with pytest.raises(ValueError, match="not valid JSON"):
            loader.load_config()

        # Leave a readable file behind: reload_config() only drops the cache, so a bad file would
        # otherwise raise from an unrelated test's setup.
        path.write_text("{}", encoding="utf-8")
        loader.reload_config()

    def test_an_absent_file_is_simply_empty(self, config_file, tmp_path, monkeypatch):
        """Absent is not an error — a first run before anything is configured must still import."""
        monkeypatch.setattr(loader, "CONFIG_FILE", tmp_path / "nope.json")
        loader.reload_config()
        assert loader.load_config() == {}


class TestStateIsSeparate:
    """The rotating Costco token is state, not config — which is what lets config.json stay :ro."""

    def test_a_token_write_leaves_other_profiles_alone(self, config_file, tmp_path, monkeypatch):
        """Several profiles share `.state.json`, so a blind whole-file write during a multi-profile
        run would drop whichever profile saved first."""
        monkeypatch.setattr(loader, "STATE_FILE", tmp_path / ".state.json")
        from scrapers.costco_api import load_costco_auth, save_costco_auth

        save_costco_auth("profile-a", {"refresh_token": "aaa"})
        save_costco_auth("profile-b", {"refresh_token": "bbb"})

        assert load_costco_auth("profile-a") == {"refresh_token": "aaa"}
        assert load_costco_auth("profile-b") == {"refresh_token": "bbb"}

    def test_tokens_never_land_in_the_config_file(self, config_file, tmp_path, monkeypatch):
        path = config_file(google={"sheet_id": "x"})
        monkeypatch.setattr(loader, "STATE_FILE", tmp_path / ".state.json")
        from scrapers.costco_api import save_costco_auth

        save_costco_auth("profile-a", {"refresh_token": "aaa"})

        assert "refresh_token" not in path.read_text(encoding="utf-8")
