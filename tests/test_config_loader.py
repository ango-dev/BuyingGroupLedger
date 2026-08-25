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
from config.settings import (BOOLEAN_SETTINGS, ENV_TO_CONFIG, _get_bool, _get_float, _get_int,
                             _get_rate, _get_str)


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


class TestNothingIsEnvironmentOnly:
    """Every setting has a config home; `.env` is purely the override layer.

    The container knobs were the hard case: docker-compose interpolates its own file before any
    Python runs, so `RUN_INTERVAL_HOURS` could never reach config.json without something resolving it
    on the container's behalf. docker/entrypoint.sh now sources scripts/container_settings.py.
    """

    @pytest.mark.parametrize("name", [
        "RUN_INTERVAL_HOURS", "RUN_ON_START", "PREFLIGHT_STRICT", "TZ",
        "AMAZON_FORCE_AGENT", "AMAZON_BUSINESS_FORCE_AGENT",
        "BESTBUY_FORCE_AGENT", "COSTCO_FORCE_AGENT",
    ])
    def test_the_formerly_env_only_settings_have_a_config_home(self, name):
        assert name in ENV_TO_CONFIG

    def test_the_container_resolver_emits_exported_shell_assignments(self, monkeypatch):
        """`export`, not a bare assignment: TZ has to reach supercronic and every scrape it spawns.

        Injects the settings rather than reloading the module. Reloading re-runs `load_dotenv()`,
        which puts the developer's own .env back into the environment mid-test — correct behaviour
        (the environment wins) but it makes the assertion depend on whose machine is running it.
        Booleans are rendered as the `true`/`false` the entrypoint's `[ "$X" = "true" ]` compares.
        """
        from types import SimpleNamespace

        import scripts.container_settings as cs

        monkeypatch.setattr(cs, "settings", SimpleNamespace(
            container_run_interval_hours=4,
            container_run_on_start=True,
            container_preflight_strict=False,
            container_timezone="America/New_York",
        ))

        assert cs.render().splitlines() == [
            "export RUN_INTERVAL_HOURS='4'",
            "export RUN_ON_START='true'",
            "export PREFLIGHT_STRICT='false'",
            "export TZ='America/New_York'",
        ]

    def test_the_container_knobs_resolve_from_the_config_file(self, config_file, monkeypatch):
        """The point of the whole exercise: with the environment silent, the config file decides."""
        for name in ("RUN_INTERVAL_HOURS", "TZ"):
            monkeypatch.delenv(name, raising=False)
        config_file(container={"run_interval_hours": 4, "timezone": "America/New_York"})

        assert _get_int("RUN_INTERVAL_HOURS", 6) == 4
        assert _get_str("TZ", "UTC") == "America/New_York"

    def test_a_value_with_a_quote_cannot_break_the_sourcing_shell(self):
        """A timezone — or any future string value — must not be able to end the quoting early and
        turn the rest of the value into shell commands, since the entrypoint SOURCES this."""
        import subprocess
        import sys

        from scripts.container_settings import _quote

        # Assert the property rather than the escaping: round-trip it through a real shell.
        quoted = _quote("it's a 'value'; echo pwned")
        out = subprocess.run(
            ["sh", "-c", f"printf %s {quoted}"], capture_output=True, text=True, check=True,
        ) if sys.platform != "win32" else None
        if out is not None:
            assert out.stdout == "it's a 'value'; echo pwned"
        else:  # no POSIX sh on Windows; assert the escaping directly
            assert quoted == "'it'" + chr(92) + "''s a '" + chr(92) + "''value'" + chr(92) + \
                   "''; echo pwned'"

    def test_force_agent_fails_closed_on_a_falsy_string(self, config_file, monkeypatch):
        """A behaviour FIX. The old `if os.getenv("COSTCO_FORCE_AGENT")` treated ANY non-empty value
        as true, so `COSTCO_FORCE_AGENT=0` forced the PAID agent — the opposite of what it reads as.
        """
        config_file()
        monkeypatch.setenv("COSTCO_FORCE_AGENT", "0")
        assert _get_bool("COSTCO_FORCE_AGENT", False) is False

        monkeypatch.setenv("COSTCO_FORCE_AGENT", "1")
        assert _get_bool("COSTCO_FORCE_AGENT", False) is True


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


class TestTheReadmeTableStaysHonest:
    """README.md publishes every variable name and the config key it overrides.

    A published name is a promise: DEPLOY.md, docker-compose.yml and people's own shell aliases use
    these, so one that quietly stops being read — or a new setting that never gets documented — is
    exactly the kind of drift nobody notices until a value silently falls back to its default. The
    table is generated from ENV_TO_CONFIG, so this asserts it still agrees with it.
    """

    @staticmethod
    def _readme() -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")

    @staticmethod
    def _booleans() -> set[str]:
        """The variables settings.py actually reads with _get_bool — it records them itself.

        Derived, not listed, so a boolean added later cannot pass by being forgotten here as well as
        in the README, which would defeat the point of the check.
        """
        from config.settings import BOOLEAN_SETTINGS

        return set(BOOLEAN_SETTINGS)

    def test_every_variable_is_documented_against_the_right_key(self):
        import re

        row = re.compile(r"^\| `([A-Z0-9_]+)`(?: †)? \| `([a-z0-9_.]+)` \|$", re.M)
        assert dict(row.findall(self._readme())) == dict(ENV_TO_CONFIG)

    def test_every_boolean_is_marked(self):
        """The dagger is what tells a reader to write `true`, not 1. An unmarked boolean is worse
        than an undocumented one: it reads as a free-text setting and invites `FOO=0`, which the
        pre-consolidation code treated as ON."""
        import re

        marked = set(re.findall(r"^\| `([A-Z0-9_]+)` † \|", self._readme(), re.M))

        assert self._booleans(), "no _get_bool calls found — the source scrape has rotted"
        assert marked == self._booleans()


class TestMigratedFlagsAreRealBooleans:
    """`.env` can only hold strings, so the migrator has to pick a JSON type for every value.

    It got the flags wrong: reading the VALUE alone, `RECEIPT_CAPTURE_ENABLED=1` and
    `RUN_INTERVAL_HOURS=1` are the same three characters, so both became the int 1 and a migrated
    config carried `"capture_enabled": 1`. It still worked — _get_bool accepts "1" — which is exactly
    why nobody noticed: no error, no warning, just a money switch written in the one spelling that
    gives no hint which way round it goes.
    """

    @pytest.mark.parametrize("name", sorted(BOOLEAN_SETTINGS))
    @pytest.mark.parametrize("raw, expected", [("1", True), ("0", False),
                                               ("true", True), ("false", False)])
    def test_a_flag_migrates_to_a_json_boolean(self, name, raw, expected):
        from scripts.migrate_config import _coerce

        value = _coerce(name, raw)

        assert value is expected, f"{name}={raw} migrated as {value!r} ({type(value).__name__})"

    def test_a_numeric_setting_is_still_a_number(self):
        """The fix must not sweep up RUN_INTERVAL_HOURS=1, which really is the int 1."""
        from scripts.migrate_config import _coerce

        assert _coerce("RUN_INTERVAL_HOURS", "1") == 1
        assert not isinstance(_coerce("RUN_INTERVAL_HOURS", "1"), bool)
        assert _coerce("BROWSER_USE_MAX_COST_USD", "0.5") == 0.5
        assert _coerce("DEFAULT_CASHBACK_RATE", "2%") == "2%"

    def test_the_shipped_example_writes_its_flags_as_booleans(self):
        """config.example.json is what people copy, so it is the spelling that propagates."""
        import json
        from pathlib import Path

        from config.settings import ENV_TO_CONFIG

        example = json.loads(
            (Path(__file__).resolve().parents[1] / "config.example.json").read_text(
                encoding="utf-8"))

        checked = 0
        for name in BOOLEAN_SETTINGS:
            node = example
            for part in ENV_TO_CONFIG[name].split("."):
                if not isinstance(node, dict) or part not in node:
                    node = None
                    break
                node = node[part]
            if node is None:
                continue
            checked += 1
            assert isinstance(node, bool), f"{ENV_TO_CONFIG[name]} is {node!r}, not a boolean"

        assert checked == len(BOOLEAN_SETTINGS), (
            "the example file no longer shows every flag, so this check has stopped covering them")
