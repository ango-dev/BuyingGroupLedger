"""The activity log: one line per thing the app DID -- a run's scrapes and ledger writes, what the
buying-group sync submitted, insured and read back, the emails the auto-reply sent, every alert,
every failure dossier, the end-of-run mirror, and every change made from the dashboard (cell edits,
added / deleted rows, receipts, backups, settings). The dashboard's Activity page is a filterable
table over it.

FORMAT: `logs/activity.jsonl`, append-only, one JSON object per line:

    {"at": "<UTC ISO>", "kind": "<KINDS>", "summary": "<one line>", "run_id": "<run-...>|null",
     "details": {...}}

Recorded AT THE SOURCE -- alerts.notifier.alert records the alert, FailureDossier.write records
the dossier, main's steps record what each step did -- so nothing has to be reconstructed from
run.log afterwards. `record` never raises: a log that fails to write must not fail the run.
`run_id` groups a scheduled run's events; main.begin_run sets it, the dashboard (a separate
process) has none.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
ACTIVITY_FILE = ROOT / "logs" / "activity.jsonl"

#: Every kind the log carries, with the label the page shows. Order = the filter's order.
KINDS: dict[str, str] = {
    "run": "Run",
    "scrape": "Scrape",
    "ledger": "Ledger write",
    "sync": "Buying-group sync",
    "reply": "Email reply",
    "alert": "Alert",
    "health": "Container health",
    "dossier": "Failure dossier",
    "mirror": "DB mirror",
    "edit": "Dashboard edit",
    "backup": "Backup",
    "settings": "Settings",
}

_run_id: str | None = None
_MAX_DETAIL_CHARS = 4000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def current_run_id() -> str | None:
    return _run_id


def begin_run(what: str = "") -> str:
    """Start grouping events under one run id (main.main). Records the run's start."""
    global _run_id
    _run_id = "run-" + _now().strftime("%Y%m%dT%H%M%SZ")
    record("run", f"Run started{': ' + what if what else ''}")
    return _run_id


def end_run(summary: str = "Run finished") -> None:
    global _run_id
    record("run", summary)
    _run_id = None


def _trim(value: Any) -> Any:
    """Details must stay JSON and small: strings are cut, unknown objects become their str()."""
    if isinstance(value, dict):
        return {str(k): _trim(v) for k, v in list(value.items())[:60]}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_trim(v) for v in list(value)[:60]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    text = str(value)
    return text if len(text) <= _MAX_DETAIL_CHARS else text[:_MAX_DETAIL_CHARS] + " …"


def record(kind: str, summary: str, details: dict | None = None, *, path: Path | None = None,
           run_id: str | None = None, at: datetime | None = None) -> dict | None:
    """Append one event. Returns the event, or None when it could not be written (logged, never
    raised)."""
    if kind not in KINDS:
        log.warning("activity: unknown kind %r (recorded anyway)", kind)
    event = {
        "at": (at or _now()).isoformat(timespec="seconds"),
        "kind": kind,
        "summary": str(summary).strip()[:500],
        "run_id": run_id if run_id is not None else _run_id,
        "details": _trim(details or {}),
    }
    target = Path(path) if path else ACTIVITY_FILE
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        log.warning("activity: could not append to %s", target, exc_info=True)
        return None
    return event


def read(path: Path | None = None) -> list[dict]:
    """Every event, NEWEST FIRST. A line that is not JSON is skipped, never fatal."""
    target = Path(path) if path else ACTIVITY_FILE
    if not target.is_file():
        return []
    events: list[dict] = []
    with target.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and "at" in event and "kind" in event:
                event.setdefault("summary", "")
                event.setdefault("run_id", None)
                event.setdefault("details", {})
                events.append(event)
    events.reverse()
    return events


def filter_events(events: list[dict], *, kinds: tuple[str, ...] = (), q: str = "", days: int = 0,
                  run_id: str = "", now: datetime | None = None,
                  hidden: tuple[str, ...] = ()) -> list[dict]:
    """Narrow a read: by kind (any of), a case-insensitive text over summary + details, a window
    of the last N days (0 = all), or one run. `hidden` kinds are left out unless `kinds` names
    them explicitly (the page's suppressed types)."""
    needle = (q or "").strip().lower()
    cutoff = (now or _now()) - timedelta(days=days) if days else None
    out = []
    for event in events:
        if kinds and event.get("kind") not in kinds:
            continue
        if not kinds and hidden and event.get("kind") in hidden:
            continue
        if run_id and event.get("run_id") != run_id:
            continue
        if cutoff is not None:
            try:
                at = datetime.fromisoformat(str(event.get("at")))
                if at.tzinfo is None:
                    at = at.replace(tzinfo=timezone.utc)
            except ValueError:
                at = None
            if at is not None and at < cutoff:
                continue
        if needle:
            haystack = (str(event.get("summary", "")) + " "
                        + json.dumps(event.get("details", {}), ensure_ascii=False)).lower()
            if needle not in haystack:
                continue
        out.append(event)
    return out


def counts_by_kind(events: list[dict]) -> dict[str, int]:
    out = {k: 0 for k in KINDS}
    for event in events:
        out[event.get("kind", "")] = out.get(event.get("kind", ""), 0) + 1
    return out


_RUN_STAMP = re.compile(r"^run-(\d{8}T\d{6}Z)$")


def run_started_at(run_id: str | None) -> str:
    """"run-20260918T140501Z" -> "2026-09-18T14:05:01+00:00" (the page renders it in local time)."""
    match = _RUN_STAMP.match(run_id or "")
    if not match:
        return ""
    s = match.group(1)
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}T{s[9:11]}:{s[11:13]}:{s[13:15]}+00:00"


def run_label(run_id: str | None) -> str:
    """"run-20260918T140501Z" -> "09-18 14:05" for a table cell."""
    match = _RUN_STAMP.match(run_id or "")
    if not match:
        return run_id or ""
    stamp = match.group(1)
    return f"{stamp[4:6]}-{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}"
