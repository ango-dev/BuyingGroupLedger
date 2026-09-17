"""docker/healthcheck.sh -- the unhealthy state must reach someone, once per episode.

Runs the real script under bash with every path overridden, and a fake NOTIFY_CMD that records
its arguments instead of sending. Skipped where bash is not available.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "docker" / "healthcheck.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")


def _run(tmp_path, *, stamp_age_s=None, started_age_s=None, hours=1, web="false", web_url=None):
    stamp = tmp_path / ".last_run"
    started = tmp_path / ".started"
    marker = tmp_path / ".unhealthy_alerted"
    sent = tmp_path / "sent.log"
    notify = tmp_path / "notify.sh"
    notify.write_text('#!/usr/bin/env bash\nprintf \'%s\n\' "$1" "$2" >> "' + str(sent).replace("\\", "/") + '"\n')
    if sent.exists():
        sent.unlink()  # each call reports only what THIS check sent
    now = time.time()
    if stamp_age_s is not None:
        stamp.write_text("x"); os.utime(stamp, (now - stamp_age_s, now - stamp_age_s))
    if started_age_s is not None:
        started.write_text("x"); os.utime(started, (now - started_age_s, now - started_age_s))
    env = {**os.environ, "HEARTBEAT_FILE": str(stamp), "STARTED_FILE": str(started),
           "UNHEALTHY_MARKER": str(marker), "NOTIFY_CMD": f"bash {notify}", "RUN_INTERVAL_HOURS": str(hours),
           # The dashboard probe (2026-09-17): off unless a test is about it, and the entrypoint's
           # resolved-settings file must not leak in from a real container.
           "WEB_ENABLED": web, "WEB_HEALTH_URL": web_url or "http://127.0.0.1:9/health",
           "CONTAINER_ENV_FILE": str(tmp_path / "no-container.env")}
    proc = subprocess.run([BASH, str(SCRIPT)], env=env, capture_output=True, text=True)
    return proc, marker, (sent.read_text().splitlines() if sent.exists() else [])


def test_a_fresh_heartbeat_is_healthy_and_silent(tmp_path):
    proc, marker, sent = _run(tmp_path, stamp_age_s=60)
    assert proc.returncode == 0 and "last run 1m ago" in proc.stdout
    assert not marker.exists() and sent == []


def test_a_stale_heartbeat_alerts_once_then_stays_quiet(tmp_path):
    proc, marker, sent = _run(tmp_path, stamp_age_s=3 * 3600, hours=1)   # grace is 2h
    assert proc.returncode == 1 and "expected one every 1h" in proc.stdout
    assert marker.exists()
    assert len(sent) == 2 and sent[0].startswith("Ledger container UNHEALTHY") and "logs/.run.lock" in sent[1]

    proc, marker, sent = _run(tmp_path, stamp_age_s=3 * 3600, hours=1)   # second check: no second alert
    assert proc.returncode == 1 and sent == [] and marker.exists()


def test_recovery_sends_the_all_clear_and_clears_the_marker(tmp_path):
    _run(tmp_path, stamp_age_s=3 * 3600, hours=1)
    proc, marker, sent = _run(tmp_path, stamp_age_s=30, hours=1)
    assert proc.returncode == 0
    assert sent and sent[0] == "Ledger container healthy again" and "unhealthy since" in sent[1]
    assert not marker.exists()


def test_no_run_yet_on_a_fresh_container_is_healthy(tmp_path):
    proc, marker, sent = _run(tmp_path, started_age_s=600, hours=1)
    assert proc.returncode == 0 and "first run due" in proc.stdout and sent == []


def test_no_run_long_after_start_alerts(tmp_path):
    proc, marker, sent = _run(tmp_path, started_age_s=3 * 3600, hours=1)
    assert proc.returncode == 1 and "NO run has completed" in proc.stdout and marker.exists() and sent


# --- the dashboard probe (one container, 2026-09-17) ------------------------------------------


def _serve_health(tmp_path):
    """A throwaway HTTP server answering /health, on a free loopback port."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/health"


def test_a_live_dashboard_keeps_the_container_healthy(tmp_path):
    server, url = _serve_health(tmp_path)
    try:
        proc, marker, sent = _run(tmp_path, stamp_age_s=60, web="true", web_url=url)
    finally:
        server.shutdown()
    assert proc.returncode == 0 and "last run 1m ago" in proc.stdout and sent == []


def test_a_dead_dashboard_is_unhealthy_and_says_so_not_the_scheduler(tmp_path):
    proc, marker, sent = _run(tmp_path, stamp_age_s=60, web="true")  # port 9: nothing listens
    assert proc.returncode == 1
    assert "web dashboard is not answering" in proc.stdout and "scheduler fine" in proc.stdout
    assert marker.exists() and len(sent) == 2 and "web dashboard" in sent[1]


def test_the_probe_is_skipped_when_the_dashboard_is_disabled(tmp_path):
    proc, marker, sent = _run(tmp_path, stamp_age_s=60, web="false")
    assert proc.returncode == 0 and sent == []
