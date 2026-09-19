"""The Google Sheet is gone (2026-09-18): nothing in the repo may import gspread or Google's
auth, and requirements.txt names neither package. A scan, so a stray import cannot creep back."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".venv", "venv", ".git", "node_modules", "__pycache__", "logs", "backups", "data"}
FORBIDDEN = ("import gspread", "from gspread", "google.oauth2", "googleapiclient")


def _tracked_python_files():
    for path in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS or part.startswith(".") for part in path.relative_to(ROOT).parts[:-1]):
            continue
        yield path


def test_no_python_file_imports_gspread_or_google_auth():
    offenders = []
    for path in _tracked_python_files():
        if path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for token in FORBIDDEN:
            if token in text:
                offenders.append(f"{path.relative_to(ROOT)}: {token!r}")
    assert not offenders, "\n".join(offenders)


def test_requirements_name_neither_package():
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    names = {re.split(r"[<>=!~\[; ]", line.strip(), maxsplit=1)[0].lower()
             for line in text.splitlines() if line.strip() and not line.startswith("#")}
    assert "gspread" not in names and "google-auth" not in names
