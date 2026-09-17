"""The scheduler heartbeat, read the way docker/healthcheck.sh reads it.

`logs/.last_run` is stamped by every completed run (docker/run_once.sh, run.sh, run.ps1) with a
UTC timestamp. Its age is the only signal that says "the scheduler stopped firing" -- a failure that
otherwise produces nothing at all: no error, no log line, no alert. The threshold is the health
check's: two run intervals, so one overrun or one lock-skipped run is not an alarm.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = ROOT / "logs"
STAMP_NAME = ".last_run"


def _parse_stamp(text: str) -> datetime | None:
    text = text.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def humanize(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


def read_heartbeat(logs_dir: Path = LOGS_DIR, *, now: datetime | None = None,
                   interval_hours: float = 6) -> dict:
    """{present, at, age_seconds, age_text, stale, threshold_seconds, interval_hours, message}.

    Missing stamp = no run has ever completed on this host, reported as stale with a message that
    says so rather than as an error: on a development PC that is simply the normal state.
    """
    now = now or datetime.now(timezone.utc)
    threshold = int(interval_hours * 2 * 3600)
    stamp = Path(logs_dir) / STAMP_NAME
    base = {"present": False, "at": None, "age_seconds": None, "age_text": None,
            "stale": True, "threshold_seconds": threshold, "interval_hours": interval_hours,
            "path": str(stamp)}
    if not stamp.is_file():
        return {**base, "message": f"no run has completed yet ({stamp.name} absent)"}
    try:
        at = _parse_stamp(stamp.read_text(encoding="utf-8"))
    except OSError:
        at = None
    if at is None:  # an unparseable stamp: the file's own mtime is the next best evidence
        at = datetime.fromtimestamp(stamp.stat().st_mtime, timezone.utc)
    age = max(0.0, (now - at).total_seconds())
    stale = age > threshold
    message = (f"last run {humanize(age)} ago; expected one every {interval_hours:g}h"
               if stale else f"last run {humanize(age)} ago")
    return {**base, "present": True, "at": at.isoformat(), "age_seconds": int(age),
            "age_text": humanize(age), "stale": stale, "message": message}
