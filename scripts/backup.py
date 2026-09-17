"""Back up the app's local state to one zip, and restore it on a fresh clone. STANDARD LIBRARY ONLY.

    python -m scripts.backup                        # -> backups/ledger_backup_<UTC stamp>.zip
    python -m scripts.backup --list
    python -m scripts.backup --restore backups/ledger_backup_20260917T120000Z.zip
    python -m scripts.backup --restore FILE --force # overwrite files that already exist

WHAT IS IN IT: everything a `git clone` does not give you --

    config.json          every credential and profile (the one file you author)
    .state.json          Costco's rotating refresh token (the one file the app rewrites)
    .env                 host overrides, if present
    data/**              the SQLite copy of the ledger, the CSV sheet backups, audit snapshots
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
import os
import socket
import subprocess
import sys
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
_SKIP_SUFFIXES = (".pyc",)
_SKIP_DIRS = ("__pycache__",)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _git_commit(root: Path) -> str:
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
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
            if any(part in _SKIP_DIRS for part in rel.parts) or rel.suffix in _SKIP_SUFFIXES:
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
            archive.write(root / rel, rel.as_posix())
        archive.writestr(MANIFEST, json.dumps(manifest, indent=2))
    return target


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
    Returns {"restored": [...], "skipped_existing": [...], "ignored": [...]}."""
    root = Path(root) if root else ROOT
    restored, skipped, ignored = [], [], []
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            target = _safe_target(root, member.filename)
            if target is None:
                if member.filename != MANIFEST:
                    ignored.append(member.filename)
                continue
            if target.exists() and not force:
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
    args = parser.parse_args(argv)

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

    target = create_backup(out_dir=Path(args.out_dir) if args.out_dir else None)
    manifest = read_manifest(target)
    print(f"Wrote {target} ({target.stat().st_size / 1024:.0f} KB, {len(manifest['files'])} file(s)). "
          "It holds every live credential -- keep it private.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    os.chdir(ROOT)
    raise SystemExit(main())
