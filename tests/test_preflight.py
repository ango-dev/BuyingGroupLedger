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
    def test_a_directory_is_diagnosed_as_the_docker_bind_mount_trap(self, tmp_path):
        """Docker creates an empty DIRECTORY when a bind-mounted host file is missing.

        Reporting this as merely 'missing' would send you looking for the wrong problem — the file
        exists on the host or it doesn't, and the fix is a compose edit either way.
        """
        (tmp_path / "profiles.json").write_text("[]", encoding="utf-8")
        (tmp_path / "service_account.json").write_text("{}", encoding="utf-8")
        (tmp_path / "cards.json").mkdir()

        result = _by_name(preflight.check_config_files(root=tmp_path), "cards.json")

        assert result.level == FAIL
        assert "DIRECTORY" in result.detail
        assert "docker-compose" in result.detail

    def test_missing_required_config_fails_but_missing_optional_only_warns(self, tmp_path):
        (tmp_path / "service_account.json").write_text("{}", encoding="utf-8")

        results = preflight.check_config_files(root=tmp_path)

        # No profiles.json = nothing is scraped at all.
        assert _by_name(results, "profiles.json").level == FAIL
        # No cards.json = profit is misstated, but orders are still captured.
        assert _by_name(results, "cards.json").level == WARN
        assert "DEFAULT_CASHBACK_RATE" in _by_name(results, "cards.json").detail

    def test_malformed_json_fails_rather_than_reading_as_absent(self, tmp_path):
        (tmp_path / "profiles.json").write_text("{not json", encoding="utf-8")
        (tmp_path / "service_account.json").write_text("{}", encoding="utf-8")

        result = _by_name(preflight.check_config_files(root=tmp_path), "profiles.json")

        assert result.level == FAIL
        assert "not valid JSON" in result.detail

    def test_service_account_path_follows_the_env_var(self, tmp_path, monkeypatch):
        (tmp_path / "profiles.json").write_text("[]", encoding="utf-8")
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

    def test_no_alert_channel_warns_because_nothing_could_report_a_failure(self, monkeypatch):
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

        result = _by_name(preflight.check_money_switches(), ".env values")

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
