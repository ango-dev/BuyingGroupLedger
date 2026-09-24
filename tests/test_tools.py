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
        with pytest.raises(ValueError, match="required"):
            tools.tool("fix_superseded_shipments").argv({"--apply": "on"})
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
                         settings=dataclasses.replace(Settings(), container_run_interval_hours=6, web_password="",
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
        menu = body[body.index('class="multi nav-menu'):body.index('class="multi nav-menu nav-pick')]
        assert 'href="/tools?tool=profile"' in menu and ">Ledger Fixes<" in menu and ">Checks<" in menu
        # the menu's groups fold: Import, Run and Accounts open, the rest folded
        assert menu.count('<details class="group-fold" open>') == 3 and menu.count('<details class="group-fold">') == 3
        assert menu.index('<details class="group-fold">') > menu.index(">Accounts<")
        picked_menu = client.get("/tools", params={"tool": "backfill_tracking"}).text
        picked_menu = picked_menu[picked_menu.index('class="multi nav-menu'):picked_menu.index('class="multi nav-menu nav-pick')]
        assert picked_menu.count('<details class="group-fold" open>') == 4  # Ledger Fixes opens for its tool
        # the profile login sits inside Accounts, first; the run is its own group, first of all
        assert menu.index(">Run<") < menu.index("tool=run_once") < menu.index(">Accounts<") \
            < menu.index("tool=profile") < menu.index("tool=costco_token")
        for key in ("run_once", "preflight", "bg_probe", "backfill_tracking", "costco_token"):
            assert f'href="/tools?tool={key}"' in menu
            assert f'id="t-{key}"' not in body  # not shown until picked
        # what has its own page, or was a one-time migration, is not a tool
        for gone in ("audit_ledger", "tax_report", "migrate_expected_payout", "migrate_receipts_local"):
            assert f"tool={gone}" not in menu and gone not in {t.key for t in tools.TOOLS}
        assert 'class="multi nav-menu' in client.get("/settings").text  # on every page
        assert "Log a Profile In" in body and 'action="/tools/profile/start"' in body
        assert "after 30 minutes" in body
        assert 'name="label" value="p1" checked' in body
        picked = client.get("/tools", params={"tool": "backfill_tracking"}).text
        assert 'id="t-backfill_tracking"' in picked and 'action="/tools/run/backfill_tracking"' in picked
        assert "<h2>Log a Profile In</h2>" not in picked and 'href="/tools?tool=backfill_tracking" class="current"' in picked
        assert "Ledger Fixes · Backfill tracking numbers" in picked
        assert "Log a Profile In" in client.get("/tools", params={"tool": "nope"}).text

    def test_the_landing_page_carries_its_own_tool_directory(self, client):
        """2026-09-21: the page used to point at the header menu and list nothing itself. The
        All Tools panel lists every tool the menu does, grouped the same, the shown one marked."""
        body = client.get("/tools").text
        panel = body[body.index('id="t-all-tools"'):body.index("</section>", body.index('id="t-all-tools"'))]
        assert ">All Tools<" in panel and ">Run<" in panel and ">Accounts<" in panel and ">Import<" in panel
        assert 'href="/tools/import"' in panel and 'href="/tools?tool=run_once"' in panel
        assert 'href="/tools?tool=profile" class="current"' in panel  # the default pick is marked
        assert "Pick a tool from the" not in body  # the old pointer sentence is gone
        # every group open, each foldable by hand
        assert panel.count('<details class="tool-group" open>') == 6 and '<details class="tool-group">' not in panel
        picked = client.get("/tools", params={"tool": "backfill_tracking"}).text
        panel = picked[picked.index('id="t-all-tools"'):picked.index("</section>", picked.index('id="t-all-tools"'))]
        assert 'href="/tools?tool=backfill_tracking" class="current"' in panel
        # the Import landing carries the directory too, Run importer marked
        landing = client.get("/tools/import").text
        panel = landing[landing.index('id="t-all-tools"'):landing.index("</section>", landing.index('id="t-all-tools"'))]
        assert ">All Tools<" in panel and 'href="/tools?tool=profile"' in panel and panel.count('<details class="tool-group" open>') == 6

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
        # the finished response re-enables the card's Run button out-of-band
        assert '<button type="submit" id="run-preflight" class="small primary" hx-swap-oob="true">Run</button>' in partial
        assert 'id="run-preflight"' in client.get("/tools", params={"tool": "preflight"}).text
        assert 'id="job-' in client.get("/tools", params={"tool": "preflight"}).text
        assert response.headers["location"] == "/tools?tool=preflight#t-preflight"
        assert client.get("/tools/jobs/nope").status_code == 404
        bad = client.post("/tools/run/backfill_receipts", data={"--limit": "soon"})
        assert bad.status_code == 400 and "whole number" in bad.text
        for bad_limit in ("-1", "0", "10000000"):  # -1 was accepted
            bad = client.post("/tools/run/backfill_receipts", data={"--limit": bad_limit})
            assert bad.status_code == 400 and "from 1 to 1,000,000" in bad.text, bad_limit

    def test_a_pasted_token_reaches_the_script_and_nothing_that_is_shown(self, client):
        """The Costco token field is a credential: the subprocess gets it, while the activity
        record, the job log's command line and the job page carry a mask -- and the form asks
        for it in a password box."""
        secret = "rt-SECRET-0123456789"
        response = client.post("/tools/run/costco_token", data={"--label": "profile-1", "--token": secret},
                               follow_redirects=False)
        assert response.status_code == 303
        assert client.started[0][-2:] == ["--token", secret]  # the script itself gets the real value
        raw_activity = (client.logs / "activity.jsonl").read_text(encoding="utf-8")
        assert secret not in raw_activity and tools.SECRET_MASK in raw_activity
        job = client.app.state.tool_runner.recent()[0]
        job._process.wait()
        assert secret not in job.log_path.read_text(encoding="utf-8") and secret not in " ".join(job.argv)
        assert secret not in client.get(f"/tools/jobs/{job.id}").text
        page = client.get("/tools", params={"tool": "costco_token"}).text
        assert 'type="password" name="--token"' in page and 'type="text" name="--label"' in page

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


class TestRunOnce:
    client = TestTheRoutes.client  # the same dashboard as the route tests

    """`Run once` is `python -m main [retailer]`: a real run, confirmed, never under a live lock."""

    def test_the_tool_is_main_itself(self):
        t = tools.tool("run_once")
        assert t.module == "main" and t.group == "Run" and t.writes and t.spends and t.heartbeat
        assert not any(o.heartbeat for o in tools.TOOLS if o.key != "run_once")
        assert t.argv({}) == [] and t.argv({"Retailer": "costco"}) == ["costco"]
        with pytest.raises(ValueError, match="not one of"):
            t.argv({"Retailer": "walmart"})

    def test_it_is_refused_while_a_run_holds_the_lock(self, client, tmp_path):
        logs_dir = tmp_path / "logs"
        (logs_dir / ".run.lock").write_text("pid 1", encoding="utf-8")
        response = client.post("/tools/run/run_once", data={"Retailer": ""}, follow_redirects=False)
        assert response.status_code == 423 and "run is in progress" in response.text
        assert not list((logs_dir / "tools").glob("*run_once*.log")) if (logs_dir / "tools").is_dir() else True

    def test_the_page_names_what_it_costs(self, client):
        body = client.get("/tools", params={"tool": "run_once"}).text
        assert 'id="t-run_once"' in body and "python -m main" in body
        assert "submits tracking numbers and files insurance" in body and 'class="tag stale"' in body
        assert 'data-param="Retailer"' in body

    def test_a_finished_run_stamps_the_heartbeat_like_the_cron_wrapper(self, tmp_path):
        """The tool holds the same run lock the schedule checks, so its run is a run: the
        heartbeat is written when it ends, whatever the exit code (run_once.sh's rule)."""
        import subprocess
        import time

        from web.heartbeat import read_heartbeat

        runner = tools.JobRunner(tmp_path, clock=lambda: NOW)
        stamped = tools.Tool("r", "unused", "R", "", "Run", heartbeat=True)
        plain = tools.Tool("p", "unused", "P", "", "Checks")
        runner.launch = lambda command, handle: subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.exit(3)"], stdout=handle, stderr=subprocess.STDOUT, text=True)
        job = runner.start(plain, [])
        job._process.wait()
        for _ in range(50):
            if not job.running:
                break
            time.sleep(0.05)
        assert not (tmp_path / ".last_run").exists()
        job = runner.start(stamped, [])
        job._process.wait()
        for _ in range(50):
            if job.finished_at and (tmp_path / ".last_run").exists():
                break
            time.sleep(0.05)
        assert job.returncode == 3
        assert (tmp_path / ".last_run").read_text(encoding="utf-8") == "2026-09-18T12:00:00Z"
        assert read_heartbeat(tmp_path, now=NOW, interval_hours=6)["stale"] is False
