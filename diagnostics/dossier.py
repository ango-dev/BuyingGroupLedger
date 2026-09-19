"""The failure dossier: everything needed to fix a broken deterministic path, written to disk.

One dossier per scrape attempt, opened by the scraper's `scrape()` through `collecting(...)` and
held in a ContextVar so code deep inside the API clients can add to it without threading a
parameter through every call. Nothing is written unless something goes wrong: `write()` is called
only by the scraper's failure branches (or when a soft `problem()` was reported), so a clean run
leaves no files behind.

WHAT GOES IN, AND WHY:
- The exception + traceback. The obvious part.
- A TIMELINE of `note()`s — which URL was loaded, how many orders were discovered, which step was
  running. "No orders found (shape changed?)" on its own says nothing about WHERE.
- SNAPSHOTS: the page's URL, title, HTML and a screenshot at the point of failure. Taken by
  `CdpBrowser.__exit__` automatically when a `with CdpBrowser(...)` block exits on an exception, so
  no API client has to remember to do it.
- A SELECTOR AUDIT per snapshot: every selector the retailer's parser depends on (declared as the
  scraper's `diagnostic_selectors`) is run against the captured HTML, and the report lists how many
  matches each got and a sample of the text. A selector at 0 where it used to match is the fix.
- API RESPONSES for the no-browser paths (Costco GraphQL): status + body of the call that failed.

REDACTION. Everything written passes through `redact()`: configured secrets (passwords, TOTP seeds,
tokens, proxy creds, usernames) are replaced verbatim, and common PII patterns (emails, phone
numbers, card "ending in NNNN") are masked. A page can still carry a name or a street address, so
the dossier lives under `logs/` (gitignored) and the report says not to commit it. Order ids and
tracking numbers are deliberately kept — they are what a fix gets tested against.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
FAILURES_DIR = ROOT / "logs" / "failures"
#: How many dossiers to keep. Each holds a full page of HTML and a PNG; a Pi's SD card is finite, and
#: a selector that stays broken produces one per scheduled run until it is fixed.
KEEP_DOSSIERS = 40

_HTML_LIMIT = 3_000_000  # chars of page HTML to keep per snapshot
_BODY_LIMIT = 40_000     # chars of API response body to keep
_SAMPLE_LEN = 160        # chars of matched text shown per selector in the audit

_current: contextvars.ContextVar[FailureDossier | None] = contextvars.ContextVar(
    "failure_dossier", default=None)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}(?!\d)")
_CARD_RE = re.compile(r"(ending in\s*)(\d{4})", re.I)
_MASKED_CARD_RE = re.compile(r"X{4,}[X\s-]*\d{4}")


def redact(text: str, secrets=()) -> str:
    """Strip configured secrets and common PII patterns out of text bound for disk."""
    if not text:
        return text
    for secret in sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = _EMAIL_RE.sub("[email]", text)
    text = _PHONE_RE.sub("[phone]", text)
    text = _CARD_RE.sub(r"\1####", text)
    text = _MASKED_CARD_RE.sub("XXXX####", text)
    return text


def audit_selectors(html: str, selectors: dict[str, str]) -> list[dict]:
    """Run every declared selector against `html`. Returns one row per selector.

    A value starting with `text:` is a substring marker (for pages whose data is embedded in
    scripts rather than elements — Best Buy's Next.js flight chunks); anything else is CSS.
    Never raises: a selector the engine rejects is reported as an error row, because the whole
    point is to keep going and show the rest.
    """
    rows: list[dict] = []
    if not selectors:
        return rows
    soup = None
    for name, selector in selectors.items():
        row = {"name": name, "selector": selector, "count": 0, "sample": ""}
        try:
            if selector.startswith("text:"):
                needle = selector[len("text:"):]
                row["count"] = html.count(needle)
            else:
                if soup is None:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(html or "", "html.parser")
                matches = soup.select(selector)
                row["count"] = len(matches)
                if matches:
                    sample = matches[0].get_text(" ", strip=True) or str(matches[0])[:_SAMPLE_LEN]
                    row["sample"] = re.sub(r"\s+", " ", sample)[:_SAMPLE_LEN]
        except Exception as exc:  # noqa: BLE001 — one bad selector must not hide the others
            row["error"] = f"{type(exc).__name__}: {exc}"
        rows.append(row)
    return rows


class FailureDossier:
    def __init__(self, retailer_key: str, profile_label: str, *,
                 selectors: dict[str, str] | None = None, secrets=(), root: Path | None = None):
        self.retailer_key = retailer_key
        self.profile_label = profile_label
        self.selectors = dict(selectors or {})
        self.secrets: set[str] = {s for s in secrets if isinstance(s, str) and s}
        self.root = Path(root) if root else FAILURES_DIR
        self.started = datetime.now(timezone.utc)
        self.notes: list[tuple[str, str, str]] = []      # (timestamp, step, message)
        self.problems: list[str] = []                    # non-fatal issues worth a dossier
        self.snapshots: list[dict] = []
        self.responses: list[dict] = []
        self._dir: Path | None = None
        self.path: Path | None = None                    # set once write() has run
        self.upload_link: str = ""
        self.hosted: list[tuple[str, str]] = []  # (file name, link) for every uploaded file

    # --- collection -------------------------------------------------------------------------------
    @staticmethod
    def _stamp() -> str:
        return datetime.now(timezone.utc).strftime("%H:%M:%S")

    def note(self, step: str, message: str) -> None:
        self.notes.append((self._stamp(), step, message))

    def problem(self, message: str) -> None:
        """A non-fatal issue the run survived but a human should fix (e.g. one unreadable page)."""
        self.problems.append(message)
        self.note("problem", message)

    def add_secrets(self, *values) -> None:
        self.secrets.update(v for v in values if isinstance(v, str) and v)

    def _dir_path(self) -> Path:
        if self._dir is None:
            stamp = self.started.strftime("%Y%m%dT%H%M%SZ")
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{self.retailer_key}_{self.profile_label}")
            self._dir = self.root / f"{safe}_{stamp}"
            self._dir.mkdir(parents=True, exist_ok=True)
        return self._dir

    def snapshot(self, page, label: str) -> None:
        """Capture URL/title/HTML/screenshot of a live Playwright page. Best-effort, never raises."""
        n = len(self.snapshots) + 1
        snap: dict = {"n": n, "label": label, "at": self._stamp(), "url": "", "title": "",
                      "html_file": "", "png_file": "", "audit": [], "errors": []}
        try:
            snap["url"] = redact(str(getattr(page, "url", "") or ""), self.secrets)
        except Exception as exc:  # noqa: BLE001
            snap["errors"].append(f"url: {exc}")
        try:
            snap["title"] = redact(str(page.title() or ""), self.secrets)
        except Exception as exc:  # noqa: BLE001
            snap["errors"].append(f"title: {exc}")
        html = ""
        try:
            html = page.content() or ""
        except Exception as exc:  # noqa: BLE001
            snap["errors"].append(f"content: {exc}")
        if html:
            try:
                target = self._dir_path() / f"page_{n}.html"
                target.write_text(redact(html, self.secrets)[:_HTML_LIMIT], encoding="utf-8")
                snap["html_file"] = target.name
            except Exception as exc:  # noqa: BLE001
                snap["errors"].append(f"write html: {exc}")
            snap["audit"] = [
                {**row, "sample": redact(row.get("sample", ""), self.secrets)}
                for row in audit_selectors(html, self.selectors)
            ]
        try:
            target = self._dir_path() / f"page_{n}.png"
            page.screenshot(path=str(target), full_page=False, timeout=30000)
            snap["png_file"] = target.name
        except Exception as exc:  # noqa: BLE001
            snap["errors"].append(f"screenshot: {exc}")
        self.snapshots.append(snap)
        self.note("snapshot", f"#{n} {label} — {snap['url'] or '(no url)'}")

    def snapshot_html(self, html: str, label: str, url: str = "") -> None:
        """Like snapshot(), for HTML already in hand — the Amazon parsers run AFTER the browser has
        closed, so a shape failure there has no live page to capture, only the document it parsed."""
        n = len(self.snapshots) + 1
        snap: dict = {"n": n, "label": label, "at": self._stamp(), "url": redact(url, self.secrets),
                      "title": "", "html_file": "", "png_file": "", "audit": [], "errors": []}
        html = html or ""
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        if m:
            snap["title"] = redact(" ".join(m.group(1).split()), self.secrets)
        if html:
            try:
                target = self._dir_path() / f"page_{n}.html"
                target.write_text(redact(html, self.secrets)[:_HTML_LIMIT], encoding="utf-8")
                snap["html_file"] = target.name
            except Exception as exc:  # noqa: BLE001
                snap["errors"].append(f"write html: {exc}")
            snap["audit"] = [
                {**row, "sample": redact(row.get("sample", ""), self.secrets)}
                for row in audit_selectors(html, self.selectors)
            ]
        snap["errors"].append("screenshot: none — captured from HTML after the browser closed")
        self.snapshots.append(snap)
        self.note("snapshot", f"#{n} {label} — {snap['url'] or '(no url)'}")

    def record_response(self, label: str, status, body, request=None) -> None:
        """Keep an API request/response pair (Costco GraphQL, Best Buy ss-api) that came back wrong."""
        n = len(self.responses) + 1
        text = body if isinstance(body, str) else json.dumps(body, indent=2, default=str)
        entry = {"n": n, "label": label, "status": status, "file": "", "at": self._stamp(),
                 "request": redact(json.dumps(request, indent=2, default=str), self.secrets)
                 if request is not None else ""}
        try:
            target = self._dir_path() / f"response_{n}.txt"
            target.write_text(redact(text, self.secrets)[:_BODY_LIMIT], encoding="utf-8")
            entry["file"] = target.name
        except Exception as exc:  # noqa: BLE001
            entry["error"] = str(exc)
        self.responses.append(entry)
        self.note("response", f"#{n} {label} -> status {status}")

    # --- output -----------------------------------------------------------------------------------
    def write(self, exc: BaseException | None = None) -> Path:
        """Write report.md (and prune old dossiers). Returns the dossier directory."""
        directory = self._dir_path()
        try:
            (directory / "report.md").write_text(self.render(exc), encoding="utf-8")
        except Exception:  # noqa: BLE001 — the dossier is an aid; it must never become the failure
            log.exception("Could not write the failure dossier report to %s", directory)
        self.path = directory
        try:
            from diagnostics import activity

            activity.record("dossier", f"{self.retailer_key} [{self.profile_label}]: failure dossier "
                            f"written{' -- ' + type(exc).__name__ if exc else ''}",
                            {"name": directory.name, "path": str(directory),
                             "error": f"{type(exc).__name__}: {exc}" if exc else "",
                             "snapshots": len(self.snapshots)})
        except Exception:  # noqa: BLE001
            log.warning("Could not record the dossier in the activity log", exc_info=True)
        self._prune()
        return directory

    #: Object-key prefix for uploaded dossiers. Put a lifecycle rule on it (30 days) in the bucket.
    def upload(self) -> str:
        """Dossiers are not uploaded anywhere since 2026-09-18 (OCI removed): the dashboard's
        Activity page renders them from logs/failures/, and the alert says so. Kept as a no-op
        so a caller written against the old shape still runs."""
        self.upload_link = ""
        self.hosted = []
        return ""

    def _prune(self) -> None:
        try:
            dirs = sorted((p for p in self.root.iterdir() if p.is_dir()), key=lambda p: p.name)
            for old in (dirs[:-KEEP_DOSSIERS] if len(dirs) > KEEP_DOSSIERS else []):
                for child in old.iterdir():
                    child.unlink()
                old.rmdir()
        except Exception:  # noqa: BLE001
            log.debug("Dossier pruning skipped.", exc_info=True)

    def render(self, exc: BaseException | None = None) -> str:
        def r(value) -> str:
            return redact(str(value), self.secrets)

        parser = self.retailer_key.replace("-", "_")
        out: list[str] = [
            f"# Failure dossier — {self.retailer_key} [{self.profile_label}] — "
            f"{self.started.strftime('%Y-%m-%d %H:%M:%SZ')}",
            "",
            "Written by the deterministic scrape path when it could not complete. The paid agent "
            "fallback did NOT run; nothing was recorded for this retailer this run. Fix the cause "
            "below and the next scheduled run picks the orders up.",
            "",
            "## What failed",
            "",
        ]
        if exc is not None:
            out += [f"**{type(exc).__name__}**: {r(exc)}", "", "```",
                    r("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))).rstrip(),
                    "```"]
        else:
            out.append("No exception — the run completed, but with the problems listed below.")
        if self.problems:
            out += ["", "### Non-fatal problems", ""]
            out += [f"- {r(p)}" for p in self.problems]
        out += ["", "## Timeline", ""]
        if self.notes:
            out += [f"- `{ts}` **{r(step)}** — {r(msg)}" for ts, step, msg in self.notes]
        else:
            out.append("- (no notes were recorded before the failure)")
        out += ["", "## Page snapshots", ""]
        if not self.snapshots:
            out.append("None — this path does not use a browser, or the failure happened before one "
                       "was opened. See the API responses below.")
        for s in self.snapshots:
            out += [f"### Snapshot {s['n']}: {r(s['label'])}", "",
                    f"- URL: `{s['url'] or '(unknown)'}`",
                    f"- Title: {s['title'] or '(unknown)'}"]
            files = [f"`{f}`" for f in (s["html_file"], s["png_file"]) if f]
            out.append(f"- Files: {', '.join(files) if files else '(none captured)'}")
            if s["errors"]:
                out.append(f"- Capture errors: {'; '.join(r(e) for e in s['errors'])}")
            if s["audit"]:
                zero = [a["name"] for a in s["audit"] if not a.get("error") and a["count"] == 0]
                out += ["",
                        f"**Selector audit** — {len(zero)} of {len(s['audit'])} declared selectors "
                        f"matched NOTHING on this page" + (f": {', '.join(zero)}" if zero else "."),
                        "",
                        "| selector name | selector | matches | first match (text) |",
                        "|---|---|---:|---|"]
                for a in s["audit"]:
                    cell = (a.get("error") or a.get("sample", "")).replace("|", "\\|")
                    out.append(f"| {a['name']} | `{a['selector']}` | {a['count']} | {cell} |")
            out.append("")
        out += ["## API responses", ""]
        if not self.responses:
            out.append("None recorded.")
        for e in self.responses:
            out += [f"### Response {e['n']}: {r(e['label'])} — status {e['status']}", "",
                    f"- Body: `{e['file']}`" if e["file"] else f"- Body not saved: {e.get('error')}"]
            if e["request"]:
                out += ["- Request:", "```", e["request"], "```"]
            out.append("")
        out += [
            "## How to use this",
            "",
            "Hand this directory to your coding agent along with the retailer's parser "
            f"(`scrapers/{parser}_mapping.py`, `scrapers/{parser}_api.py`, and the sign-in module if "
            "the failure was on login). The selector audit says which declared selectors no longer "
            "match; `page_N.html` is the real DOM to write the replacement against and `page_N.png` "
            "shows what it looked like. Add the captured HTML as a test fixture so the fix is proven "
            "offline before the next scheduled run.",
            "",
            "This bundle is REDACTED for configured secrets and common PII patterns but can still "
            "contain names and addresses. It lives under `logs/` (gitignored). Do not commit it.",
            "",
        ]
        return "\n".join(out)


# --- module-level API (no-ops when no dossier is open) ----------------------------------------------
def current() -> FailureDossier | None:
    return _current.get()


@contextmanager
def collecting(retailer_key: str, profile_label: str, *, selectors: dict[str, str] | None = None,
               secrets=(), root: Path | None = None):
    """Open a dossier for the duration of a scrape. A nested open reuses the outer dossier."""
    existing = _current.get()
    if existing is not None:
        yield existing
        return
    dossier = FailureDossier(retailer_key, profile_label, selectors=selectors, secrets=secrets,
                             root=root)
    token = _current.set(dossier)
    try:
        yield dossier
    finally:
        _current.reset(token)


def note(step: str, message: str) -> None:
    d = _current.get()
    if d is not None:
        d.note(step, message)


def problem(message: str) -> None:
    d = _current.get()
    if d is not None:
        d.problem(message)


def problem_count() -> int:
    """How many problems the open dossier holds (0 with none open) -- a client measures it before
    a parse and attaches its page when the count grew, whoever reported the problem."""
    d = _current.get()
    return len(d.problems) if d is not None else 0


def problems_since(count: int) -> list[str]:
    d = _current.get()
    return list(d.problems[count:]) if d is not None else []


def report_unreadable_rows(rows, sources: dict[str, str] | None = None, evidence=None) -> dict[str, list[str]]:
    """THE CAPTURE GATE (2026-09-19): every mandatory cell the built rows could not read becomes a
    problem, so the run ends with a dossier + alert even though the rows are recorded (blank never
    overwrites; the next re-read may fill them). Grouped by order id; `sources` names the selector
    or JSON path each field is read from (the mapping's FIELD_SOURCES) so the report says WHAT
    stopped matching; `evidence(order_id, messages)` is called once per order with a gap so the
    caller can attach that order's page or payload. Returns {order_id: [messages]}. Outside a
    dossier it reports nothing (the mappings stay pure and offline-testable)."""
    from models.order import CAPTURE_FIELD_LABELS, unreadable_fields

    found: dict[str, list[str]] = {}
    for row in rows or []:
        for field in unreadable_fields(row):
            source = (sources or {}).get(field)
            where = f" (read from {source})" if source else ""
            found.setdefault(row.order_id, []).append(
                f"order {row.order_id} / shipment {row.shipment}: {CAPTURE_FIELD_LABELS[field]} could "
                f"not be read for '{row.item_name}'{where} -- recorded blank. A cell that used to "
                f"read is a shape change: fix the reader against the attached page/payload.")
    if _current.get() is None:
        return found
    for order_id, messages in found.items():
        for message in messages:
            problem(message)
        if evidence is not None:
            try:
                evidence(order_id, messages)
            except Exception:  # noqa: BLE001 -- never let diagnostics mask the scrape
                log.debug("Dossier evidence callback failed.", exc_info=True)
    return found


def add_secrets(*values) -> None:
    d = _current.get()
    if d is not None:
        d.add_secrets(*values)


def snapshot(page, label: str) -> None:
    d = _current.get()
    if d is not None and page is not None:
        try:
            d.snapshot(page, label)
        except Exception:  # noqa: BLE001 — never let diagnostics mask the real failure
            log.debug("Dossier snapshot failed.", exc_info=True)


def snapshot_html(html: str, label: str, url: str = "") -> None:
    d = _current.get()
    if d is not None:
        try:
            d.snapshot_html(html, label, url)
        except Exception:  # noqa: BLE001
            log.debug("Dossier html snapshot failed.", exc_info=True)


def record_response(label: str, status, body, request=None) -> None:
    d = _current.get()
    if d is not None:
        try:
            d.record_response(label, status, body, request)
        except Exception:  # noqa: BLE001
            log.debug("Dossier response record failed.", exc_info=True)
