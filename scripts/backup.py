"""Back up the app's local state to one zip, and restore it on a fresh clone. STANDARD LIBRARY ONLY.

    python -m scripts.backup                        # -> backups/ledger_backup_<UTC stamp>.zip
    python -m scripts.backup --list
    python -m scripts.backup --restore backups/ledger_backup_20260917T120000Z.zip
    python -m scripts.backup --restore FILE --force # overwrite files that already exist
    python -m scripts.backup --scheduled            # the container's cron job: create, then keep
                                                    # the newest `backups.keep` zips (config.json)
    python -m scripts.backup --prune [--keep N]     # apply the retention rule now
    python -m scripts.backup --print-cron           # the schedule as a cron expression ("" = off)

WHAT IS IN IT: everything a `git clone` does not give you --

    config.json          every credential and profile (the one file you author)
    .state.json          Costco's rotating refresh token (the one file the app rewrites)
    .env                 host overrides, if present
    data/**              the SQLite ledger, the CSV ledger backups, audit snapshots
    backup_manifest.json when, from which commit, and the file list

NOT in it: `logs/` (run logs and failure dossiers -- host-local, and the dossiers hold captured
pages), `.venv/`, and the code (that is what git is for). The archive holds every live secret, so
treat it like config.json itself: keep it off shared drives and out of git (`backups/` is ignored).

MOVING TO A NEW MACHINE: `git clone`, then `python -m scripts.backup --restore <zip>` with the
system Python (no venv or pip needed -- this file imports nothing outside the standard library),
then the normal setup. Restore refuses to overwrite an existing file unless --force: the intended
target is an empty clone, and a live host's config.json must never be replaced by accident. The web
dashboard offers the same two operations on its Backup page (restore there only while no
config.json exists -- the fresh-clone case).
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import os
import socket
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUPS_DIR = ROOT / "backups"
MANIFEST = "backup_manifest.json"
PREFIX = "ledger_backup_"

#: Single files at the repo root, included when present.
ROOT_FILES = ("config.json", ".state.json", ".env")
#: Directories included whole (relative to the root), when present.
DIRS = ("data",)
#: Never restored anywhere but under the repo root; never anything with a path component that
#: climbs. Enforced on restore, whatever the archive says.
# SQLite's side files: a backup copies the database through SQLite's own backup API (a consistent
# snapshot even mid-transaction), so a journal from another moment would only confuse a restore.
_SKIP_SUFFIXES = (".pyc", "-wal", "-shm", "-journal")
_SQLITE_MAGIC = b"SQLite format 3\x00"
_SKIP_DIRS = ("__pycache__",)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _git_commit(root: Path) -> str:
    """The short commit the code at `root` is, by asking git; failing that (inside the Docker
    image there is no git and no working tree) from GIT_COMMIT in the environment, or from the
    .git/HEAD and ref files .dockerignore lets into the image."""
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    from_env = os.environ.get("GIT_COMMIT", "").strip()
    if from_env:
        return from_env[:12]
    return _commit_from_git_files(root)


def _commit_from_git_files(root: Path) -> str:
    """Resolve HEAD by reading .git/HEAD -> .git/refs/heads/<branch> (or packed-refs)."""
    try:
        head = (root / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not head.startswith("ref:"):
        return head[:7]
    ref = head.split(":", 1)[1].strip()
    try:
        return (root / ".git" / ref).read_text(encoding="utf-8").strip()[:7]
    except OSError:
        pass
    try:
        for line in (root / ".git" / "packed-refs").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                return parts[0][:7]
    except OSError:
        pass
    return ""


def backup_members(root: Path | None = None) -> list[Path]:
    """The files a backup would hold, as paths relative to `root`, in a stable order."""
    root = Path(root) if root else ROOT
    members: list[Path] = []
    for name in ROOT_FILES:
        if (root / name).is_file():
            members.append(Path(name))
    for directory in DIRS:
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if any(part in _SKIP_DIRS for part in rel.parts) or rel.name.endswith(_SKIP_SUFFIXES):
                continue
            members.append(rel)
    return members


def create_backup(root: Path | None = None, out_dir: Path | None = None) -> Path:
    """Write backups/ledger_backup_<stamp>.zip and return its path."""
    root = Path(root) if root else ROOT
    out_dir = Path(out_dir) if out_dir else root / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{PREFIX}{_stamp()}.zip"
    members = backup_members(root)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": socket.gethostname(),
        "git_commit": _git_commit(root),
        "files": [m.as_posix() for m in members],
    }
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for rel in members:
            _write_member(archive, root / rel, rel.as_posix())
        archive.writestr(MANIFEST, json.dumps(manifest, indent=2))
    return target


def _is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(len(_SQLITE_MAGIC)) == _SQLITE_MAGIC
    except OSError:
        return False


def _write_member(archive: zipfile.ZipFile, path: Path, arcname: str) -> None:
    """Add one file. A SQLite database (the ledger) goes in as a
    copy made by SQLite's online backup API, opened read-only: a scheduled backup may land while a
    run is writing the ledger, and a byte copy of a file mid-transaction is not a database."""
    if not _is_sqlite(path):
        archive.write(path, arcname)
        return
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / path.name
        source = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            target = sqlite3.connect(copy)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        archive.write(copy, arcname)


def prune_backups(out_dir: Path | None = None, keep: int = 0) -> list[Path]:
    """Delete every zip beyond the newest `keep` (by name, which is the UTC stamp); `keep` <= 0
    keeps everything. Returns what was deleted, oldest last."""
    if keep <= 0:
        return []
    deleted = []
    for path in list_backups(out_dir)[keep:]:
        path.unlink()
        deleted.append(path)
    return deleted


# --------------------------------------------------------------------------------------------------
# The schedule: backups.* in config.json -> a cron line for the container's own scheduler
# --------------------------------------------------------------------------------------------------

FREQUENCIES = ("daily", "weekly", "monthly")
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_CRON_DOW = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 0}
_LONG_DAY = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
             "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}
DEFAULT_TIME = "03:30"
DEFAULT_KEEP = 14


def parse_time(text: str) -> tuple[int, int]:
    """"HH:MM" (24-hour) -> (hour, minute); ValueError otherwise."""
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", text or "")
    if not match:
        raise ValueError(f"{text!r} is not a time of day (write HH:MM, e.g. 03:30)")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"{text!r} is not a time of day (HH is 00-23, MM is 00-59)")
    return hour, minute


def parse_days(frequency: str, text: str) -> tuple:
    """The days a weekly / monthly schedule fires on: day names ("mon,thu") or days of the month
    ("1,15"). Blank = Sunday / the 1st. Daily ignores it. ValueError when it fits neither."""
    tokens = [t.strip().lower() for t in re.split(r"[,\s;]+", text or "") if t.strip()]
    if frequency == "weekly":
        if not tokens:
            return ("sun",)
        names = tuple(t[:3] for t in tokens)
        bad = [t for t in names if t not in DAY_NAMES]
        if bad:
            raise ValueError(f"{text!r}: weekly days are day names (mon, tue, ... sun)")
        return tuple(dict.fromkeys(names))
    if frequency == "monthly":
        if not tokens:
            return (1,)
        try:
            numbers = tuple(dict.fromkeys(int(t) for t in tokens))
        except ValueError:
            raise ValueError(f"{text!r}: monthly days are days of the month (1-28)") from None
        if any(n < 1 or n > 28 for n in numbers):
            raise ValueError(f"{text!r}: a monthly day must be 1-28 so it exists in every month")
        return numbers
    return ()


def parse_days_any(text: str) -> str:
    """Validation without knowing the frequency (the Settings page checks one field at a time):
    blank, day names, or days of the month. Returns the text as typed."""
    if not (text or "").strip():
        return ""
    errors = []
    for frequency in ("weekly", "monthly"):
        try:
            parse_days(frequency, text)
            return text.strip()
        except ValueError as exc:
            errors.append(str(exc))
    raise ValueError("write day names (mon,thu) for weekly or days of the month (1,15) for monthly")


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _join(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


class Schedule:
    """backups.* resolved: never invalid -- a bad value falls back and is named in `problems`."""

    def __init__(self, enabled: bool = True, frequency: str = "daily", time: str = DEFAULT_TIME,
                 days: str = "", keep: int = DEFAULT_KEEP):
        problems: list[str] = []
        self.enabled = bool(enabled)
        frequency = str(frequency or "daily").strip().lower()
        if frequency not in FREQUENCIES:
            problems.append(f"backups.frequency {frequency!r} is not daily / weekly / monthly; using daily")
            frequency = "daily"
        self.frequency = frequency
        try:
            self.hour, self.minute = parse_time(str(time))
        except ValueError as exc:
            problems.append(f"backups.time: {exc}; using {DEFAULT_TIME}")
            self.hour, self.minute = parse_time(DEFAULT_TIME)
        try:
            self.days = parse_days(frequency, str(days))
        except ValueError as exc:
            problems.append(f"backups.days: {exc}; using the default")
            self.days = parse_days(frequency, "")
        try:
            self.keep = max(0, int(keep))
        except (TypeError, ValueError):
            problems.append(f"backups.keep {keep!r} is not a whole number; using {DEFAULT_KEEP}")
            self.keep = DEFAULT_KEEP
        self.problems = tuple(problems)

    @property
    def time(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    def cron(self) -> str:
        """The five-field cron expression (the container's TZ applies)."""
        if self.frequency == "weekly":
            return f"{self.minute} {self.hour} * * {','.join(str(_CRON_DOW[d]) for d in self.days)}"
        if self.frequency == "monthly":
            return f"{self.minute} {self.hour} {','.join(str(d) for d in self.days)} * *"
        return f"{self.minute} {self.hour} * * *"

    def describe(self) -> str:
        if self.frequency == "weekly":
            when = f"every {_join([_LONG_DAY[d] for d in self.days])}"
        elif self.frequency == "monthly":
            when = f"on the {_join([_ordinal(d) for d in self.days])} of each month"
        else:
            when = "every day"
        return f"{when} at {self.time}"


def schedule_from_settings(settings) -> Schedule:
    return Schedule(enabled=settings.backups_enabled, frequency=settings.backups_frequency,
                    time=settings.backups_time, days=settings.backups_days, keep=settings.backups_keep)


def run_scheduled(root: Path | None = None, out_dir: Path | None = None) -> int:
    """The cron job: one backup, then the retention rule, recorded in the activity log; a failure
    ALERTS (a backup that silently stopped is the failure nobody notices until the day it is
    needed). Reads the schedule from the settings, so this is the one entry point that is not
    standard-library-only -- it runs inside the app's own environment."""
    from config.settings import settings

    schedule = schedule_from_settings(settings)
    for problem in schedule.problems:
        print(f"warning: {problem}", file=sys.stderr)
    if not schedule.enabled:
        print("Scheduled backups are off (backups.enabled = false); nothing written.")
        return 0
    root = Path(root) if root else ROOT
    out_dir = Path(out_dir) if out_dir else root / "backups"
    try:
        target = create_backup(root, out_dir)
        deleted = prune_backups(out_dir, schedule.keep)
    except Exception as exc:
        from alerts.notifier import alert

        alert("Scheduled backup FAILED",
              f"The scheduled backup could not be written: {type(exc).__name__}: {exc}\n"
              f"Backups land in {out_dir}; the schedule is {schedule.describe()}. Make one from the "
              "dashboard's Settings page to check the path, and look at docker compose logs.",
              kind="backup")
        print(f"Scheduled backup failed: {exc}", file=sys.stderr)
        return 1
    from diagnostics import activity

    manifest = read_manifest(target)
    activity.record("backup", f"Scheduled backup {target.name} created"
                    + (f"; {len(deleted)} older one(s) deleted" if deleted else ""),
                    {"name": target.name, "size_kb": target.stat().st_size // 1024,
                     "files": len(manifest.get("files", [])), "keep": schedule.keep,
                     "deleted": [p.name for p in deleted], "scheduled": True})
    print(f"Wrote {target} ({target.stat().st_size / 1024:.0f} KB); keeping the newest "
          f"{schedule.keep or 'all'}" + (f", deleted {len(deleted)}" if deleted else "") + ".")
    return 0


def list_backups(out_dir: Path | None = None) -> list[Path]:
    out_dir = Path(out_dir) if out_dir else ROOT / "backups"
    if not out_dir.is_dir():
        return []
    return sorted(out_dir.glob(f"{PREFIX}*.zip"), reverse=True)


def read_manifest(archive_path: Path) -> dict:
    with zipfile.ZipFile(archive_path) as archive:
        try:
            return json.loads(archive.read(MANIFEST).decode("utf-8"))
        except KeyError:
            return {}


def _safe_target(root: Path, name: str) -> Path | None:
    """The path a member restores to, or None if it must be ignored (the manifest, a climbing or
    absolute path, anything outside the allowed set)."""
    if name == MANIFEST or name.endswith("/"):
        return None
    rel = Path(name)
    if rel.is_absolute() or ".." in rel.parts:
        return None
    if rel.parts[0] in ROOT_FILES and len(rel.parts) == 1:
        return root / rel
    if rel.parts[0] in DIRS:
        return root / rel
    return None


def restore_backup(archive_path: Path, root: Path | None = None, *, force: bool = False) -> dict:
    """Write the archive's files under `root`. Existing files are left alone unless `force`.
    Returns {"restored": [...], "skipped_existing": [...], "ignored": [...]}.

    An EMPTY existing file does not count as existing: a fresh Docker host `touch`es config.json
    and .state.json before its first start (the bind mounts need a file to exist), and the wizard's
    Restore step must fill those stubs without the overwrite tick -- a zero-byte file holds nothing
    to keep."""
    root = Path(root) if root else ROOT
    restored, skipped, ignored = [], [], []
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            target = _safe_target(root, member.filename)
            if target is None:
                if member.filename != MANIFEST:
                    ignored.append(member.filename)
                continue
            if target.exists() and not force and not (target.is_file() and target.stat().st_size == 0):
                skipped.append(member.filename)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as src, open(target, "wb") as dst:
                dst.write(src.read())
            restored.append(member.filename)
    return {"restored": restored, "skipped_existing": skipped, "ignored": ignored}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.backup", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="list existing backups")
    parser.add_argument("--restore", metavar="ZIP", default=None, help="restore this archive")
    parser.add_argument("--force", action="store_true",
                        help="with --restore: overwrite files that already exist")
    parser.add_argument("--out-dir", default=None, help=f"where to write (default {BACKUPS_DIR})")
    parser.add_argument("--scheduled", action="store_true",
                        help="the cron job: back up, then keep only the newest backups.keep zips")
    parser.add_argument("--prune", action="store_true", help="apply the retention rule now")
    parser.add_argument("--keep", type=int, default=None,
                        help="with --prune: how many to keep (default: backups.keep from config.json)")
    parser.add_argument("--print-cron", action="store_true",
                        help="print the schedule as a cron expression (blank when disabled)")
    parser.add_argument("--describe", action="store_true", help="print the schedule in words")
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir) if args.out_dir else None

    if args.print_cron or args.describe:
        from config.settings import settings

        schedule = schedule_from_settings(settings)
        for problem in schedule.problems:
            print(f"warning: {problem}", file=sys.stderr)
        if args.print_cron:
            print(schedule.cron() if schedule.enabled else "")
        else:
            print(schedule.describe() if schedule.enabled else "off")
        return 0

    if args.scheduled:
        return run_scheduled(out_dir=out_dir)

    if args.prune:
        keep = args.keep
        if keep is None:
            from config.settings import settings

            keep = schedule_from_settings(settings).keep
        deleted = prune_backups(out_dir or BACKUPS_DIR, keep)
        for path in deleted:
            print(f"deleted  {path.name}")
        print(f"{len(deleted)} deleted; keeping the newest {keep or 'all'}.", file=sys.stderr)
        return 0

    if args.list:
        for path in list_backups(Path(args.out_dir) if args.out_dir else BACKUPS_DIR):
            manifest = read_manifest(path)
            print(f"{path.name}  {path.stat().st_size / 1024:.0f} KB  "
                  f"{manifest.get('created_at', '?')}  commit {manifest.get('git_commit') or '?'}  "
                  f"{len(manifest.get('files', []))} file(s)")
        return 0

    if args.restore:
        archive = Path(args.restore)
        if not archive.is_file():
            print(f"No such archive: {archive}", file=sys.stderr)
            return 2
        result = restore_backup(archive, force=args.force)
        for name in result["restored"]:
            print(f"restored  {name}")
        for name in result["skipped_existing"]:
            print(f"kept      {name}  (exists; --force to overwrite)")
        for name in result["ignored"]:
            print(f"ignored   {name}  (not a backup member)")
        print(f"{len(result['restored'])} restored, {len(result['skipped_existing'])} kept, "
              f"{len(result['ignored'])} ignored", file=sys.stderr)
        return 0

    target = create_backup(out_dir=out_dir)
    manifest = read_manifest(target)
    print(f"Wrote {target} ({target.stat().st_size / 1024:.0f} KB, {len(manifest['files'])} file(s)). "
          "It holds every live credential -- keep it private.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    os.chdir(ROOT)
    raise SystemExit(main())
