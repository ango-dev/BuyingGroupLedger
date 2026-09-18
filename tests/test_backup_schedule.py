"""Scheduled backups (2026-09-18): the schedule from `backups.*`, the cron line the container
appends, the retention rule, the consistent SQLite copy, and the cron job's own entry point."""
from __future__ import annotations

import dataclasses
import sqlite3
import zipfile
from pathlib import Path

import pytest

from config.settings import Settings
from scripts import backup
from scripts.backup import (
    Schedule, create_backup, list_backups, parse_days, parse_days_any, parse_time, prune_backups,
    run_scheduled, schedule_from_settings,
)


def settings_with(**overrides) -> Settings:
    return dataclasses.replace(Settings(), **overrides)


class TestParsing:
    def test_a_time_of_day(self):
        assert parse_time("03:30") == (3, 30) and parse_time(" 23:05 ") == (23, 5) and parse_time("7:00") == (7, 0)
        for bad in ("24:00", "12:60", "noon", "", "3"):
            with pytest.raises(ValueError):
                parse_time(bad)

    def test_days_by_frequency(self):
        assert parse_days("weekly", "") == ("sun",)
        assert parse_days("weekly", "Mon, thu") == ("mon", "thu")
        assert parse_days("weekly", "monday thursday monday") == ("mon", "thu")
        assert parse_days("monthly", "") == (1,)
        assert parse_days("monthly", "1,15") == (1, 15)
        assert parse_days("daily", "whatever") == ()
        with pytest.raises(ValueError, match="day names"):
            parse_days("weekly", "1,15")
        with pytest.raises(ValueError, match="1-28"):
            parse_days("monthly", "31")
        with pytest.raises(ValueError):
            parse_days("monthly", "mon")

    def test_days_validated_without_a_frequency(self):
        assert parse_days_any("") == "" and parse_days_any("mon,thu") == "mon,thu" and parse_days_any("1,15") == "1,15"
        with pytest.raises(ValueError, match="day names"):
            parse_days_any("someday")


class TestSchedule:
    def test_daily_is_one_cron_line(self):
        s = Schedule(frequency="daily", time="03:30")
        assert s.cron() == "30 3 * * *" and s.describe() == "every day at 03:30" and s.problems == ()

    def test_weekly_names_its_days(self):
        s = Schedule(frequency="weekly", time="02:15", days="mon,thu")
        assert s.cron() == "15 2 * * 1,4"
        assert s.describe() == "every Monday and Thursday at 02:15"
        assert Schedule(frequency="weekly").cron() == "30 3 * * 0"  # Sunday by default

    def test_monthly_names_its_dates(self):
        s = Schedule(frequency="monthly", time="04:00", days="1,15")
        assert s.cron() == "0 4 1,15 * *" and s.describe() == "on the 1st and 15th of each month at 04:00"
        assert Schedule(frequency="monthly", days="1, 2, 3, 22").describe() == \
            "on the 1st, 2nd, 3rd and 22nd of each month at 03:30"

    def test_a_bad_value_falls_back_and_is_named(self):
        s = Schedule(frequency="fortnightly", time="25:00", days="???", keep="lots")
        assert s.frequency == "daily" and s.time == "03:30" and s.keep == 14
        assert len(s.problems) == 3  # days are fine for daily (ignored)
        assert any("fortnightly" in p for p in s.problems) and any("25:00" in p for p in s.problems)
        weekly = Schedule(frequency="weekly", days="1,15")
        assert weekly.days == ("sun",) and any("backups.days" in p for p in weekly.problems)

    def test_from_the_settings(self):
        s = schedule_from_settings(settings_with(backups_enabled=False, backups_frequency="weekly",
                                                 backups_time="01:00", backups_days="fri", backups_keep=3))
        assert not s.enabled and s.cron() == "0 1 * * 5" and s.keep == 3
        default = schedule_from_settings(Settings())
        assert default.enabled and default.cron() == "30 3 * * *" and default.keep == 14

    def test_the_container_exports_the_five_settings(self):
        from scripts.container_settings import render

        out = render()
        for name in ("BACKUP_ENABLED", "BACKUP_FREQUENCY", "BACKUP_TIME", "BACKUP_DAYS", "BACKUP_KEEP"):
            assert f"export {name}=" in out

    def test_the_cli_prints_the_cron_line_or_nothing(self, capsys, monkeypatch):
        import config.settings as cs

        monkeypatch.setattr(cs, "settings", settings_with(backups_frequency="monthly", backups_days="1"))
        assert backup.main(["--print-cron"]) == 0
        assert capsys.readouterr().out.strip() == "30 3 1 * *"
        monkeypatch.setattr(cs, "settings", settings_with(backups_enabled=False))
        assert backup.main(["--print-cron"]) == 0 and capsys.readouterr().out.strip() == ""
        assert backup.main(["--describe"]) == 0 and capsys.readouterr().out.strip() == "off"


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    (root / "config.json").write_text('{"x": 1}', encoding="utf-8")
    return root


def _ledger(path: Path, rows: int) -> None:
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE "ledger_rows" ("order_id" TEXT)')
    conn.executemany('INSERT INTO "ledger_rows" VALUES (?)', [(f"O{i}",) for i in range(rows)])
    conn.commit()
    conn.close()


class TestRetention:
    def test_the_oldest_beyond_keep_are_deleted(self, tmp_path):
        out = tmp_path / "backups"
        out.mkdir()
        names = [f"ledger_backup_2026091{d}T000000Z.zip" for d in range(1, 6)]
        for name in names:
            (out / name).write_bytes(b"zip")
        deleted = prune_backups(out, keep=2)
        assert [p.name for p in deleted] == names[2::-1][::-1][:3] or {p.name for p in deleted} == set(names[:3])
        assert [p.name for p in list_backups(out)] == names[:2:-1][::-1] or {p.name for p in list_backups(out)} == set(names[3:])
        assert prune_backups(out, keep=0) == [] and len(list_backups(out)) == 2


class TestSqliteCopy:
    def test_the_ledger_is_copied_through_the_backup_api_and_side_files_are_skipped(self, tmp_path):
        root = _repo(tmp_path)
        _ledger(root / "data" / "ledger.sqlite3", 3)
        (root / "data" / "ledger.sqlite3-wal").write_bytes(b"stale")
        (root / "data" / "notes.txt").write_text("plain", encoding="utf-8")
        archive = create_backup(root, tmp_path / "out")
        with zipfile.ZipFile(archive) as z:
            names = set(z.namelist())
            assert "data/ledger.sqlite3" in names and "data/notes.txt" in names and "config.json" in names
            assert not any(n.endswith("-wal") for n in names)
            z.extract("data/ledger.sqlite3", tmp_path / "restored")
        conn = sqlite3.connect(tmp_path / "restored" / "data" / "ledger.sqlite3")
        assert conn.execute('SELECT COUNT(*) FROM "ledger_rows"').fetchone()[0] == 3
        conn.close()

    def test_a_copy_lands_while_a_writer_holds_a_transaction(self, tmp_path):
        root = _repo(tmp_path)
        path = root / "data" / "ledger.sqlite3"
        _ledger(path, 2)
        writer = sqlite3.connect(path)
        writer.execute("BEGIN")
        writer.execute('INSERT INTO "ledger_rows" VALUES ("uncommitted")')
        try:
            archive = create_backup(root, tmp_path / "out")
        finally:
            writer.rollback()
            writer.close()
        with zipfile.ZipFile(archive) as z:
            z.extract("data/ledger.sqlite3", tmp_path / "restored")
        conn = sqlite3.connect(tmp_path / "restored" / "data" / "ledger.sqlite3")
        assert conn.execute('SELECT COUNT(*) FROM "ledger_rows"').fetchone()[0] == 2
        conn.close()


class TestTheCronJob:
    def test_it_backs_up_prunes_and_records(self, tmp_path, monkeypatch, capsys):
        import config.settings as cs
        from diagnostics import activity

        monkeypatch.setattr(cs, "settings", settings_with(backups_keep=2))
        root = _repo(tmp_path)
        out = root / "backups"
        out.mkdir()
        for stamp in ("20260901T000000Z", "20260902T000000Z"):
            (out / f"ledger_backup_{stamp}.zip").write_bytes(b"zip")
        assert run_scheduled(root, out) == 0
        kept = [p.name for p in list_backups(out)]
        assert len(kept) == 2 and "ledger_backup_20260901T000000Z.zip" not in kept
        events = [e for e in activity.read() if e["kind"] == "backup"]
        assert events and "Scheduled backup" in events[-1]["summary"] and "1 older one(s) deleted" in events[-1]["summary"]
        assert events[-1]["details"]["deleted"] == ["ledger_backup_20260901T000000Z.zip"]
        assert "keeping the newest 2" in capsys.readouterr().out

    def test_disabled_writes_nothing(self, tmp_path, monkeypatch, capsys):
        import config.settings as cs

        monkeypatch.setattr(cs, "settings", settings_with(backups_enabled=False))
        root = _repo(tmp_path)
        assert run_scheduled(root, root / "backups") == 0
        assert list_backups(root / "backups") == [] and "off" in capsys.readouterr().out

    def test_a_failure_alerts_instead_of_dying_quietly(self, tmp_path, monkeypatch):
        import config.settings as cs
        from alerts import notifier

        monkeypatch.setattr(cs, "settings", settings_with())
        sent = []
        monkeypatch.setattr(notifier, "alert", lambda subject, message, **kw: sent.append((subject, kw.get("kind"))))
        root = _repo(tmp_path)
        monkeypatch.setattr(backup, "create_backup", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        assert run_scheduled(root, root / "backups") == 1
        assert sent == [("Scheduled backup FAILED", "backup")]


class TestTheSettingsPage:
    def test_the_backup_fields_are_validated_before_a_save(self):
        from web import settings_form

        by_env = {s.env: s for s in settings_form.schema()}
        assert settings_form._parse(by_env["BACKUP_FREQUENCY"], "Weekly") == "weekly"
        assert settings_form._parse(by_env["BACKUP_TIME"], "3:05") == "03:05"
        assert settings_form._parse(by_env["BACKUP_DAYS"], "mon, thu") == "mon, thu"
        for env, bad in (("BACKUP_FREQUENCY", "hourly"), ("BACKUP_TIME", "25:00"), ("BACKUP_DAYS", "someday")):
            with pytest.raises(ValueError):
                settings_form._parse(by_env[env], bad)
        assert settings_form.restart_scope(by_env["BACKUP_TIME"]) == "container"
