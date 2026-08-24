"""Tests for scripts/preflight.py — the startup check that catches SILENT misconfiguration.

Every case here mirrors a real deployment failure where the run keeps working and quietly does the
wrong thing. The assertions deliberately check the *explanation*, not just the level: a preflight
that says "FAIL: cards.json" and stops teaches nobody anything at 3am, and the whole reason these
failures went unnoticed is that their consequence is invisible.
"""

import json

import pytest

from scripts import preflight
from scripts.preflight import FAIL, OK, WARN, Result


def _by_name(results: list[Result], name: str) -> Result:
    matches = [r for r in results if r.name == name]
    assert matches, f"no check named {name!r} in {[r.name for r in results]}"
    return matches[0]


class TestDeterministicImports:
    """The expensive-but-silent failure: a missing dep moves retailers onto the PAID agent."""

    def test_all_deterministic_modules_import_in_this_checkout(self):
        # Doubles as a guard on requirements.txt: if a deterministic path grows a dependency that
        # nobody declares, this fails here rather than on the Pi as a recurring agent bill.
        results = preflight.check_deterministic_imports()
        assert [r for r in results if r.level != OK] == []

    def test_missing_playwright_is_reported_as_a_paid_fallback(self, monkeypatch):
        """The exact bug this file exists for: playwright was undeclared in requirements.txt."""
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "playwright" or name.startswith("playwright."):
                raise ModuleNotFoundError("No module named 'playwright'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        # scrapers.cdp is already imported by the test session, so drop it (and its dependents)
        # from the module cache to force a genuine re-import through the patched hook.
        for module in list(preflight.DETERMINISTIC_IMPORTS):
            monkeypatch.delitem(__import__("sys").modules, module, raising=False)

        results = preflight.check_deterministic_imports()
        failed = [r for r in results if r.level == FAIL]

        assert _by_name(results, "import scrapers.cdp").level == FAIL
        # The point isn't that it failed — it's that the report says why it matters.
        assert all("PAID" in r.detail for r in failed)
        assert any("Best Buy" in r.detail for r in failed)


class TestConfigFiles:
    """One config file now, and preflight is what stops an un-migrated host running silently.

    The failure mode it guards is specifically QUIET: without config.json the loaders return empty
    sections rather than raising, so such a host scrapes nothing, routes nothing to a buying group,
    and exits 0. Preflight gates the container (docker/entrypoint.sh), so it must FAIL rather than
    let that pass.
    """

    @staticmethod
    def _write(root, **sections):
        (root / "config.json").write_text(json.dumps(sections), encoding="utf-8")

    def test_an_unmigrated_host_fails_and_names_the_migration(self, tmp_path):
        """The old files still present with no config.json is the recognisable shape of a host that
        was updated but never migrated. Saying so beats "config.json is missing", which reads like a
        fresh install and invites someone to hand-write one beside a perfectly good old config."""
        for name in ("profiles.json", "cards.json"):
            (tmp_path / name).write_text("[]", encoding="utf-8")

        result = _by_name(preflight.check_config_files(root=tmp_path), "config.json")

        assert result.level == FAIL
        assert "not been migrated" in result.detail
        assert "scripts.migrate_config" in result.detail

    def test_a_directory_is_diagnosed_as_the_docker_bind_mount_trap(self, tmp_path):
        """Docker creates an empty DIRECTORY when a bind-mounted host file is missing.

        Reporting this as merely 'missing' would send you looking for the wrong problem — the file
        exists on the host or it doesn't, and the fix is a compose edit either way.
        """
        (tmp_path / "config.json").mkdir()

        result = _by_name(preflight.check_config_files(root=tmp_path), "config.json")

        assert result.level == FAIL
        assert "DIRECTORY" in result.detail
        assert "docker-compose" in result.detail

    def test_an_empty_profiles_section_fails_but_an_empty_cards_section_only_warns(
        self, tmp_path, config_file
    ):
        """Same severity split as before, on sections rather than files: no profiles means nothing
        is scraped at all, while no cards only misstates profit."""
        self._write(tmp_path, google={"service_account": {"private_key": "x"}})
        config_file(google={"service_account": {"private_key": "x"}})

        results = preflight.check_config_files(root=tmp_path)

        assert _by_name(results, "config.json `profiles`").level == FAIL
        assert _by_name(results, "config.json `cards`").level == WARN
        assert "DEFAULT_CASHBACK_RATE" in _by_name(results, "config.json `cards`").detail

    def test_malformed_json_fails_rather_than_reading_as_absent(self, tmp_path):
        """A typo in the ONE file holding every credential must not degrade to "no config"."""
        (tmp_path / "config.json").write_text("{not json", encoding="utf-8")

        result = _by_name(preflight.check_config_files(root=tmp_path), "config.json")

        assert result.level == FAIL
        assert "not valid JSON" in result.detail

    def test_legacy_files_alongside_a_config_only_warn(self, tmp_path, config_file):
        """Migration deliberately leaves the originals, so their presence is normal — but they are
        no longer read, and someone editing one would see no effect."""
        self._write(tmp_path, profiles=[{"label": "p", "retailers": ["amazon"]}])
        config_file(profiles=[{"label": "p", "retailers": ["amazon"]}])
        (tmp_path / "profiles.json").write_text("[]", encoding="utf-8")

        result = _by_name(preflight.check_config_files(root=tmp_path), "legacy config")

        assert result.level == WARN
        assert "NO LONGER READ" in result.detail

    def test_the_inlined_google_credential_satisfies_the_check(
        self, tmp_path, monkeypatch, config_file
    ):
        monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_FILE", raising=False)
        self._write(tmp_path, google={"service_account": {"private_key": "x"}})
        config_file(google={"service_account": {"private_key": "x"}})

        assert _by_name(preflight.check_config_files(root=tmp_path),
                        "google credentials").level == OK

    def test_service_account_path_still_follows_the_env_var(self, tmp_path, monkeypatch, config_file):
        """The standalone file remains supported and still overrides the inlined block."""
        self._write(tmp_path, profiles=[{"label": "p", "retailers": ["amazon"]}])
        config_file(profiles=[{"label": "p", "retailers": ["amazon"]}])
        key = tmp_path / "nested" / "key.json"
        key.parent.mkdir()
        key.write_text(json.dumps({"type": "service_account"}), encoding="utf-8")
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(key))

        assert _by_name(preflight.check_config_files(root=tmp_path), "key.json").level == OK


class TestEnv:
    def test_unset_required_env_fails_with_its_consequence(self, monkeypatch):
        monkeypatch.setenv("BROWSER_USE_API_KEY", "")
        monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet-123")

        results = preflight.check_env()

        assert _by_name(results, "BROWSER_USE_API_KEY").level == FAIL
        assert _by_name(results, "GOOGLE_SHEET_ID").level == OK

    def test_no_alert_channel_warns_because_nothing_could_report_a_failure(
        self, monkeypatch, config_file
    ):
        config_file()  # empty config: the channels can only come from the environment
        for name in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "DISCORD_WEBHOOK_URL"):
            monkeypatch.setenv(name, "")

        result = _by_name(preflight.check_env(), "alerts")

        assert result.level == WARN
        assert "unattended" in result.detail

    def test_one_configured_channel_is_enough(self, monkeypatch):
        monkeypatch.setenv("GMAIL_ADDRESS", "")
        monkeypatch.setenv("GMAIL_APP_PASSWORD", "")
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/webhook")

        assert _by_name(preflight.check_env(), "alerts").level == OK


def _store_token(tmp_path, monkeypatch):
    """Put a Costco token in `.state.json` — where they live now, because they ROTATE."""
    import json as _json

    from config import loader

    state = tmp_path / ".state.json"
    monkeypatch.setattr(loader, "STATE_FILE", state)
    state.write_text(_json.dumps({"costco": {"profile-alpha": {"refresh_token": "rt"}}}),
                     encoding="utf-8")
    loader.reload_config()
    return state


class TestCostcoTokens:
    def test_missing_token_is_a_failure_naming_the_fix(self, tmp_path, monkeypatch):
        class _Profile:
            label = "profile-alpha"

        monkeypatch.setattr("config.profiles.load_profiles_for_retailer", lambda key: [_Profile()])

        result = _by_name(preflight.check_costco_tokens(root=tmp_path),
                          "costco token [profile-alpha]")

        assert result.level == FAIL
        assert "scripts.costco_token" in result.detail
        assert "PAID agent" in result.detail

    def test_a_present_but_unwritable_token_dir_fails(self, tmp_path, monkeypatch):
        """docker-compose mounted ./.costco as :ro.

        Costco ROTATES its refresh token and _save_auth persists the new one, so a read-only mount
        raises OSError, degrades Costco to the PAID agent every run, and throws the rotation away.
        Checking only that the file EXISTS misses it completely — which is what happened.
        """
        class _Profile:
            label = "profile-alpha"

        monkeypatch.setattr("config.profiles.load_profiles_for_retailer", lambda key: [_Profile()])
        _store_token(tmp_path, monkeypatch)

        # Simulate the read-only mount: the probe write is what fails, not the read.
        real_touch = preflight.Path.touch

        def deny(self, *args, **kwargs):
            if self.name == ".preflight_write_probe":
                raise OSError(30, "Read-only file system")
            return real_touch(self, *args, **kwargs)

        monkeypatch.setattr(preflight.Path, "touch", deny)

        result = _by_name(preflight.check_costco_tokens(root=tmp_path),
                          "costco token [profile-alpha]")

        assert result.level == FAIL
        assert "NOT WRITABLE" in result.detail
        assert ":ro" in result.detail          # names the exact fix

    def test_a_writable_token_dir_passes(self, tmp_path, monkeypatch):
        class _Profile:
            label = "profile-alpha"

        monkeypatch.setattr("config.profiles.load_profiles_for_retailer", lambda key: [_Profile()])
        _store_token(tmp_path, monkeypatch)

        result = _by_name(preflight.check_costco_tokens(root=tmp_path),
                          "costco token [profile-alpha]")

        assert result.level == OK
        assert "writable" in result.detail
        # The probe must not leave litter behind in a directory full of secrets.
        assert not (tmp_path / ".costco" / ".preflight_write_probe").exists()

    def test_no_costco_profile_is_not_a_problem(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.profiles.load_profiles_for_retailer", lambda key: [])

        assert _by_name(preflight.check_costco_tokens(root=tmp_path), "costco token").level == OK


class TestMoneySwitches:
    def test_unloadable_settings_is_reported_not_raised(self, monkeypatch):
        """config/settings.py validates at IMPORT time and raises on a bad value by design.

        DEFAULT_CASHBACK_RATE=2 is rejected outright because it reads equally as 2% or 200%. That
        kills every run at import — so preflight must NAME it, not die the same way and leave a
        traceback as the only clue.
        """
        import sys

        monkeypatch.delitem(sys.modules, "config.settings", raising=False)
        monkeypatch.setenv("DEFAULT_CASHBACK_RATE", "2")

        result = _by_name(preflight.check_money_switches(), "config values")

        assert result.level == FAIL
        assert "DEFAULT_CASHBACK_RATE" in result.detail or "outside 0-1" in result.detail
        assert "every run fails" in result.detail.lower()

    def test_enabled_sync_surfaces_the_mod_allowlist(self, monkeypatch):
        import sys

        monkeypatch.delitem(sys.modules, "config.settings", raising=False)
        monkeypatch.setenv("BUYING_GROUP_SYNC_ENABLED", "1")
        monkeypatch.setenv("MAXOUTDEALS_API_KEY", "token-123")

        results = preflight.check_money_switches()

        assert _by_name(results, "buying-group sync").level == OK
        assert "real money" in _by_name(results, "buying-group sync").detail
        # Nothing on this host can verify the allowlist, so it is surfaced every time.
        assert _by_name(results, "MaxOutDeals IP allowlist").level == WARN

    def test_disabled_sync_warns_that_nothing_is_submitted(self, monkeypatch):
        import sys

        monkeypatch.delitem(sys.modules, "config.settings", raising=False)
        monkeypatch.setenv("BUYING_GROUP_SYNC_ENABLED", "0")

        result = _by_name(preflight.check_money_switches(), "buying-group sync")

        assert result.level == WARN
        assert "not submitted" in result.detail


class TestRunInterval:
    """The schedule is bounded by MaxOutDeals' daily quota, not by anything in this repo."""

    @pytest.mark.parametrize("hours,expected", [
        (1, 24), (2, 12), (3, 8), (4, 6), (6, 4), (8, 3), (12, 2), (23, 2),
        # Intervals that do NOT divide 24 evenly are the ones a 24/H formula gets wrong: cron
        # enumerates multiples within 0-23, so H=5 fires at 0,5,10,15,20 — five times, not 4.8.
        (5, 5), (7, 4), (9, 3), (10, 3), (11, 3),
    ])
    def test_runs_per_day_matches_how_cron_actually_steps(self, hours, expected):
        assert preflight.runs_per_day(hours) == expected

    def _interval(self, monkeypatch, hours, sync="1"):
        import sys
        monkeypatch.delitem(sys.modules, "config.settings", raising=False)
        monkeypatch.setenv("RUN_INTERVAL_HOURS", str(hours))
        monkeypatch.setenv("BUYING_GROUP_SYNC_ENABLED", sync)
        return preflight.check_run_interval()

    def test_three_hours_is_inside_the_quota_and_reports_the_headroom(self, monkeypatch):
        result = _by_name(self._interval(monkeypatch, 3), "run interval")

        assert result.level == OK
        assert "8 run(s)/day" in result.detail
        assert "2 spare" in result.detail

    def test_two_hours_warns_because_it_outruns_the_payout_quota(self, monkeypatch):
        result = _by_name(self._interval(monkeypatch, 2), "run interval")

        assert result.level == WARN
        assert "12 runs/day" in result.detail
        # Says what actually breaks, and what does not.
        assert "Payout Amount" in result.detail
        assert "tracking submission is unaffected" in result.detail
        # And what to do instead: 24/10 rounded up = 3h.
        assert "Use 3h or longer" in result.detail

    def test_no_warning_when_the_buying_group_sync_is_off(self, monkeypatch):
        """Nothing calls MOD, so the quota is irrelevant however often it runs."""
        result = _by_name(self._interval(monkeypatch, 1, sync="0"), "run interval")

        assert result.level == OK
        assert "MaxOutDeals" not in result.detail

    def test_unset_interval_is_not_reported_at_all(self, monkeypatch):
        monkeypatch.delenv("RUN_INTERVAL_HOURS", raising=False)
        # The native cron path has no such variable; install_cron.sh does this check itself.
        assert preflight.check_run_interval() == []

    @pytest.mark.parametrize("bad", ["0", "24", "abc", "4.5", "-1"])
    def test_an_unusable_interval_warns_and_names_the_fallback(self, monkeypatch, bad):
        monkeypatch.setenv("RUN_INTERVAL_HOURS", bad)

        result = _by_name(preflight.check_run_interval(), "RUN_INTERVAL_HOURS")

        assert result.level == WARN
        assert "fall back to 6" in result.detail


class TestExitCode:
    """The container and cron both branch on this, so the exit code is part of the contract."""

    def test_failures_exit_nonzero(self, monkeypatch):
        monkeypatch.setattr(preflight, "run_checks",
                            lambda: [Result(FAIL, "x", "broken"), Result(OK, "y", "fine")])
        assert preflight.main([]) == 1

    def test_warnings_alone_exit_zero(self, monkeypatch):
        monkeypatch.setattr(preflight, "run_checks",
                            lambda: [Result(WARN, "x", "iffy"), Result(OK, "y", "fine")])
        assert preflight.main([]) == 0

    def test_strict_promotes_warnings_to_failure(self, monkeypatch):
        monkeypatch.setattr(preflight, "run_checks", lambda: [Result(WARN, "x", "iffy")])
        assert preflight.main(["--strict"]) == 1

    def test_alert_flag_reports_only_the_failures(self, monkeypatch):
        sent = []
        monkeypatch.setattr("alerts.notifier.alert",
                            lambda subject, body: sent.append((subject, body)))
        monkeypatch.setattr(preflight, "run_checks", lambda: [
            Result(FAIL, "import scrapers.cdp", "playwright is missing"),
            Result(WARN, "cards.json", "missing"),
        ])

        assert preflight.main(["--alert"]) == 1
        assert len(sent) == 1
        subject, body = sent[0]
        assert "Preflight FAILED" in subject
        assert "scrapers.cdp" in body
        assert "cards.json" not in body  # warnings are not worth waking someone up for

    def test_an_unsendable_alert_does_not_mask_the_failures(self, monkeypatch):
        def boom(subject, body):
            raise RuntimeError("smtp down")

        monkeypatch.setattr("alerts.notifier.alert", boom)
        monkeypatch.setattr(preflight, "run_checks", lambda: [Result(FAIL, "x", "broken")])

        # The whole point of preflight is reporting a problem; it must not turn into one.
        assert preflight.main(["--alert"]) == 1


@pytest.mark.parametrize("flag", ["--alert", "--strict"])
def test_flags_are_accepted(flag, monkeypatch):
    monkeypatch.setattr(preflight, "run_checks", lambda: [Result(OK, "x", "fine")])
    assert preflight.main([flag]) == 0
