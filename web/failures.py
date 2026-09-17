"""The failure dossiers under logs/failures/, for the /failures page. Read-only.

A dossier is a directory `<retailer>_<profile>_<YYYYMMDDTHHMMSSZ>` (diagnostics/dossier.py) holding
report.md plus page_N.html / page_N.png / response_N.txt. The report's "Hosted copies" section, when
the dossier was uploaded, lists each file's object-storage link; those links are the thing an alert
carries, so the page shows them VERBATIM -- never rewritten, never re-derived -- next to the
rendered report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from diagnostics.dossier import FAILURES_DIR

_STAMP = re.compile(r"_(\d{8}T\d{6}Z)$")
_HOSTED_LINE = re.compile(r"^- `([^`]+)`: (\S+)\s*$", re.M)
_HOSTED_SECTION = re.compile(r"^## Hosted copies\s*$", re.M)
_TITLE = re.compile(r"^# (.+)$", re.M)


@dataclass
class Dossier:
    name: str
    path: Path
    retailer: str
    profile: str
    at: datetime | None
    files: list[str] = field(default_factory=list)
    report_md: str = ""
    report_html: str = ""
    title: str = ""
    hosted: list[tuple[str, str]] = field(default_factory=list)

    @property
    def has_report(self) -> bool:
        return bool(self.report_md)

    @property
    def at_text(self) -> str:
        return self.at.strftime("%Y-%m-%d %H:%M:%SZ") if self.at else ""


def parse_name(name: str) -> tuple[str, str, datetime | None]:
    """"costco_profile-bravo_20260830T070304Z" -> ("costco", "profile-bravo", 2026-08-30T07:03:04Z).
    Anything that does not fit is still listed, with what could be read."""
    match = _STAMP.search(name)
    at = None
    stem = name
    if match:
        stem = name[: match.start()]
        try:
            at = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            at = None
    retailer, _, profile = stem.partition("_")
    return retailer, profile, at


def hosted_copies(report_md: str) -> list[tuple[str, str]]:
    """(file name, link) pairs from the report's "Hosted copies" section, verbatim."""
    match = _HOSTED_SECTION.search(report_md)
    if not match:
        return []
    return [(name, link) for name, link in _HOSTED_LINE.findall(report_md[match.end():])]


def render_markdown(text: str) -> str:
    """report.md -> HTML. Raw HTML in the source is escaped, not passed through: a dossier is a
    redacted page capture and may still carry anything the page did."""
    from markdown_it import MarkdownIt

    return MarkdownIt("commonmark", {"html": False}).enable("table").render(text)


def load_dossier(directory: Path) -> Dossier:
    retailer, profile, at = parse_name(directory.name)
    if at is None:
        at = datetime.fromtimestamp(directory.stat().st_mtime, timezone.utc)
    files = sorted(p.name for p in directory.iterdir() if p.is_file())
    report = directory / "report.md"
    text = ""
    if report.is_file():
        try:
            text = report.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    title_match = _TITLE.search(text) if text else None
    return Dossier(
        name=directory.name, path=directory, retailer=retailer, profile=profile, at=at,
        files=files, report_md=text, report_html=render_markdown(text) if text else "",
        title=title_match.group(1).strip() if title_match else directory.name,
        hosted=hosted_copies(text) if text else [],
    )


def list_dossiers(root: Path = FAILURES_DIR) -> list[Dossier]:
    """Every dossier directory, newest first."""
    root = Path(root)
    if not root.is_dir():
        return []
    dossiers = [load_dossier(p) for p in root.iterdir() if p.is_dir()]
    dossiers.sort(key=lambda d: (d.at or datetime.min.replace(tzinfo=timezone.utc), d.name),
                  reverse=True)
    return dossiers
