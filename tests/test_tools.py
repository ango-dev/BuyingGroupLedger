"""The Tools page (web/tools.py): the registry, the job runner, the embedded profile-login
session and its timeout, and the routes."""

from __future__ import annotations

import dataclasses
import importlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from diagnostics import activity
from web import tools

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


class TestTheRegistry:
    def test_every_tool_names_a_real_script_with_the_arguments_it_declares(self):
        for t in tools.TOOLS:
            module = importlib.import_module(t.module)
            source = Path(module.__file__).read_text(encoding="utf-8")
            for f in t.fields:
                if f.name:
                    assert f'"{f.name}"' in source, f"{t.key}: {f.name} is not an argument of {t.module}"
        assert len({t.key for t in tools.TOOLS}) == len(tools.TOOLS)
        assert all(t.group in tools.GROUPS for t in tools.TOOLS)

    def test_argv_is_built_and_validated_from_the_form(self):
        t = tools.tool("backfill_receipts")
        assert t.argv({}) == []
        assert t.argv({"--apply": "on", "--retailer": "costco", "--limit": "3"}) == ["--apply", "--retailer", "costco", "--limit", "3"]
        with pytest.raises(ValueError, match="whole number"):
            t.argv({"--limit": "three"})
        with pytest.raises(ValueError, match="not one of"):
            t.argv({"--retailer": "walmart"})
        report = tools.tool("tax_report")
        assert report.argv({"Year": "2026", "--no-rows": "on"}) == ["2026", "--no-rows"]
        with pytest.raises(ValueError, match="required"):
            report.argv({})
        fix = tools.tool("fix_superseded_shipments")
        assert fix.argv({"--order": "A1, B2", "--apply": "on"}) == ["--order", "A1", "--order", "B2", "--apply"]


class TestTheJobRunner:
    def test_a_job_runs_a_subprocess_and_keeps_its_output(self, tmp_path):
        runner = tools.JobRunner(tmp_path, clock=lambda: NOW)
        fake = tools.Tool("echo", "unused", "Echo", "", "Checks")
        launches = []

        def launch(command, handle):
            launches.append(command)
            import subprocess

            return subprocess.Popen([sys.executable, "-c", "print('hello from the tool')"],
                                    stdout=handle, stderr=subprocess.STDOUT, text=True)

        runner.launch = launch
        job = runner.start(fake, ["--x", "1"])
        assert launches[0][:3] == [sys.executable, "-m", "unused"] and launches[0][3:] == ["--x", "1"]
        job._process.wait()
        for _ in range(50):
            if not job.running:
                break
            import time

            time.sleep(0.05)
        assert job.returncode == 0 and "hello from the tool" in job.output()
        assert job.output().startswith("$ python -m unused --x 1")
        assert runner.recent()[0] is job and runner.running_for("echo") is None

    def test_one_job_per_tool_at_a_time_and_a_launch_failure_is_reported(self, tmp_path):
        runner = tools.JobRunner(tmp_path, clock=lambda: NOW)
        fake = tools.Tool("slow", "unused", "Slow", "", "Checks")

        class Never:
            def wait(self):
                import threading

                threading.Event().wait(0.3)
                return 0

        runner.launch = lambda command, handle: Never()
        first = runner.start(fake, [])
        assert first.running
        with pytest.raises(RuntimeError, match="already running"):
            runner.start(fake, [])

        def boom(command, handle):
            raise FileNotFoundError("no python")

        runner.launch = boom
        broken = runner.start(tools.Tool("broken", "unused", "Broken", "", "Checks"), [])
        assert not broken.running and "FileNotFoundError" in broken.error


class FakeSession:
    def __init__(self, sid="sess-1", url="https://live.example/session/1"):
        self.id = sid
        self.live_url = url


class FakeClient:
    stopped: list = []
    created: list = []

    class profiles:
        @staticmethod
        def create(name):
            FakeClient.created.append(name)

            class P:
                id = "prof-new"

            return P()

    class sessions:
        @staticmethod
        def create(profile_id, custom_proxy, keep_alive):
            return FakeSession()

        @staticmethod
        def stop(session_id):
            FakeClient.stopped.append(session_id)

    def close(self):
        pass


@pytest.fixture
def profile_config(config_file):
    config_file(profiles=[{"label": "p1", "profile_id": "", "retailers": ["costco"],
                           "proxy": {"host": "proxy.example", "port": 8000, "username": "u", "password": "p"}}])
    FakeClient.stopped = []
    FakeClient.created = []


class TestTheProfileSession:
    def test_open_close_and_save(self, tmp_path, profile_config):
        from config.loader import config_value

        clock = {"now": NOW}
        sessions = tools.ProfileSessions(tmp_path, minutes=60, clock=lambda: clock["now"],
                                         client_factory=FakeClient, timer=False)
        assert sessions.current() is None
        state = sessions.start("p1", ["bestbuy"])
        assert state["live_url"] == "https://live.example/session/1" and state["created"] is True
        assert state["profile_id"] == "prof-new" and FakeClient.created == ["p1"]
        assert sessions.current()["session_id"] == "sess-1"
        assert sessions.remaining_seconds() == 3600
        assert any("Log into: costco, bestbuy" in line for line in state["guidance"])
        with pytest.raises(RuntimeError, match="already open"):
            sessions.start("p1")
        # not yet: nothing expires before the limit
        clock["now"] = NOW + timedelta(minutes=59)
        assert sessions.expire_if_due() is None and FakeClient.stopped == []
        closed = sessions.finish()
        assert FakeClient.stopped == ["sess-1"] and closed["saved"].startswith("profile_id saved")
        assert "added bestbuy" in closed["saved"]
        assert config_value("profiles")[0]["profile_id"] == "prof-new"
        assert config_value("profiles")[0]["retailers"] == ["costco", "bestbuy"]
        assert sessions.current() is None and sessions.finish() is None

    def test_a_session_left_open_is_closed_after_the_limit(self, tmp_path, profile_config):
        clock = {"now": NOW}
        sessions = tools.ProfileSessions(tmp_path, minutes=45, clock=lambda: clock["now"],
                                         client_factory=FakeClient, timer=False)
        sessions.start("p1")
        clock["now"] = NOW + timedelta(minutes=46)
        closed = sessions.expire_if_due()
        assert closed["reason"] == "left open for 45 minutes" and FakeClient.stopped == ["sess-1"]
        # a fresh object (a restarted dashboard) sees the same file and nothing to close
        assert tools.ProfileSessions(tmp_path, client_factory=FakeClient, timer=False).current() is None

    def test_a_missing_profile_or_proxy_is_the_operators_message(self, tmp_path, profile_config, config_file):
        sessions = tools.ProfileSessions(tmp_path, client_factory=FakeClient, timer=False)
        with pytest.raises(ValueError, match="No entry with label"):
            sessions.start("nope")
        config_file(profiles=[{"label": "p2", "profile_id": "", "retailers": []}])
        with pytest.raises(ValueError, match="no proxy configured"):
            sessions.start("p2")


class TestTheRoutes:
    @pytest.fixture
    def client(self, tmp_path, profile_config):
        from fastapi.testclient import TestClient

        from config.settings import Settings
        from web.app import create_app
        from web.ledger_reader import SnapshotReader

        logs = tmp_path / "logs"
        logs.mkdir()
        app = create_app(SnapshotReader(data_dir=tmp_path / "empty"), logs_dir=logs, failures_dir=logs,
                         backup_dir=tmp_path / "b", repo_root_dir=tmp_path, clock=lambda: NOW,
                         settings=dataclasses.replace(Settings(), container_run_interval_hours=6,
                                                      web_tool_session_minutes=30),
                         restarter=lambda: None)
        app.state.profile_sessions = tools.ProfileSessions(logs, minutes=30, clock=lambda: NOW,
                                                           client_factory=FakeClient, timer=False)
        started = []

        def launch(command, handle):
            started.append(command)
            import subprocess

            return subprocess.Popen([sys.executable, "-c", "print('ran')"], stdout=handle,
                                    stderr=subprocess.STDOUT, text=True)

        app.state.tool_runner.launch = launch
        test_client = TestClient(app)
        test_client.started = started
        test_client.logs = logs
        return test_client

    def test_the_page_lists_the_tools_and_the_nav_links_it(self, client):
        body = client.get("/tools").text
        assert "<h1>Tools</h1>" in body
        # the header's Tools menu lists every tool, grouped; the profile login is the default panel
        menu = body[body.index('class="multi nav-menu'):body.index("</details>", body.index('class="multi nav-menu'))]
        assert 'href="/tools?tool=profile"' in menu and ">Ledger Fixes<" in menu and ">Checks<" in menu
        for key in ("preflight", "tax_report", "backfill_tracking", "costco_token"):
            assert f'href="/tools?tool={key}"' in menu
            assert f'id="t-{key}"' not in body  # not shown until picked
        assert 'tool=audit_sheet' not in menu  # sheet-only, and the ledger is the database
        assert 'class="multi nav-menu' in client.get("/settings").text  # on every page
        assert "Log a Profile In" in body and 'action="/tools/profile/start"' in body
        assert "after 30 minutes" in body
        assert 'name="label" value="p1" checked' in body
        picked = client.get("/tools", params={"tool": "backfill_tracking"}).text
        assert 'id="t-backfill_tracking"' in picked and 'action="/tools/run/backfill_tracking"' in picked
        assert "Log a Profile In" not in picked and 'href="/tools?tool=backfill_tracking" class="current"' in picked
        assert "Ledger Fixes · Backfill tracking numbers" in picked
        assert "Log a Profile In" in client.get("/tools", params={"tool": "nope"}).text

    def test_running_a_tool_records_it_and_shows_its_output(self, client):
        response = client.post("/tools/run/preflight", data={"--strict": "on"}, follow_redirects=False)
        assert response.status_code == 303 and client.started[0][1:] == ["-m", "scripts.preflight", "--strict"]
        events = activity.read(client.logs / "activity.jsonl")
        assert events[0]["kind"] == "tool" and events[0]["summary"] == "Ran Preflight check: python -m scripts.preflight --strict"
        job = client.app.state.tool_runner.recent()[0]
        job._process.wait()
        import time

        for _ in range(50):
            if not job.running:
                break
            time.sleep(0.05)
        partial = client.get(f"/tools/jobs/{job.id}").text
        assert "ran" in partial and "finished" in partial and "hx-trigger" not in partial
        assert 'id="job-' in client.get("/tools", params={"tool": "preflight"}).text
        assert response.headers["location"] == "/tools?tool=preflight#t-preflight"
        assert client.get("/tools/jobs/nope").status_code == 404
        bad = client.post("/tools/run/tax_report", data={"Year": "soon"})
        assert bad.status_code == 400 and "whole number" in bad.text

    def test_a_writing_tool_is_refused_while_a_run_is_in_progress(self, client):
        (client.logs / ".run.lock").write_text("1", encoding="utf-8")
        refused = client.post("/tools/run/sort_ledger", data={"--apply": "on"})
        assert refused.status_code == 423 and "run is in progress" in refused.text
        assert client.started == []
        ok = client.post("/tools/run/preflight", data={}, follow_redirects=False)  # read-only: fine
        assert ok.status_code == 303

    def test_the_profile_session_is_embedded_closed_and_saved(self, client):
        from config.loader import config_value

        opened = client.post("/tools/profile/start", data={"label": "p1", "add_retailers": "bestbuy"},
                             follow_redirects=False)
        assert opened.status_code == 303
        body = client.get("/tools").text
        assert '<iframe class="live" src="https://live.example/session/1"' in body
        assert 'href="https://live.example/session/1" target="_blank"' in body
        assert 'action="/tools/profile/finish"' in body and 'data-seconds="1800"' in body
        assert "Log into: costco, bestbuy" in body
        again = client.post("/tools/profile/start", data={"label": "p1"})
        assert again.status_code == 400 and "already open" in again.text
        closed = client.post("/tools/profile/finish", follow_redirects=False)
        assert closed.status_code == 303 and "cookies+saved" in closed.headers["location"]
        assert FakeClient.stopped == ["sess-1"]
        assert config_value("profiles")[0]["profile_id"] == "prof-new"
        events = activity.read(client.logs / "activity.jsonl")
        assert events[0]["summary"].startswith("Profile session for p1 closed: closed from the page")
        assert "<iframe" not in client.get("/tools").text
        assert client.post("/tools/profile/finish").status_code == 400

    def test_a_session_past_its_limit_is_closed_on_the_next_visit(self, client):
        client.post("/tools/profile/start", data={"label": "p1"}, follow_redirects=False)
        sessions = client.app.state.profile_sessions
        sessions.clock = lambda: NOW + timedelta(minutes=31)
        body = client.get("/tools").text
        assert "<iframe" not in body and FakeClient.stopped == ["sess-1"]
        assert "left open for 30 minutes" in activity.read(client.logs / "activity.jsonl")[0]["summary"]
