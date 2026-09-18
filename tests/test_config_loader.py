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
            web_enabled=True,
            backups_enabled=True, backups_frequency="daily", backups_time="03:30",
            backups_days="", backups_keep=14,
        ))

        assert cs.render().splitlines() == [
            "export RUN_INTERVAL_HOURS='4'",
            "export RUN_ON_START='true'",
            "export PREFLIGHT_STRICT='false'",
            "export TZ='America/New_York'",
            "export WEB_ENABLED='true'",
            "export BACKUP_ENABLED='true'",
            "export BACKUP_FREQUENCY='daily'",
            "export BACKUP_TIME='03:30'",
            "export BACKUP_DAYS=''",
            "export BACKUP_KEEP='14'",
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

    def test_a_flag_fails_closed_on_a_falsy_string(self, config_file, monkeypatch):
        """A behaviour FIX (learned on the retired *_FORCE_AGENT hooks): a bare truthiness test
        treats "0" as true, so a money switch written as =0 read as ON. Only affirmative spellings
        count.
        """
        config_file()
        monkeypatch.setenv("BUYING_GROUP_SYNC_ENABLED", "0")
        assert _get_bool("BUYING_GROUP_SYNC_ENABLED", False) is False

        monkeypatch.setenv("BUYING_GROUP_SYNC_ENABLED", "1")
        assert _get_bool("BUYING_GROUP_SYNC_ENABLED", False) is True


class TestCommentsSurvive:
    """JSON has no comments, so the config file uses `"// note"` keys."""

    def test_comment_keys_are_stripped_before_a_model_sees_them(self, config_file):
        """A comment key is a REAL key, so a pydantic model handed one rejects it.

        `Card.retailer_rates` is typed `dict[str, ...]` — every key in it has to be a retailer — so a
        `"// note"` sitting beside the rates is the case that actually fails validation rather than
        being quietly ignored.
        """
        config_file(cards=[{
            "// note": "why this card is here",
            "last4": "4321",
            "name": "Chase Freedom Unlimited",
            "retailer_rates": {
                "// note": "5% on Amazon while the promo runs",
                "Amazon": "5%",
            },
        }])
        from config.cards import load_cards

        card = load_cards()[0]
        # Keys are normalized to lowercase for loose matching; the point is that the comment is not
        # among them and did not fail validation on the way in.
        assert sorted(card.retailer_rates) == ["amazon"]

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
    """docs/configuration.md publishes every variable name and the config key it overrides.

    A published name is a promise: DEPLOY.md, docker-compose.yml and people's own shell aliases use
    these, so one that quietly stops being read — or a new setting that never gets documented — is
    exactly the kind of drift nobody notices until a value silently falls back to its default. The
    table is generated from ENV_TO_CONFIG, so this asserts it still agrees with it.
    """

    @staticmethod
    def _readme() -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / "docs" / "configuration.md").read_text(encoding="utf-8")

    @staticmethod
    def _booleans() -> set[str]:
        """The variables settings.py actually reads with _get_bool — it records them itself.

        Derived, not listed, so a boolean added later cannot pass by being forgotten here as well as
        in the docs, which would defeat the point of the check.
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


class TestTheCredentialStoreNeverShipsInTheImage:
    """config.json must be excluded from BOTH git and the Docker build context.

    Consolidation moved every credential into one file, and `.dockerignore` — whose whole stated
    purpose is "never bake secrets into the image" — kept naming the six files it replaced. So
    `COPY . .` would have baked the Google private key, both buying-group API keys, the proxy and
    retailer passwords and the OCI secret key into `buying-group-ledger`.

    The `:ro` bind mount is what makes that invisible: the mounted file wins at runtime, so the
    container behaves exactly right while the image quietly carries a full set of live credentials
    wherever it is pushed or saved. Nothing would ever have raised.

    Derived from the loader's own constants, so renaming either file moves this check with it.
    """

    @staticmethod
    def _patterns(filename: str) -> set[str]:
        from pathlib import Path

        text = (Path(__file__).resolve().parents[1] / filename).read_text(encoding="utf-8")
        return {line.strip() for line in text.splitlines()
                if line.strip() and not line.lstrip().startswith("#")}

    @pytest.mark.parametrize("ignore_file", [".dockerignore", ".gitignore"])
    def test_the_config_and_state_files_are_excluded(self, ignore_file):
        from config.loader import CONFIG_FILE, STATE_FILE

        patterns = self._patterns(ignore_file)

        for path in (CONFIG_FILE, STATE_FILE):
            assert path.name in patterns, (
                f"{path.name} is not in {ignore_file} — it holds every live credential")

    def test_dockerignore_still_covers_the_legacy_secret_files(self):
        """They are gone from this machine, but an un-migrated host still has them next to a
        Dockerfile that copies the whole tree."""
        patterns = self._patterns(".dockerignore")

        for name in (".env", "service_account.json", "profiles.json", "cards.json",
                     "warehouses.json", ".costco/"):
            assert name in patterns, f"{name} is not in .dockerignore"


class TestEveryBrowserScriptReachesTheApiKey:
    """A script that drives the cloud browser must import `config.settings`.

    The Browser-Use SDK reads BROWSER_USE_API_KEY out of `os.environ` ITSELF — nothing hands it over
    — and importing `config.settings` is the only thing that puts the config.json value there
    (`_export_sdk_env`). The one-off scripts predate the consolidation and called `load_dotenv()`
    instead, which was sufficient when the key lived in `.env` and finds nothing now: five of them
    died at "No API key provided" against a completely valid setup.

    Checked by SOURCE rather than by importing, because the correct fix is sometimes a lazy import
    inside the function that needs it (scripts/amazon_business_signin_probe.py does this to keep
    `--help` fast), and an import-and-inspect test would call that broken.
    """

    #: Importing one of these means the script talks to the cloud browser and needs the key.
    BROWSER_IMPORTS = ("browser_use_sdk", "scrapers.cdp", "from scrapers.cdp")

    @staticmethod
    def _scripts():
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "scripts"
        return sorted(p for p in root.glob("*.py") if p.name != "__init__.py")

    def test_every_script_that_drives_a_browser_imports_config_settings(self):
        offenders = []
        checked = 0
        for path in self._scripts():
            source = path.read_text(encoding="utf-8")
            if not any(marker in source for marker in self.BROWSER_IMPORTS):
                continue
            checked += 1
            if "config.settings" not in source:
                offenders.append(path.name)

        assert checked, "no browser-driving scripts found — the marker list has rotted"
        assert not offenders, (
            f"these drive the cloud browser but never import config.settings, so the API key in "
            f"config.json never reaches the SDK: {offenders}")

    def test_no_script_relies_on_load_dotenv_for_the_api_key(self):
        """`load_dotenv()` is not wrong, it is just not sufficient any more — and a bare call with a
        comment about the API key next to it is the exact pattern that broke."""
        offenders = [
            path.name for path in self._scripts()
            if "load_dotenv()" in path.read_text(encoding="utf-8")
            and "config.settings" not in path.read_text(encoding="utf-8")
        ]

        assert not offenders, f"{offenders} call load_dotenv() without importing config.settings"


class TestEveryScriptCanPrintItsOwnHelp:
    """`--help` must not die on the console encoding it will actually be printed to.

    argparse renders the module docstring, and Windows consoles default to cp1252. A character
    outside it — `→` in one probe's usage banner — makes `--help` traceback with a UnicodeEncodeError
    before printing anything useful, which reads as "the script is broken" rather than "one arrow is
    unprintable". Em-dashes, `…` and `§` all survive cp1252, so this is a narrow check, not a ban on
    typography.
    """

    def test_no_script_docstring_uses_a_character_cp1252_cannot_print(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "scripts"
        offenders = {}
        for path in sorted(root.glob("*.py")):
            if path.name == "__init__.py":
                continue
            # argparse prints everything down to the first definition: the docstring and any
            # module-level help strings above it.
            head = path.read_text(encoding="utf-8").split("def ")[0]
            bad = sorted({c for c in head if not _cp1252_safe(c)})
            if bad:
                offenders[path.name] = [f"U+{ord(c):04X}" for c in bad]

        assert not offenders, f"--help would raise UnicodeEncodeError on a cp1252 console: {offenders}"


def _cp1252_safe(char: str) -> bool:
    try:
        char.encode("cp1252")
    except UnicodeEncodeError:
        return False
    return True
