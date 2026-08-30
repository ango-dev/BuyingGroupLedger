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


def _run(tmp_path, *, stamp_age_s=None, started_age_s=None, hours=1):
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
           "UNHEALTHY_MARKER": str(marker), "NOTIFY_CMD": f"bash {notify}", "RUN_INTERVAL_HOURS": str(hours)}
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
