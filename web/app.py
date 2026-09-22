"""The FastAPI application: five GET routes over one read-only reader, plus the Backup page.

The ledger routes are GET-only. The two POSTs on /backup write LOCAL files only (a zip under
backups/, or a restore into a fresh clone); nothing here can write the ledger or call a third party.

`create_app()` is a factory so tests can hand it a SnapshotReader over a temporary CSV, a temporary
logs/ and failures/ directory, and a fixed clock. The module-level `app` is what `uvicorn web.app:app`
or `python -m web` serves, built from config.json's `web` section.
"""

from __future__ import annotations

import dataclasses

import hmac
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from typing import Callable
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from diagnostics import activity as activity_module
from scripts import backup as backup_module

from web import auth as auth_module
from web import failures as failures_module
from web import importer
from web import setup_wizard
from web.ledger_writer import RunInProgress
from web import heartbeat as heartbeat_module
from web.ledger_reader import FIELD_TO_HEADER, LedgerReader, Snapshot, reader_from_settings
from web.audit_view import AuditCache, audit_grids, audit_key, key_of, run_audit
from web.queries import (CHOICE_FIELDS, Filters, _values as query_values, cell_choices, column_headings, facets, filter_rows,
                         order_view, sort_rows)
from web.queries import DEFAULT_SORT, sort_by_finding  # the Finding column's sort
from models import retailers as retailers_module
from web.recon_view import findings_for as recon_findings, reconcile
from web import tax_inputs
from web.summary import overview

# The except paths below (the nav's audit, the activity log) already spoke to `log`; it was
# never bound, so a caught error raised NameError instead of being logged. Bound 2026-09-19.
log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = HERE / "templates"
STATIC_DIR = HERE / "static"

#: The routes, for the read-only test that asserts every one of them refuses any non-GET method.
ROUTES = ("/", "/orders", "/orders/{order_id}", "/audit", "/recon", "/taxes", "/failures", "/health")


# --------------------------------------------------------------------------------------------------
# Template filters
# --------------------------------------------------------------------------------------------------


def money(value) -> str:
    """$1,234.56; negatives as -$12.00; blank for None / non-numbers. The ledger's own formatting,
    so a number reads the same on the page as in the ledger."""
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    sign = "-" if number < 0 else ""
    return f"{sign}${abs(number):,.2f}"


def percent(value) -> str:
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    text = f"{number * 100:.2f}".rstrip("0").rstrip(".")
    return f"{text}%"


def cell(row, name: str) -> str:
    """A row's cell for the table: the display text, except the two formula columns, which a CSV
    backup stores as formula text and the page shows as the computed number."""
    if name == "cogs":
        return money(row.cogs)
    if name == "total_profit":
        return money(row.profit)
    if name in ("total_cost", "insurance", "payout_amount", "expected_payout", "cost_per_item",
                "shipping", "sales_tax", "gift_card", "rewards_used"):
        return money(row.number(name))
    if name in ("cashback_rate", "promo_rate"):
        return percent(row.number(name))
    if name == "tracking_submitted":
        return "✓" if row.tracking_submitted else ""
    return row.text(name)


# --------------------------------------------------------------------------------------------------
# App factory
# --------------------------------------------------------------------------------------------------


def _exit_soon(delay: float = 0.5) -> None:
    """Leave the process shortly after the current response is sent. In the container,
    docker/entrypoint.sh's loop starts the dashboard again within seconds, with config.json
    re-read; on a desktop the operator runs `python -m web` again."""
    import threading

    threading.Timer(delay, lambda: os._exit(0)).start()


def _signal_container(delay: float = 1.0) -> None:
    """Restart the whole container from inside it. PID 1 is supercronic (docker/entrypoint.sh execs it); a SIGTERM
    ends it, and the compose service's `restart: unless-stopped` starts the container again --
    the entrypoint re-reads config.json, so a container-scope setting takes effect. Delayed so the
    response that says so is sent first. The route refuses while a run lock is live: a restart
    mid-run would abort that run."""
    import signal
    import threading

    threading.Timer(delay, lambda: os.kill(1, signal.SIGTERM)).start()


def _in_container() -> bool:
    return Path("/.dockerenv").exists()


def create_app(reader: LedgerReader | None = None, *, settings=None,
               logs_dir: Path | None = None, failures_dir: Path | None = None,
               backup_dir: Path | None = None, repo_root_dir: Path | None = None,
               clock: Callable[[], datetime] | None = None,
               restarter: Callable[[], None] | None = None,
               container_restarter: Callable[[], None] | None = None,
               in_container: bool | None = None,
               writer=None, session_secret: bytes | str | None = None) -> FastAPI:
    """`writer` is the ONE ledger-write path (web/ledger_writer.LedgerCellWriter): cell edits on
    the Orders page. None = the page is view-only (the snapshot backend, or a test).
    `container_restarter` / `in_container` are injection points for the container-restart
    button (the defaults signal PID 1, and detect Docker by /.dockerenv). `session_secret` signs
    the sign-in cookies (default: the one kept in .state.json, made on first use)."""
    restart = restarter or _exit_soon
    restart_container = container_restarter or _signal_container
    in_container = _in_container() if in_container is None else bool(in_container)
    if settings is None:
        from config.settings import settings as live_settings

        settings = live_settings
    reader = reader or reader_from_settings(settings)
    logs_dir = Path(logs_dir) if logs_dir else heartbeat_module.LOGS_DIR
    failures_dir = Path(failures_dir) if failures_dir else failures_module.FAILURES_DIR
    clock = clock or (lambda: datetime.now(timezone.utc))
    interval_hours = float(getattr(settings, "container_run_interval_hours", 6) or 6)

    app = FastAPI(title="Buying Group Ledger", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.reader = reader
    app.state.read_only = True
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/receipts/{path:path}")
    def receipt_file(path: str):
        """A stored receipt (receipts/store.py): the Receipt Link column points here. The store
        refuses a path that climbs out of its directory."""
        from web import receipts_upload

        target = receipts_upload.receipt_file_path(path)
        if target is None or not target.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(str(target), filename=target.name)

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    # No auto-reload: with it on, EVERY `{% include %}` stats the template file, and the Orders
    # sheet includes _cell.html once per cell -- ~10,000 stat calls per page render, most of the
    # page's server time on the Pi's SD card (profiled 2026-09-21). Templates change only with
    # the code, and the dashboard restarts with the code (the Settings page's Restart button,
    # `python -m web` in dev).
    templates.env.auto_reload = False
    # Cache-busting for the static assets: the newest mtime under static/ goes on every asset URL
    # (?v=...), so a deploy is never served an older stylesheet from the browser's cache (seen
    # live: a page rendered with the previous CSS after a pull).
    try:
        asset_version = str(int(max(p.stat().st_mtime for p in STATIC_DIR.iterdir() if p.is_file())))
    except (OSError, ValueError):
        asset_version = "0"
    templates.env.globals["asset_version"] = asset_version
    templates.env.filters["money"] = money
    templates.env.filters["percent"] = percent
    templates.env.filters["cell"] = cell
    from web.queries import query_string

    templates.env.filters["query"] = query_string
    templates.env.globals["header_of"] = FIELD_TO_HEADER.get

    def heartbeat() -> dict:
        return heartbeat_module.read_heartbeat(logs_dir, now=clock(), interval_hours=interval_hours,
                                              stale_hours=settings.web_heartbeat_stale_hours or None)

    # The activity log this dashboard reads and appends to (diagnostics/activity.py): the same
    # file the scheduled run writes, since both share logs/.
    activity_path = Path(logs_dir) / activity_module.ACTIVITY_FILE.name

    def act(kind: str, summary: str, details: dict | None = None) -> None:
        activity_module.record(kind, summary, details, path=activity_path)

    def load(request: Request) -> Snapshot:
        force = str(request.query_params.get("refresh", "")).lower() in ("1", "true", "yes")
        try:
            return reader.load(force=force)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    def page(request: Request, name: str, **context):
        snapshot = context.get("snapshot")
        base = {
            "request": request,
            "backend": reader.backend,
            "source": snapshot.source if snapshot else "",
            "loaded_at": snapshot.loaded_at if snapshot else None,
            "schema_matches": snapshot.schema_matches if snapshot else True,
            "heartbeat": context.pop("heartbeat", None) or heartbeat(),
        }
        return templates.TemplateResponse(request=request, name=name, context={**base, **context})

    # ---- the first-time setup (web/setup_wizard.py) -------------------------------------------------
    # A fresh install (no profiles, no record of a finished setup) lands on /setup from anywhere
    # but the wizard's own pages, the static assets, /health, sign-in, the restore and Settings
    # (the expert's way in). Decided ONCE per process: a configured install is never re-checked.
    SETUP_OPEN = ("/setup", "/static/", "/health", "/login", "/logout", "/backup/restore", "/settings")
    setup_state = {"needed": None}

    def setup_needed() -> bool:
        if not setup_wizard.GATE_ENABLED:
            return False
        if setup_state["needed"] is None or setup_state["needed"] is True:
            try:
                setup_state["needed"] = setup_wizard.needs_setup(clock)
            except Exception:  # noqa: BLE001 -- a malformed state file must not take the site down
                log.exception("could not read the setup record")
                setup_state["needed"] = False
        return setup_state["needed"]

    @app.middleware("http")
    async def require_setup(request: Request, call_next):
        path = request.url.path
        if path.startswith(SETUP_OPEN) or not setup_needed():
            return await call_next(request)
        if request.headers.get("HX-Request"):
            return Response(status_code=401, headers={"HX-Redirect": "/setup"})
        return RedirectResponse(url="/setup", status_code=303)

    # ---- the sign-in (web/auth.py): a password, a rate limit, remember me ------------------------
    # `web.password` set = every page but the login page, the static assets and /health (the
    # container's healthcheck probes it; it holds no ledger data) wants the session cookie.
    # Blank = the dashboard as it was, open.
    def _setting(name: str, default):
        value = getattr(settings, name, None)
        return default if value in (None, "") else value

    password = str(_setting("web_password", "")).strip()
    auth_on = bool(password)
    templates.env.globals["auth_enabled"] = auth_on
    sessions = limiter = None
    if auth_on:
        secret = session_secret or auth_module.session_secret()
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        epoch = lambda: clock().timestamp()  # noqa: E731 -- the injected clock, as seconds
        sessions = auth_module.Sessions(secret, password,
                                        session_hours=float(_setting("web_session_hours", 6)),
                                        remember_days=float(_setting("web_remember_days", 730)),
                                        clock=epoch)
        limiter = auth_module.LoginLimiter(int(_setting("web_login_attempts", 5)),
                                           float(_setting("web_login_lockout_minutes", 15)), clock=epoch)
    OPEN_PREFIXES = ("/login", "/logout", "/static/", "/health")

    def signed_in(request: Request) -> str:
        """"session" / "remembered" / "" -- or "open" when there is no password."""
        if not auth_on:
            return "open"
        return sessions.verify(request.cookies.get(auth_module.COOKIE))

    def client_address(request: Request) -> str:
        # The socket's peer, never a forwarded header: anyone can write X-Forwarded-For, and the
        # rate limit is keyed on this.
        return request.client.host if request.client else "unknown"

    def _minutes(seconds: float) -> str:
        return auth_module.describe(max(60, math.ceil(seconds / 60) * 60))

    @app.middleware("http")
    async def require_sign_in(request: Request, call_next):
        path = request.url.path
        if not auth_on or path.startswith(OPEN_PREFIXES) or signed_in(request):
            return await call_next(request)
        if request.headers.get("HX-Request"):
            # A fragment request: send the whole window to the login page, back to the page it
            # was on (htmx names it), not to the fragment's own address.
            current = request.headers.get("HX-Current-URL", "")
            from urllib.parse import urlsplit

            parts = urlsplit(current) if current else None
            back = (parts.path + (f"?{parts.query}" if parts.query else "")) if parts and parts.path else path
            return Response(status_code=401,
                            headers={"HX-Redirect": f"/login?next={quote(auth_module.safe_next(back), safe='')}"})
        back = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(url=f"/login?next={quote(back, safe='')}", status_code=303)

    def login_page(request: Request, *, nxt: str = "/", error: str = "", locked: bool = False,
                   retry: float = 0, remember: bool = False, status: int = 200):
        response = page(request, "login.html", next=nxt, error=error, locked=locked, remember=remember,
                        retry_label=_minutes(retry) if retry else "",
                        remember_label=auth_module.describe(sessions.lifetime(True)),
                        session_label=auth_module.describe(sessions.lifetime(False)))
        response.status_code = status
        if retry:
            response.headers["Retry-After"] = str(int(math.ceil(retry)))
        return response

    @app.get("/login", response_class=HTMLResponse)
    def login_get(request: Request):
        nxt = auth_module.safe_next(request.query_params.get("next"))
        if signed_in(request):
            return RedirectResponse(url=nxt, status_code=303)
        wait = limiter.retry_after(client_address(request))
        if wait:
            return login_page(request, nxt=nxt, locked=True, retry=wait, status=429,
                              error=f"Too many wrong passwords from this address. Try again in {_minutes(wait)}.")
        return login_page(request, nxt=nxt)

    @app.post("/login", response_class=HTMLResponse)
    async def login_post(request: Request):
        if not auth_on:
            return RedirectResponse(url="/", status_code=303)
        form = await request.form()
        nxt = auth_module.safe_next(str(form.get("next", "") or ""))
        remember = str(form.get("remember", "") or "").strip().lower() in ("on", "1", "true", "yes")
        address = client_address(request)
        wait = limiter.retry_after(address)
        if wait:
            return login_page(request, nxt=nxt, remember=remember, locked=True, retry=wait, status=429,
                              error=f"Too many wrong passwords from this address. Try again in {_minutes(wait)}.")
        typed = str(form.get("password", "") or "")
        if hmac.compare_digest(typed.encode("utf-8"), password.encode("utf-8")):
            limiter.succeeded(address)
            token, lifetime = sessions.issue(remember)
            response = RedirectResponse(url=nxt, status_code=303)
            response.set_cookie(auth_module.COOKIE, token, max_age=lifetime, httponly=True,
                                samesite="lax", path="/")
            act("signin", f"Dashboard sign-in from {address}"
                + (f", remembered for {auth_module.describe(lifetime)}" if remember else ""),
                {"address": address, "remember": remember})
            return response
        left = limiter.failed(address)
        lock = limiter.lockout_seconds
        if left == 0 and lock > 0:
            log.warning("dashboard sign-in locked for %s after %d wrong passwords", address, limiter.max_attempts)
            act("alert", f"Dashboard sign-in locked for {address}: {limiter.max_attempts} wrong passwords "
                         f"in a row (locked for {_minutes(lock)})",
                {"address": address, "attempts": limiter.max_attempts, "lockout_minutes": lock / 60})
            return login_page(request, nxt=nxt, remember=remember, locked=True, retry=lock, status=429,
                              error=f"Too many wrong passwords. This address is locked for {_minutes(lock)}.")
        log.warning("dashboard sign-in: wrong password from %s", address)
        if lock > 0:
            error = (f"Wrong password. {left} attempt{'' if left == 1 else 's'} left before this address "
                     f"is locked for {_minutes(lock)}.")
        else:
            error = "Wrong password."
        return login_page(request, nxt=nxt, remember=remember, status=401, error=error)

    @app.post("/logout")
    def logout(request: Request):
        response = RedirectResponse(url="/login" if auth_on else "/", status_code=303)
        response.delete_cookie(auth_module.COOKIE, path="/")
        return response

    LOUD_KINDS = ("alert", "dossier", "cap")  # cap: a card's spend limit close or reached (2026-09-20)
    LOUD_DAYS = 7
    _loud_cache: dict = {}

    def loud_summary() -> dict:
        """The alerts and failure dossiers from the last seven days of the activity log that nobody
        has ACKNOWLEDGED, per kind: {"alert": {"count", "newest", "events": [{at, summary}]},
        "dossier": {...}}. An acknowledgement is itself an activity event (kind `ack`): with
        details {kind, through} every event of that kind up to `through` is dealt with (the
        overview's "acknowledge all"); with details {kind, at, summary} that one event is (the
        Activity page's per-row button, user 2026-09-19). A newer one shows again. Cached on the log file's size
        and mtime: every page's nav reads it."""
        from datetime import timedelta

        empty = {kind: {"count": 0, "newest": "", "events": []} for kind in LOUD_KINDS}
        try:
            stat = activity_path.stat()
        except OSError:
            return empty
        stamp = (stat.st_mtime_ns, stat.st_size, clock().strftime("%Y-%m-%dT%H"))
        if _loud_cache.get("stamp") == stamp:
            return _loud_cache["value"]
        try:
            events = activity_module.read(activity_path)  # newest first
        except Exception:  # noqa: BLE001 -- an unreadable log must not take the nav down
            log.exception("could not read the activity log")
            return empty
        since = (clock() - timedelta(days=LOUD_DAYS)).isoformat(timespec="seconds")
        through = {kind: "" for kind in LOUD_KINDS}
        singly: dict[str, set] = {kind: set() for kind in LOUD_KINDS}
        for e in events:
            if e.get("kind") == "ack":
                details = e.get("details") or {}
                kind = str(details.get("kind", ""))
                if kind not in through:
                    continue
                if details.get("at"):
                    singly[kind].add((str(details.get("at", "")), str(details.get("summary", ""))))
                else:
                    through[kind] = max(through[kind], str(details.get("through", "")))
        out = {}
        for kind in LOUD_KINDS:
            fresh = [{"at": str(e.get("at", "")), "summary": str(e.get("summary", ""))} for e in events
                     if e.get("kind") == kind and since <= str(e.get("at", "")) and str(e.get("at", "")) > through[kind]
                     and (str(e.get("at", "")), str(e.get("summary", ""))) not in singly[kind]]
            out[kind] = {"count": len(fresh), "newest": max(f["at"] for f in fresh) if fresh else "", "events": fresh}
        _loud_cache["stamp"], _loud_cache["value"] = stamp, out
        return out

    def nav_badges() -> dict:
        """The counts on the nav's Activity, Audit and Recon links, on every page: the unacknowledged alerts plus failure dossiers of the last seven days, the
        rows a FAILING audit check flags, the orders paid short of or over their commitment. Zero
        is no badge. No ledger yet, or a check that blows up, leaves the badge off rather than
        the page down."""
        loud = loud_summary()
        out = {"activity": sum(loud[kind]["count"] for kind in LOUD_KINDS), "audit": 0, "recon": 0,
               "import": importer.staged_count(data_dir)}  # the staging sheet's rows still to import
        try:
            snapshot = reader.load()
        except Exception:  # noqa: BLE001
            return out
        try:
            report = audit_report(snapshot)
            out["audit"] = sum(1 for findings in report.by_key.values()
                               if any(f.status == "FAIL" for f in findings))
        except Exception:  # noqa: BLE001
            log.exception("could not audit the ledger for the nav")
        try:
            recon = recon_summary(snapshot)
            out["recon"] = len(recon.short) + len(recon.over)
        except Exception:  # noqa: BLE001
            log.exception("could not reconcile the ledger for the nav")
        return out

    def needs_attention(snapshot) -> list[dict]:
        """What the overview should shout about, or nothing: the unacknowledged alerts and dossiers from
        the last seven days of the activity log (each with an acknowledge button), the audit's
        failing and warning checks, and the reconciliation's short- and over-paid orders. Each
        card links where it is dealt with."""
        cards: list[dict] = []
        loud = loud_summary()
        if loud["alert"]["count"]:
            cards.append({"tone": "bad", "label": "Alerts", "count": loud["alert"]["count"],
                          "text": "unacknowledged, last 7 days", "href": "/activity?type=alert&days=7&unacked=1",
                          "ack": {"kind": "alert", "through": loud["alert"]["newest"]}})
        if loud["dossier"]["count"]:
            cards.append({"tone": "bad", "label": "Failure dossiers", "count": loud["dossier"]["count"],
                          "text": "runs that recorded nothing", "href": "/activity?type=dossier&days=7&unacked=1",
                          "ack": {"kind": "dossier", "through": loud["dossier"]["newest"]}})
        if loud["cap"]["count"]:  # a card's spend limit close or reached
            reached = any("limit reached" in e["summary"] for e in loud["cap"]["events"])
            cards.append({"tone": "bad" if reached else "warn", "label": "Spend limits", "count": loud["cap"]["count"],
                          "text": "card limit(s) reached" if reached else "card limit(s) getting close",
                          "href": "/activity?type=cap&days=7&unacked=1",
                          "ack": {"kind": "cap", "through": loud["cap"]["newest"]}})
        try:
            report = audit_report(snapshot)
            counts = report.counts()

            def audit_href(status: str) -> str:  # the rows those checks flag
                names = [c["name"] for c in report.checks if c["status"] == status and c["rows"]]
                return "/audit?" + urlencode([("check", n) for n in names]) if names else "/audit"

            if counts.get("FAIL"):
                cards.append({"tone": "bad", "label": "Audit failures", "count": counts["FAIL"],
                              "text": f"check(s) failing, {len(report.by_key)} row(s) flagged", "href": audit_href("FAIL")})
            if counts.get("WARN"):
                cards.append({"tone": "warn", "label": "Audit warnings", "count": counts["WARN"],
                              "text": "check(s) worth a look", "href": audit_href("WARN")})
        except Exception:  # noqa: BLE001
            log.exception("could not audit the ledger for the overview")
        try:
            recon = recon_summary(snapshot)
            if recon.short:
                cards.append({"tone": "bad", "label": "Short-paid", "count": len(recon.short),
                              "text": f"order(s), {money(recon.short_total)} under the commitment", "href": "/recon?kind=short"})
            if recon.over:
                cards.append({"tone": "warn", "label": "Over-paid", "count": len(recon.over),
                              "text": f"order(s), {money(recon.over_total)} over the commitment", "href": "/recon?kind=over"})
        except Exception:  # noqa: BLE001
            log.exception("could not reconcile the ledger for the overview")
        return cards

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        snapshot = load(request)
        month = str(request.query_params.get("month") or "")
        return page(request, "overview.html", snapshot=snapshot, body_class="overview",
                    summary=overview(snapshot, month=month, today=clock().date(),
                                     inputs_by_year=tax_inputs.load_all(tax_inputs_path)),
                    attention=needs_attention(snapshot))

    # The ledger writer: cell edits on the Orders page. The snapshot backend is a CSV, so there is
    # nothing to write to and the page stays view-only there.
    if writer is None and reader.backend != "snapshot":
        from web.ledger_writer import LedgerCellWriter

        writer = LedgerCellWriter(logs_dir=logs_dir)
    app.state.writer = writer
    import json

    from web.ledger_writer import EDITABLE_FIELDS, EditError
    from web.queries import LINK_FIELDS, MONEY_FIELDS

    templates.env.globals["EDITABLE_FIELDS"] = EDITABLE_FIELDS
    from web.ledger_writer import DATE_FIELDS
    from web.queries import CHOICE_FIELDS

    templates.env.globals["DATE_FIELDS"] = DATE_FIELDS
    templates.env.globals["STAGING_FIELDS"] = importer.STAGING_FIELDS
    templates.env.globals["CHOICE_FIELDS"] = CHOICE_FIELDS
    templates.env.globals["LINK_FIELDS"] = LINK_FIELDS
    templates.env.globals["MONEY_FIELDS"] = MONEY_FIELDS

    from web.queries import (CARD_COLUMNS, ORDER_COLUMNS, PER_PAGE_CHOICES, SEARCH_FIELDS, SORT_CHOICES,
                             order_cards, paginate)

    templates.env.globals["PER_PAGE_CHOICES"] = PER_PAGE_CHOICES
    templates.env.globals["ORDER_COLUMNS"] = ORDER_COLUMNS
    templates.env.globals["CARD_COLUMNS"] = CARD_COLUMNS
    templates.env.globals["SORT_CHOICES"] = SORT_CHOICES
    templates.env.globals["SEARCH_FIELDS"] = SEARCH_FIELDS  # the search box names every column it looks in

    from starlette.datastructures import QueryParams

    from web.queries import PER_PAGE_CHOICES as _PER, VIEWS as _VIEWS

    VIEW_COOKIE, PER_COOKIE, FILTERS_COOKIE = "ledger-view", "ledger-per", "ledger-filters"

    def cookie(base: str, scope: str = "") -> str:
        """Orders, Audit and Recon each remember their OWN view, page size and filters: the scoped pages
        keep theirs under a suffixed cookie name."""
        return f"{base}-{scope}" if scope else base
    COOKIE_MAX_AGE = 365 * 24 * 3600
    #: Never remembered: a page number is where you were, not how you look at the ledger.
    TRANSIENT = {"page", "notice", "error", "remember", "refresh"}

    def remembered(request: Request, params, replay: bool = True, scope: str = "") -> QueryParams:
        """The request's params with the remembered view / page size filled in when the request
        does not say. A BARE
        /orders -- no query at all, the nav link -- comes back with the whole remembered filter
        and sort state."""
        items = list(params.multi_items()) if hasattr(params, "multi_items") else list(params.items())
        if replay and not [k for k, _v in items if k not in TRANSIENT]:
            from urllib.parse import parse_qsl

            items = [(k, v) for k, v in items if k in TRANSIENT] + \
                parse_qsl(request.cookies.get(cookie(FILTERS_COOKIE, scope), ""), keep_blank_values=False)
        names = {k for k, _v in items}
        view = request.cookies.get(cookie(VIEW_COOKIE, scope), "")
        if "view" not in names and view in _VIEWS:
            items.append(("view", view))
        per = request.cookies.get(cookie(PER_COOKIE, scope), "")
        if "per" not in names and per.isdigit() and int(per) in _PER:
            items.append(("per", per))
        return QueryParams(items)

    def remember(request: Request, response, filters: Filters, scope: str = ""):
        """Persist the view / page size and the whole filter / sort state -- but ONLY from the
        filter form's own requests (they carry `remember=1`). A link that names a filter in its
        URL, an overview tile or a bookmark, is applied for that visit and never rewrites the
        memory."""
        if request.query_params.get("remember") != "1":
            return response
        if "view" in request.query_params:
            response.set_cookie(cookie(VIEW_COOKIE, scope), filters.view, max_age=COOKIE_MAX_AGE, samesite="lax")
        if "per" in request.query_params:
            response.set_cookie(cookie(PER_COOKIE, scope), str(filters.per), max_age=COOKIE_MAX_AGE, samesite="lax")
        explicit = [(k, v) for k, v in request.query_params.multi_items() if k not in TRANSIENT]
        if explicit:  # each page's own memory (Orders, Audit and Recon are separate)
            from urllib.parse import urlencode

            response.set_cookie(cookie(FILTERS_COOKIE, scope), urlencode(explicit, doseq=True),
                                max_age=COOKIE_MAX_AGE, samesite="lax")
        return response

    # The Audit and Reconciliation pages ARE the Orders view (table or cards, the same filters,
    # sort, search, editing) over a subset of rows with a finding beside each. `scope` names the
    # subset; the filter form carries it so an htmx swap or a bulk edit re-renders the same page.
    SCOPES = {"": "/orders", "audit": "/audit", "recon": "/recon"}
    tax_inputs_path = (Path(repo_root_dir) if repo_root_dir else tax_inputs.Path(__file__).resolve().parents[1]) \
        / "data" / tax_inputs.FILE_NAME

    def ledger_years(snapshot) -> dict[int, int]:
        """{year: rows} for every year the ledger touches, by Order Date or Payout Date."""
        out: dict[int, int] = {}
        for row in snapshot.rows:
            seen = set()
            for name in ("order_date", "payout_date"):
                text = row.text(name)[:4]
                if len(text) == 4 and text.isdigit():
                    seen.add(int(text))
            for y in seen:
                out[y] = out.get(y, 0) + 1
        return out

    def tax_years(snapshot) -> list[int]:
        """Every year the ledger touches (by Order Date or Payout Date), every year with saved
        inputs, and this one, newest first -- a new year appears on its own on January 1."""
        years = {clock().year} | set(tax_inputs.load_all(tax_inputs_path)) | set(ledger_years(snapshot))
        return sorted(years, reverse=True)

    def close_year_refusal(snapshot, year: int) -> str:
        """Why a year cannot close: the current year, or a year with ledger rows.
        Empty when it may."""
        if int(year) == clock().year:
            return f"{year} is the current year"
        rows = ledger_years(snapshot).get(int(year), 0)
        if rows:
            return f"{rows} ledger row(s) were placed or paid in {year}"
        return ""

    def tax_report_for(snapshot, year: int) -> dict:
        from scripts.audit_ledger import Sheet
        from scripts.tax_report import build_report

        return build_report(Sheet(audit_grids(reader, snapshot)), year)
    audit_cache = AuditCache()
    app.state.audit_cache = audit_cache

    def audit_report(snapshot):
        def build():
            # The app's clock, so the staleness check is deterministic under test and honest on
            # a host whose wall clock the dashboard already trusts everywhere else.
            return run_audit(audit_grids(reader, snapshot), now=clock())

        return audit_cache.get(audit_key(reader, snapshot), build)

    # The reconciliation, cached exactly as the audit is: it ran over every row on EVERY page
    # render for the nav badge (2026-09-21). The key is audit_key's -- cheap now that the digest
    # is memoized on the snapshot -- so one ledger change rebuilds both once.
    _recon_cache: dict = {}

    def recon_summary(snapshot):
        key = audit_key(reader, snapshot)
        if _recon_cache.get("key") != key:
            _recon_cache["key"], _recon_cache["value"] = key, reconcile(snapshot.rows)
        return _recon_cache["value"]

    def row_key(row) -> tuple:
        return key_of(row.order_id, row.order_date, row.item_name, row.shipment)

    templates.env.globals["row_key"] = row_key

    def settings_cards() -> list:
        """The settings' cards, for the Card Name <-> Card Last 4 pairing; none when unreadable."""
        try:
            from config.cards import load_cards

            return list(load_cards())
        except Exception:  # noqa: BLE001 -- a broken cards section must not break the Orders page
            return []

    def orders_context(request: Request, params=None, *, scope: str = "", **extra) -> dict:
        snapshot = load(request)
        params = params if params is not None else request.query_params
        scope = scope or str(params.get("scope") or "")
        if scope not in SCOPES:
            scope = ""
        # Each page replays ITS OWN memory: a bare /audit comes back as the Audit page was left.
        filters = Filters.from_query(remembered(request, params, scope=scope))
        rows = snapshot.rows
        findings = None
        if scope == "audit":
            report = audit_report(snapshot)
            checks = tuple(c for c in query_values(params, "check") if c)
            wanted = report.keys_for(checks)
            rows = [r for r in rows if row_key(r) in wanted]
            findings = {
                k: [(f.check, f.line) for f in fs if not checks or f.check in checks]
                for k, fs in report.by_key.items() if k in wanted
            }
            extra.update(audit=report, checks=checks)
        elif scope == "recon":
            report = recon_summary(snapshot)
            kind = str(params.get("kind") or "")  # the tiles: short-paid / over-paid orders only
            kind = kind if kind in ("short", "over") else ""
            rows = [r for r in rows if r.order_id in report.keys and (not kind or report.kind_of(r.order_id) == kind)]
            findings = recon_findings(rows, report)
            extra.update(recon=report, kind=kind)
        by_order: dict[str, list] = {}
        for key, lines in (findings or {}).items():
            bucket = by_order.setdefault(key[0], [])
            for line in lines:
                if line not in bucket:
                    bucket.append(line)
        if filters.sort == "finding" and findings is None:  # Orders remembers no such column
            filters = dataclasses.replace(filters, sort=DEFAULT_SORT, desc=True, explicit_sort=False)
        if filters.sort == "finding":
            rows = sort_by_finding(filter_rows(rows, filters), findings, row_key, desc=filters.desc)
        else:
            rows = sort_rows(filter_rows(rows, filters), filters)
        context = {
            "snapshot": snapshot, "filters": filters, "rows": rows, "total": len(snapshot.rows),
            "facets": facets(snapshot.rows), "columns": column_headings(),
            "editable": writer is not None, "wide": True,
            "choices": cell_choices(snapshot.rows, settings_cards()) if writer is not None else {},
            "scope": scope, "base": SCOPES[scope], "findings": findings,
            "findings_by_order": by_order, **extra,
        }
        # The Export CSV link: this page's own CSV route with the view's whole query (Audit's
        # check picks and Recon's short / over tile ride along; the pager never does).
        from web.queries import query_string

        export_query = dict(filters.as_query())
        if scope == "audit" and context.get("checks"):
            export_query["check"] = list(context["checks"])
        elif scope == "recon" and params.get("kind") in ("short", "over"):
            export_query["kind"] = params.get("kind")
        context["export_href"] = f"{SCOPES[scope]}.csv?{query_string(export_query)}"
        if filters.view == "cards":
            context["pager"] = paginate(order_cards(rows), filters.per, filters.page)
        return context

    def partial_for(filters: Filters) -> str:
        return "_orders_cards.html" if filters.view == "cards" else "_orders_table.html"

    @app.get("/orders", response_class=HTMLResponse)
    def orders(request: Request):
        if request.query_params.get("reset"):
            # The Reset button: forget the remembered filters / sort (the view and page size are
            # layout preferences and stay) and land on the defaults.
            response = RedirectResponse(url="/orders", status_code=303)
            response.delete_cookie(FILTERS_COOKIE)
            return response
        context = orders_context(request, notice=request.query_params.get("notice", ""))
        # htmx asks for just the table / cards; a plain browser request gets the whole page.
        if request.headers.get("HX-Request", "").lower() == "true":
            response = page(request, partial_for(context["filters"]), **context)
        else:
            response = page(request, "orders.html", **context)
        return remember(request, response, context["filters"])

    def scoped(request: Request, scope: str):
        """The Audit / Reconciliation page: the Orders view over that scope's rows."""
        if request.query_params.get("reset"):
            response = RedirectResponse(url=SCOPES[scope], status_code=303)
            response.delete_cookie(cookie(FILTERS_COOKIE, scope))
            return response
        context = orders_context(request, scope=scope, notice=request.query_params.get("notice", ""))
        if request.headers.get("HX-Request", "").lower() == "true":
            response = page(request, partial_for(context["filters"]), **context)
        else:
            response = page(request, f"{scope}.html", **context)
        return remember(request, response, context["filters"], scope)

    @app.get("/audit", response_class=HTMLResponse)
    def audit(request: Request):
        return scoped(request, "audit")

    @app.get("/recon", response_class=HTMLResponse)
    def recon(request: Request):
        return scoped(request, "recon")

    # ---- Taxes: the year on Schedule C, with the hand-entered items (web/tax_inputs.py) ---------
    data_dir = tax_inputs_path.parent

    def tax_prompts(snapshot, year: int) -> tuple[list, list]:
        try:
            from config.profiles import load_profiles

            profiles = load_profiles()
        except Exception:  # noqa: BLE001 -- a broken profiles section must not hide the summary
            profiles = []
        try:
            from config.cards import load_cards

            cards = load_cards()
        except Exception:  # noqa: BLE001
            cards = []
        return (tax_inputs.program_prompts(profiles),
                tax_inputs.card_prompts(snapshot.rows, year, cards))

    def taxes_page(request: Request, year: int, **extra):
        return page(request, "taxes.html", **taxes_context(request, year, **extra))

    def taxes_swap(request: Request, year: int, *, message: str = "", error: str = "", status: int = 200):
        """An in-place save's answer: the inputs form, the summary (out of band) and the toast."""
        response = page_no_snapshot(request, "_tax_swap.html", message=message, **taxes_context(request, year, error=error))
        response.status_code = status
        return response

    def taxes_expenses_swap(request: Request, year: int, *, message: str = "", error: str = "", draft=None, status: int = 200):
        """An in-place expense add's answer: the panel, the summary (out of band) and the toast."""
        response = page_no_snapshot(request, "_tax_expenses_swap.html", message=message,
                                    **taxes_context(request, year, error=error, draft=draft))
        response.status_code = status
        return response

    def taxes_context(request: Request, year: int, **extra) -> dict:
        from web.settings_form import profile_labels

        snapshot = load(request)
        years = tax_years(snapshot)
        inputs = load_tax_inputs(year)
        programs, cards = tax_prompts(snapshot, year)
        summary = tax_inputs.schedule_c(tax_report_for(snapshot, year), inputs)
        program_logs, site_logs, bonus_logs = tax_inputs.log_entries(inputs, year)  # the dated logs' rows
        close_why = close_year_refusal(snapshot, year)
        # the year steps like the overview's month: through the listed years, the
        # right arrow greyed on the latest listed one (a future year can be opened), not on the current
        older = [y for y in years if y < year]
        newer = [y for y in years if y > year]
        year_prev, year_next = (max(older) if older else None), (min(newer) if newer else None)
        try:
            labels = profile_labels()
        except Exception:  # noqa: BLE001
            labels = []
        draft = extra.pop("draft", None)  # a refused add re-renders the form with what was typed
        esort = str(request.query_params.get("esort") or "").strip()  # the expenses table's header sort
        edir = "asc" if str(request.query_params.get("edir") or "").lower() == "asc" else "desc"
        # The expenses grid is one page at a time, newest date first by
        # default; the size is a preset, remembered in a cookie like the other grids', the page
        # never (it is where you were). Every sort link lands on page 1 by carrying no epage.
        eper = _expenses_per(request)
        try:
            epage = max(1, int(str(request.query_params.get("epage") or 1)))
        except ValueError:
            epage = 1
        ordered = tax_inputs.sort_expenses(inputs.expenses, esort or "date", edir == "desc" if esort else True)
        epager = paginate(ordered, eper or max(1, len(ordered)), epage)
        return dict(snapshot=snapshot, year=year, years=years, epager=epager, eper=eper,
                    inputs=inputs, summary=summary, program_prompts=programs, card_prompts=cards,
                    site_names=tax_inputs.site_names(inputs), profile_labels=labels,
                    program_logs=program_logs, site_logs=site_logs, bonus_logs=bonus_logs,
                    other_logs=tax_inputs.other_logs(inputs, year), closable=not close_why, close_why=close_why,
                    year_prev=year_prev, year_next=year_next, this_year=clock().year,
                    today=tax_inputs.log_default_date(year, clock().date().isoformat()),  # the logs' new row: in the year
                    draft=draft or {}, expense_choices=tax_inputs.expense_choices_all(tax_inputs.load_all(tax_inputs_path)),
                    expenses=epager["items"],
                    esort=esort, edir=edir, **extra)

    EXPENSES_PER_CHOICES = (25, 50, 100, 0)  # 0 = all of them
    EXPENSES_PER_DEFAULT = 25  # 25 a page
    EXPENSES_PER_COOKIE = "expenses-per"

    def _expenses_per(request: Request) -> int:
        """The expenses page size: the query's preset, else this browser's remembered one, else 25."""
        raw = request.query_params.get("eper")
        if raw is None or raw == "":
            raw = request.cookies.get(EXPENSES_PER_COOKIE, "")
        try:
            per = int(str(raw))
        except ValueError:
            return EXPENSES_PER_DEFAULT
        return per if per in EXPENSES_PER_CHOICES else EXPENSES_PER_DEFAULT

    def load_tax_inputs(year: int):
        return tax_inputs.load_year(tax_inputs_path, year)

    def requested_year(request: Request, default: int, form=None) -> int:
        text = str((form.get("year") if form is not None else None) or request.query_params.get("year") or "").strip()
        return int(text) if len(text) == 4 and text.isdigit() else default

    # ---- downloads: the rows in view as CSV; a tax year as one zip ----------
    @app.get("/orders.csv")
    def orders_csv_download(request: Request):
        """The Orders page's rows IN VIEW -- the same filters, search and sort the page shows,
        every page of them -- as CSV in the ledger's column order, cells as the page displays
        them. The link carries the query, so no remembered filter is replayed here."""
        from web import export

        snapshot = load(request)
        filters = Filters.from_query(request.query_params)
        rows = sort_rows(filter_rows(snapshot.rows, filters), filters)
        name = f"orders_{clock().date().isoformat()}.csv"
        return Response(export.orders_csv(rows, cell), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/audit.csv")
    def audit_csv_download(request: Request):
        """The Audit page's rows in view, plus its Finding column."""
        from web import export

        context = orders_context(request, scope="audit")
        name = f"audit_{clock().date().isoformat()}.csv"
        return Response(export.orders_csv(context["rows"], cell, ("Finding", export.findings_text(context["findings"], row_key))),
                        media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/recon.csv")
    def recon_csv_download(request: Request):
        """The Reconciliation page's rows in view, plus its Finding column."""
        from web import export

        context = orders_context(request, scope="recon")
        name = f"recon_{clock().date().isoformat()}.csv"
        return Response(export.orders_csv(context["rows"], cell, ("Finding", export.findings_text(context["findings"], row_key))),
                        media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/activity.csv")
    def activity_csv_download(request: Request):
        """The Activity page's events in view -- its filters, search, run and sort, every page."""
        from starlette.datastructures import QueryParams

        from web import export

        params = QueryParams([(k, v) for k, v in request.query_params.multi_items() if k not in ("per", "page")] + [("per", "0")])
        _filters, context = activity_context(request, params)
        name = f"activity_{clock().date().isoformat()}.csv"
        return Response(export.activity_csv(context["events"], activity_module.KINDS), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/taxes/expenses.csv")
    def expenses_csv_download(request: Request):
        """The year's expense list as the grid sorts it, every page."""
        from web import export

        year = requested_year(request, clock().year)
        esort = str(request.query_params.get("esort") or "").strip()
        edir = "asc" if str(request.query_params.get("edir") or "").lower() == "asc" else "desc"
        expenses = tax_inputs.sort_expenses(load_tax_inputs(year).expenses, esort or "date", edir == "desc" if esort else True)
        return Response(export.expenses_csv(expenses), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="expenses_{year}.csv"'})

    @app.get("/taxes/export")
    def taxes_export(request: Request):
        """Everything of a tax year in one organised zip -- the Schedule C lines, the orders placed
        and the orders paid out, the expense list with its uploaded receipts, every dated income
        entry, the notes, and the order receipts held on disk -- the binder an audit asks for."""
        from web import export, receipts_upload

        year = requested_year(request, clock().year)
        snapshot = load(request)
        inputs = load_tax_inputs(year)
        programs, cards = tax_prompts(snapshot, year)
        labels = {p.key: p.label for p in programs}
        labels.update({f"bonus:{p.last4}": p.label for p in cards})
        program_logs, site_logs, bonus_logs = tax_inputs.log_entries(inputs, year)
        data = export.year_bundle(
            year, rows=snapshot.rows, cell=cell,
            summary=tax_inputs.schedule_c(tax_report_for(snapshot, year), inputs),
            inputs=inputs, labels=labels, program_logs=program_logs, site_logs=site_logs, bonus_logs=bonus_logs,
            other_logs=tax_inputs.other_logs(inputs, year),
            expense_file=lambda entry_id: tax_inputs.receipt_path(inputs, entry_id, data_dir=data_dir),
            receipt_file=receipts_upload.receipt_file_path, generated_at=clock())
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="tax_{year}.zip"'})

    @app.get("/taxes", response_class=HTMLResponse)
    def taxes(request: Request):
        response = taxes_page(request, requested_year(request, clock().year),
                              notice=request.query_params.get("notice", ""),
                              error=request.query_params.get("error", ""))
        if request.query_params.get("eper"):  # an explicit page size updates the memory
            response.set_cookie(EXPENSES_PER_COOKIE, str(_expenses_per(request)), max_age=365 * 24 * 3600, samesite="lax")
        return response

    @app.post("/taxes/save")
    async def taxes_save(request: Request):
        form = await request.form()
        year = requested_year(request, clock().year, form)
        programs, cards = tax_prompts(load(request), year)
        inputs = load_tax_inputs(year)
        in_place = request.headers.get("HX-Request", "").lower() == "true"
        try:
            inputs = tax_inputs.apply_form(inputs, form, programs + cards, today=clock().date().isoformat(), year=year)
        except ValueError as exc:
            if in_place:
                return taxes_swap(request, year, error=str(exc), status=400)
            return taxes_page(request, year, error=str(exc))
        tax_inputs.save_year(tax_inputs_path, year, inputs)
        act("settings", f"Tax inputs for {year} saved",
            {"year": year, "programs": inputs.program_total, "bonuses": inputs.bonus_total,
             "sites": inputs.site_total, "other": len(inputs.other)})
        if in_place:  # the form swaps itself, the summary follows, a toast says so
            return taxes_swap(request, year, message=f"Saved {year}")
        return RedirectResponse(url=f"/taxes?year={year}&notice=Saved+{year}", status_code=303)

    async def expense_form(request: Request):
        """The expense form's parts: (year, fields, receipt_file or None)."""
        form = await request.form()
        year = requested_year(request, clock().year, form)
        upload = form.get("receipt_file")
        receipt_file = None
        if upload is not None and getattr(upload, "filename", ""):
            payload = await upload.read()
            if payload:
                receipt_file = (upload.filename, payload)
        fields = {k: str(v) for k, v in form.items() if isinstance(v, str)}
        return year, fields, receipt_file

    @app.post("/taxes/close")
    async def taxes_close(request: Request):
        """Close a year: its saved inputs and receipts go, and it leaves the year list. Refused for the current year and for a year with ledger rows."""
        form = await request.form()
        year = requested_year(request, clock().year, form)
        snapshot = load(request)
        why = close_year_refusal(snapshot, year)
        if why:
            return taxes_page(request, year, error=f"{year} cannot be closed: {why}.")
        gone = tax_inputs.remove_year(tax_inputs_path, year, data_dir=data_dir)
        act("settings", f"Tax year {year} closed", {"year": year, "expenses": len(gone.expenses) if gone else 0})
        return RedirectResponse(url=f"/taxes?year={clock().year}&notice=Closed+{year}", status_code=303)

    @app.post("/taxes/expense")
    async def taxes_expense_add(request: Request):
        """One expense with its receipt: every field required (web/tax_inputs.add_expense)."""
        year, fields, receipt_file = await expense_form(request)
        if receipt_file:  # twin names are numbered across every year
            receipt_file = (tax_inputs.number_receipt_name(tax_inputs_path, receipt_file[0], data_dir=data_dir), receipt_file[1])
        inputs = load_tax_inputs(year)  # after the numbering: it may have renamed a twin in this year
        in_place = request.headers.get("HX-Request", "").lower() == "true"
        try:
            entry = tax_inputs.add_expense(inputs, fields, year=year, data_dir=data_dir,
                                           receipt_file=receipt_file)
        except ValueError as exc:
            if in_place:
                return taxes_expenses_swap(request, year, error=str(exc), draft=fields, status=400)
            return taxes_page(request, year, error=str(exc), draft=fields)
        tax_inputs.save_year(tax_inputs_path, year, inputs)
        act("settings", f"Expense added to {year}: {entry['description']} ({entry['amount']:.2f})",
            {"year": year, "id": entry["id"], "amount": entry["amount"], "profile": entry["profile"]})
        if in_place:  # the panel swaps itself, the summary follows
            return taxes_expenses_swap(request, year, message=f"Added {entry['description']}")
        return RedirectResponse(url=f"/taxes?year={year}&notice={quote('Added ' + entry['description'])}#s-expenses",
                                status_code=303)

    @app.post("/taxes/expense/cell", response_class=HTMLResponse)
    async def taxes_expense_cell(request: Request):
        """Write ONE cell of the expenses table (the Taxes page's inline editor, the Orders grid's
        machinery) and answer with that cell re-rendered. Always 200 with the cell: an error rides
        in its data-error, so htmx swaps it in and the message shows where the edit was made."""
        form = await request.form()
        year = requested_year(request, clock().year, form)
        entry_id = str(form.get("entry_id", ""))
        field = str(form.get("field", ""))
        value = str(form.get("value", ""))
        expected = form.get("expected")
        inputs = load_tax_inputs(year)
        error = ""
        try:
            tax_inputs.update_expense_field(inputs, entry_id, field, value, year=year, data_dir=data_dir,
                                            expected=None if expected is None else str(expected))
        except KeyError:
            error = "no such expense (the table is stale: reload the page)"
        except ValueError as exc:
            error = str(exc)
        entry = next((e for e in inputs.expenses if e["id"] == entry_id), None)
        if not error:
            tax_inputs.save_year(tax_inputs_path, year, inputs)
            act("settings", f"Expense edited in {year}: {field} on {entry['description']}: {expected!r} → {value!r}",
                {"year": year, "id": entry_id, "field": field, "was": expected, "now": value})
        return page_no_snapshot(request, "_expense_cell.html", e=entry, field=field, entry_id=entry_id,
                                year=year, error=error)

    @app.post("/taxes/expense/{entry_id}/receipt")
    async def taxes_expense_receipt(request: Request, entry_id: str):
        """The receipt cell's ⤒ button: the chosen file becomes the entry's receipt (the old file
        is deleted), then back to the list."""
        year, _fields, receipt_file = await expense_form(request)
        back = f"/taxes?year={year}"
        if receipt_file is None:
            return RedirectResponse(url=f"{back}&error={quote('No file was chosen')}#s-expenses", status_code=303)
        inputs = load_tax_inputs(year)
        try:
            receipt_file = (tax_inputs.number_receipt_name(tax_inputs_path, receipt_file[0], data_dir=data_dir), receipt_file[1])
            inputs = load_tax_inputs(year)
            entry = tax_inputs.replace_receipt(inputs, entry_id, receipt_file, year=year, data_dir=data_dir)
        except KeyError:
            raise HTTPException(status_code=404, detail="no such expense")
        except ValueError as exc:
            return RedirectResponse(url=f"{back}&error={quote(str(exc))}#s-expenses", status_code=303)
        tax_inputs.save_year(tax_inputs_path, year, inputs)
        act("settings", f"Receipt uploaded for an expense in {year}: {entry['description']} ({entry['receipt'].get('name', '')})",
            {"year": year, "id": entry_id, "file": entry["receipt"].get("file", "")})
        return RedirectResponse(url=f"{back}&notice={quote('Receipt uploaded for ' + entry['description'])}#s-expenses",
                                status_code=303)

    @app.post("/taxes/expenses/delete")
    async def taxes_expenses_delete(request: Request):
        """Delete every selected expense (the table's row selection, confirmed once)."""
        form = await request.form()
        year = requested_year(request, clock().year, form)
        inputs = load_tax_inputs(year)
        gone = tax_inputs.remove_expenses(inputs, [str(v) for v in form.getlist("sel")], data_dir=data_dir)
        if gone:
            tax_inputs.save_year(tax_inputs_path, year, inputs)
            act("settings", f"{len(gone)} expense(s) removed from {year}: " + ", ".join(e["description"] for e in gone[:10]),
                {"year": year, "ids": [e["id"] for e in gone]})
        return RedirectResponse(url=f"/taxes?year={year}&notice={quote(f'Deleted {len(gone)} expense(s)')}#s-expenses",
                                status_code=303)

    @app.post("/taxes/expense/{entry_id}/delete")
    def taxes_expense_delete(request: Request, entry_id: str):
        year = requested_year(request, clock().year)
        inputs = load_tax_inputs(year)
        entry = tax_inputs.remove_expense(inputs, entry_id, data_dir=data_dir)
        if entry is None:
            raise HTTPException(status_code=404, detail="no such expense")
        tax_inputs.save_year(tax_inputs_path, year, inputs)
        act("settings", f"Expense removed from {year}: {entry['description']}", {"year": year, "id": entry_id})
        return RedirectResponse(url=f"/taxes?year={year}&notice={quote('Removed ' + entry['description'])}#s-expenses",
                                status_code=303)

    @app.get("/taxes/receipt/{entry_id}")
    def taxes_receipt(request: Request, entry_id: str):
        year = requested_year(request, clock().year)
        path = tax_inputs.receipt_path(load_tax_inputs(year), entry_id, data_dir=data_dir)
        if path is None:
            raise HTTPException(status_code=404)
        return FileResponse(str(path), filename=path.name)

    def selected_keys(form) -> list[dict]:
        keys = []
        for raw in form.getlist("sel"):
            try:
                key = json.loads(raw)
            except ValueError:
                continue
            if isinstance(key, dict):
                keys.append(key)
        return keys

    def table_after(request: Request, form, *, notice: str = "", error: str = ""):
        """The table re-rendered from a forced re-read, with the page's filters (posted along
        with the form) still applied and a notice or error line on top."""
        context = orders_context(request, params=form, notice=notice, error=error)
        return page(request, partial_for(context["filters"]), **context)

    @app.post("/orders/delete", response_class=HTMLResponse)
    async def orders_delete(request: Request):
        """Delete every selected row (the page confirms first)."""
        form = await request.form()
        keys = selected_keys(form)
        if writer is None:
            return table_after(request, form, error="editing is off: this backend is a CSV snapshot")
        # the receipts the rows going away link to: deleted below once no remaining row links to
        # them (one order is one document, shared by its rows)
        going = {(k.get("order_id", ""), k.get("order_date", ""), k.get("item_name", ""), str(k.get("shipment", ""))) for k in keys}
        links = {r.text("receipt_link") for r in load(request).rows
                 if (r.text("order_id"), r.text("order_date"), r.text("item_name"), r.text("shipment")) in going
                 and r.text("receipt_link").startswith("/receipts/")}
        try:
            result = writer.remove_rows(keys)
        except EditError as exc:
            return table_after(request, form, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return table_after(request, form, error=f"{type(exc).__name__}: {exc}")
        snapshot = reader.load(force=True)
        remaining = {r.text("receipt_link") for r in snapshot.rows}
        removed = sum(1 for link in links - remaining if receipts_upload.delete_receipt(link))
        act("edit", f"Deleted {result['deleted']} row(s) from the ledger",
            {"rows": result["deleted"], "order_ids": sorted({k.get("order_id", "") for k in keys}),
             "keys": keys[:40], "receipts_removed": removed})
        note = f"Deleted {result['deleted']} row(s)" + (f" and {removed} receipt file(s)" if removed else "")
        return table_after(request, form, notice=note)

    from web import receipts_upload

    async def receipt_from(form, *, retailer: str, order_id: str, order_date: str) -> str:
        """The Receipt Link for an uploaded `receipt_file`, "" when none was sent."""
        upload = form.get("receipt_file")
        if upload is None or not getattr(upload, "filename", ""):
            return ""
        data = await upload.read()
        return receipts_upload.store_receipt(retailer=retailer, order_id=order_id,
                                            order_date=order_date, filename=upload.filename,
                                            data=data)

    @app.post("/orders/add")
    async def orders_add(request: Request):
        """Append one row from the Add-a-row form, then show it. A photo / PDF in `receipt_file`
        is stored first and its link becomes the row's Receipt Link."""
        form = await request.form()
        fields = {k: str(v) for k, v in form.items() if isinstance(v, str)}
        if writer is None:
            raise HTTPException(status_code=409, detail="editing is off: this backend is a CSV snapshot")

        def refused(message: str, status: int):
            context = orders_context(request, params={}, error=message, add_form=fields,
                                     add_open=True)
            response = page(request, "orders.html", **context)
            response.status_code = status
            return response

        try:
            link = await receipt_from(form, retailer=fields.get("retailer", ""),
                                      order_id=fields.get("order_id", ""),
                                      order_date=fields.get("order_date", ""))
        except receipts_upload.UploadError as exc:
            return refused(f"receipt not uploaded: {exc}", 400)
        if link:
            fields["receipt_url"] = link
        try:
            result = writer.add_row(fields)
        except EditError as exc:
            return refused(str(exc), exc.status)
        reader.load(force=True)
        notice = f"Added {result['key']['order_id']} at row {result['row_number']}"
        act("edit", f"Added a row for {result['key']['order_id']} ({fields.get('item_name', '')})",
            {"order_id": result["key"]["order_id"], "row_number": result["row_number"],
             "fields": {k: v for k, v in fields.items() if v}})
        return RedirectResponse(
            url="/orders?" + urlencode({"q": result["key"]["order_id"], "notice": notice}),
            status_code=303)

    @app.post("/orders/cell", response_class=HTMLResponse)
    async def orders_cell(request: Request):
        """Write ONE cell (the Orders page's inline editor) and answer with that cell re-rendered
        from a fresh read. Always 200 with the cell: an error rides in the cell's data-error, so
        htmx swaps it in and the page shows the message where the edit was made."""
        form = await request.form()
        key = {k: str(form.get(k, "")) for k in ("order_id", "order_date", "item_name", "shipment")}
        field = str(form.get("field", ""))
        value = str(form.get("value", ""))
        expected = form.get("expected")
        protect = str(form.get("protect", "1")).strip().lower() not in ("0", "false", "off", "no")
        error = ""
        if writer is None:
            error = "editing is off: this backend is a CSV snapshot"
        elif field not in EDITABLE_FIELDS:
            error = f"{field} is not editable"
        else:
            try:
                writer.write_cell(key, field, value,
                                  None if expected is None else str(expected), protect=protect)
                act("edit", f"{FIELD_TO_HEADER.get(field, field)} on {key['order_id']} "
                            f"(shipment {key['shipment']}): {expected!r} → {value!r}"
                            + ("" if protect else " (a correction; runs may overwrite it)"),
                    {"order_id": key["order_id"], "item_name": key["item_name"],
                     "shipment": key["shipment"], "field": field, "was": expected, "now": value,
                     "protect": protect})
            except EditError as exc:
                error = str(exc)
            except Exception as exc:  # noqa: BLE001 -- the page must show why, not a 500
                error = f"{type(exc).__name__}: {exc}"
        snapshot = reader.load(force=not error)
        row = next((r for r in snapshot.rows
                    if r.order_id == key["order_id"] and r.order_date == key["order_date"]
                    and r.item_name == key["item_name"] and r.shipment == key["shipment"]), None)
        return page(request, "_cell_response.html", snapshot=snapshot, row=row, col=field, key=key,
                    editable=writer is not None, error=error,
                    choices=cell_choices(snapshot.rows, settings_cards()) if field in CHOICE_FIELDS else None)

    @app.post("/orders/cell/release", response_class=HTMLResponse)
    @app.post("/orders/cell/protect", response_class=HTMLResponse)
    async def orders_cell_mark(request: Request):
        """The hand-edit mark on ONE cell, value kept: /release drops it (the runs may write the
        cell again), /protect sets it on the cell's current value. The grid's Ctrl+Shift+H and the
        count line's button toggle every selected cell this way. Answers with the cell re-rendered."""
        releasing = request.url.path.endswith("/release")
        form = await request.form()
        key = {k: str(form.get(k, "")) for k in ("order_id", "order_date", "item_name", "shipment")}
        field = str(form.get("field", ""))
        error = ""
        if writer is None:
            error = "editing is off: this backend is a CSV snapshot"
        elif field not in EDITABLE_FIELDS:
            error = f"{field} is not editable"
        else:
            try:
                if releasing:
                    writer.release_cell(key, field)
                    what = "hand edit released, value kept (runs may write it again)"
                else:
                    writer.protect_cell(key, field)
                    what = "marked as a hand edit (runs keep this value)"
                act("edit", f"{FIELD_TO_HEADER.get(field, field)} on {key['order_id']} (shipment {key['shipment']}): {what}",
                    {"order_id": key["order_id"], "item_name": key["item_name"], "shipment": key["shipment"],
                     "field": field, "released": releasing, "protected": not releasing})
            except EditError as exc:
                error = str(exc)
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
        snapshot = reader.load(force=not error)
        row = next((r for r in snapshot.rows
                    if r.order_id == key["order_id"] and r.order_date == key["order_date"]
                    and r.item_name == key["item_name"] and r.shipment == key["shipment"]), None)
        return page(request, "_cell_response.html", snapshot=snapshot, row=row, col=field, key=key,
                    editable=writer is not None, error=error, choices=None)

    @app.get("/orders/{order_id}", response_class=HTMLResponse)
    def order(request: Request, order_id: str):
        snapshot = load(request)
        view = order_view(snapshot.by_order(order_id))
        if view is None:
            raise HTTPException(status_code=404, detail=f"No ledger row carries Order ID {order_id!r}")
        return page(request, "order.html", snapshot=snapshot, order=view, columns=column_headings(),
                    editable=writer is not None,
                    notice=request.query_params.get("notice", ""),
                    error=request.query_params.get("error", ""))

    @app.post("/orders/{order_id}/receipt")
    async def order_receipt(request: Request, order_id: str):
        """Upload a receipt for an EXISTING order: store it, then write the link into Receipt Link
        on every row of that order (the link is per order -- docs/data-model.md). From the order
        page the answer is a redirect back to it; from the Orders table (`next=table`, the cell's
        upload button) it is the table re-rendered with a notice, as a cell edit answers."""
        form = await request.form()
        to_table = str(form.get("next", "")) == "table"
        snapshot = reader.load()
        rows = snapshot.by_order(order_id)
        if not rows:
            raise HTTPException(status_code=404, detail=f"No ledger row carries Order ID {order_id!r}")
        if writer is None:
            raise HTTPException(status_code=409, detail="editing is off: this backend is a CSV snapshot")
        first = rows[0]
        try:
            link = await receipt_from(form, retailer=first.retailer, order_id=order_id,
                                      order_date=first.order_date)
            if not link:
                raise receipts_upload.UploadError("choose a file first")
            keys = [{"order_id": r.order_id, "order_date": r.order_date, "item_name": r.item_name,
                     "shipment": r.shipment} for r in rows]
            result = writer.write_cells(keys, "receipt_url", link)
            act("edit", f"Receipt uploaded for {order_id}: linked on {result['written']} row(s)",
                {"order_id": order_id, "link": link, "rows": result["written"]})
        except (receipts_upload.UploadError, EditError) as exc:
            if to_table:
                return table_after(request, form, error=str(exc))
            return RedirectResponse(url=f"/orders/{order_id}?" + urlencode({"error": str(exc)}),
                                    status_code=303)
        reader.load(force=True)
        notice = f"Receipt stored and linked on {result['written']} row(s)"
        if to_table:
            return table_after(request, form, notice=notice)
        return RedirectResponse(url=f"/orders/{order_id}?" + urlencode({"notice": notice}),
                                status_code=303)

    # --- the Tools page: scripts run from the browser, the profile login embedded
    from web import tools as tools_module
    from web.ledger_writer import run_in_progress as _run_in_progress

    tool_runner = tools_module.JobRunner(logs_dir, clock=clock)

    def nav_tools() -> list[tuple[str, list[tuple[str, str]]]]:
        """The header's Tools menu: every tool this backend shows, grouped in GROUPS order."""
        # The importer is a page of its own (Tools > Import > Run importer).
        out = [("Import", [("import", "Run importer")])]
        for group in tools_module.GROUPS:
            items = [(t.key, t.title) for t in tools_module.TOOLS if t.group == group]
            if group == "Accounts":
                # The profile login is a page of its own, not a script; it belongs with the
                # account tools.
                items.insert(0, ("profile", "Log a profile in"))
            if items:
                out.append((group, items))
        return out

    templates.env.globals["nav_tools"] = nav_tools
    profile_sessions = tools_module.ProfileSessions(
        logs_dir, minutes=int(getattr(settings, "web_tool_session_minutes", 60) or 60), clock=clock)
    app.state.tool_runner = tool_runner
    app.state.profile_sessions = profile_sessions

    def tools_page(request: Request, *, message: str = "", error: str = "", status: int = 200):
        runner = app.state.tool_runner
        sessions = app.state.profile_sessions
        expired = sessions.expire_if_due()
        if expired:
            act("tool", f"Profile session for {expired['label']} closed: {expired['reason']}; {expired['saved']}",
                {k: v for k, v in expired.items() if k not in ("guidance", "live_url")})
        clear = request.query_params.get("clear", "")
        if clear:
            for job in [j for j in runner.jobs.values() if j.tool_key == clear and not j.running]:
                runner.jobs.pop(job.id, None)
        jobs = {}
        for job in sorted(runner.jobs.values(), key=lambda j: j.started_at):
            jobs[job.tool_key] = job  # the newest per tool
        shown = list(tools_module.TOOLS)
        session = sessions.current()
        # One tool at a time, picked from a dropdown
        options = [("profile", "Accounts · Log a profile in")] + [
            (t.key, f"{t.group} · {t.title}") for t in shown]
        selected = str(request.query_params.get("tool") or "profile")
        if selected != "profile" and selected not in {t.key for t in shown}:
            selected = "profile"
        chosen = next((t for t in shown if t.key == selected), None)
        hint = (chosen.blurb if chosen else "opens a live browser on a profile for a manual login")
        response = page_no_snapshot(
            request, "tools.html", message=message, error=error, tools=shown, options=options,
            selected=selected, tool=chosen, hint=hint,
            groups=tools_module.GROUPS, jobs=jobs, session=session,
            remaining=sessions.remaining_seconds(session) if session else 0,
            session_minutes=sessions.minutes, profile_labels=settings_form.profile_labels())
        response.status_code = status
        return response

    @app.get("/tools", response_class=HTMLResponse)
    def tools_get(request: Request):
        return tools_page(request, message=request.query_params.get("message", ""),
                          error=request.query_params.get("error", ""))

    @app.post("/tools/run/{key}", response_class=HTMLResponse)
    async def tools_run(request: Request, key: str):
        try:
            t = tools_module.tool(key)
        except KeyError:
            raise HTTPException(status_code=404)
        form = await request.form()
        try:
            argv = t.argv(form)
        except ValueError as exc:
            return tools_page(request, error=f"{t.title}: {exc}", status=400)
        if t.writes and _run_in_progress(logs_dir):
            return tools_page(request, error=f"{t.title} writes the ledger and a scheduled run is in "
                                             "progress (logs/.run.lock); try again when it finishes.",
                              status=423)
        try:
            job = app.state.tool_runner.start(t, argv)
        except RuntimeError as exc:
            return tools_page(request, error=str(exc), status=409)
        act("tool", f"Ran {t.title}: python -m {t.module} {' '.join(argv)}".strip(),
            {"tool": t.key, "argv": argv, "job": job.id, "writes": t.writes})
        return RedirectResponse(url=f"/tools?tool={t.key}#t-{t.key}", status_code=303)

    @app.get("/tools/jobs/{job_id}", response_class=HTMLResponse)
    def tools_job(request: Request, job_id: str):
        job = app.state.tool_runner.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404)
        body = templates.get_template("_tool_job.html").render(job=job)
        poll = (f' hx-get="/tools/jobs/{job.id}" hx-trigger="every 2s" hx-swap="outerHTML"'
                if job.running else "")
        # The card's Run button was disabled while the job ran; the polling swaps only the output
        # panel, so the finished job's response swaps the button back in out-of-band.
        t = tools_module.tool(job.tool_key)
        button = ("" if job.running else
                  f'<button type="submit" id="run-{t.key}" class="small {"danger" if t.writes else "primary"}" '
                  f'hx-swap-oob="true">Run</button>')
        if not job.running:
            act("tool", f"{tools_module.tool(job.tool_key).title} finished"
                        + (f" with exit {job.returncode}" if job.returncode else "")
                        + (f": {job.error}" if job.error else ""),
                {"tool": job.tool_key, "job": job.id, "returncode": job.returncode}) \
                if not getattr(job, "_recorded", False) else None
            job._recorded = True
        return HTMLResponse(f'<div class="tool-output" id="job-{job.id}"{poll}>{body}</div>{button}')

    @app.post("/tools/profile/start", response_class=HTMLResponse)
    async def tools_profile_start(request: Request):
        form = await request.form()
        label = str(form.get("label", "")).strip()
        add = [r.strip() for r in str(form.get("add_retailers", "")).replace(",", " ").split() if r.strip()]
        try:
            state = app.state.profile_sessions.start(label, add)
        except (ValueError, RuntimeError) as exc:
            return tools_page(request, error=str(exc), status=400)
        except Exception as exc:  # noqa: BLE001 -- the SDK's message is the useful part
            return tools_page(request, error=f"Could not open the session: {type(exc).__name__}: {exc}",
                              status=502)
        act("tool", f"Profile session opened for {label} (Browser-Use profile {state['profile_id']}"
                    f"{', new' if state['created'] else ''}); closes after {app.state.profile_sessions.minutes} min",
            {"label": label, "profile_id": state["profile_id"], "session_id": state["session_id"],
             "add_retailers": add})
        return RedirectResponse(url="/tools?tool=profile#t-profile", status_code=303)

    @app.post("/tools/profile/finish", response_class=HTMLResponse)
    def tools_profile_finish(request: Request):
        closed = app.state.profile_sessions.finish()
        if not closed:
            return tools_page(request, error="No profile session is open.", status=400)
        act("tool", f"Profile session for {closed['label']} closed: {closed['reason']}; {closed['saved']}",
            {k: v for k, v in closed.items() if k not in ("guidance", "live_url")})
        message = f"Session closed and cookies saved for {closed['label']}; {closed['saved']}."
        if closed.get("warning"):
            message += f" ({closed['warning']})"
        return RedirectResponse(url=f"/tools?message={message.replace(' ', '+')}#t-profile", status_code=303)

    # --- the activity feed ------------------------------------------------------
    from web.activity_view import ActivityFilters

    templates.env.globals["KINDS"] = activity_module.KINDS
    templates.env.globals["nav_badges"] = nav_badges
    templates.env.globals["expense_raw"] = tax_inputs.expense_raw
    templates.env.globals["run_label"] = activity_module.run_label
    templates.env.globals["run_started_at"] = activity_module.run_started_at

    def with_dossiers(events: list[dict]) -> list[dict]:
        """The failure dossiers on disk, merged in: a dossier the log already recorded gets its
        report attached; one written before the log existed (or by an older version) becomes a
        dossier event of its own, dated from its directory name. Newest first, like the rest."""
        dossiers = {d.name: d for d in failures_module.list_dossiers(failures_dir)}
        seen: set[str] = set()
        merged: list[dict] = []
        for event in events:
            if event.get("kind") == "dossier":
                name = str((event.get("details") or {}).get("name") or "")
                if name in dossiers:
                    event = {**event, "dossier": dossiers[name]}
                    seen.add(name)
            merged.append(event)
        for name, dossier in dossiers.items():
            if name in seen:
                continue
            at = dossier.at.replace(tzinfo=timezone.utc) if dossier.at and dossier.at.tzinfo is None \
                else dossier.at
            merged.append({
                "at": (at or clock()).isoformat(timespec="seconds"), "kind": "dossier",
                "summary": f"{dossier.retailer} [{dossier.profile}]: failure dossier",
                "run_id": None, "details": {"name": name, "path": str(dossier.path)},
                "dossier": dossier,
            })
        merged.sort(key=lambda e: str(e.get("at", "")), reverse=True)
        return merged

    HIDE_COOKIE = "activity-hide"
    ACTIVITY_PER_COOKIE = "activity-per"  # the page size, remembered like Orders' (never the page)

    @app.get("/activity", response_class=HTMLResponse)
    def activity_page(request: Request):
        if request.query_params.get("reset"):
            # The Reset button: type, window, search, run and order back to their defaults. The
            # hidden types are a preference and stay.
            return RedirectResponse(url="/activity", status_code=303)
        filters, context = activity_context(request, request.query_params)
        name = "_activity_rows.html" if request.headers.get("HX-Request") else "activity.html"
        response = page_no_snapshot(request, name, wide=True, **context)  # the Orders layout
        if filters.hide_set:
            if filters.hidden:
                response.set_cookie(HIDE_COOKIE, ",".join(filters.hidden), max_age=365 * 24 * 3600,
                                    samesite="lax")
            else:
                response.delete_cookie(HIDE_COOKIE)  # "hide nothing": forget, rather than store ""
        if request.query_params.get("per"):  # an explicit choice updates the memory
            response.set_cookie(ACTIVITY_PER_COOKIE, str(filters.per), max_age=365 * 24 * 3600,
                                samesite="lax")
        return response

    def activity_context(request: Request, params) -> tuple:
        """The Activity table for a query: (filters, template context). Shared by the page and by
        an acknowledgement's in-place answer."""
        import dataclasses as _dc

        from web.activity_view import PER_CHOICES as _per_choices

        filters = ActivityFilters.from_query(params)
        # The hidden types: what the form just said, else what this browser remembered.
        if not filters.hide_set:
            remembered_hidden = [k for k in request.cookies.get(HIDE_COOKIE, "").split(",") if k]
            filters = filters.with_hidden(remembered_hidden)
        if not params.get("per"):  # a request that names no size uses this browser's remembered one
            try:
                remembered_per = int(request.cookies.get(ACTIVITY_PER_COOKIE, ""))
            except ValueError:
                remembered_per = -1
            if remembered_per in _per_choices:
                filters = _dc.replace(filters, per=remembered_per)
        events = with_dossiers(activity_module.read(activity_path))
        shown = activity_module.filter_events(events, kinds=filters.kinds, q=filters.q,
                                              days=filters.days, run_id=filters.run_id, now=clock(),
                                              hidden=filters.hidden)
        loud = loud_summary()  # the rows still to acknowledge carry their own button
        unacked = {(kind, f["at"], f["summary"]) for kind in LOUD_KINDS for f in loud[kind]["events"]}
        if filters.unacked:  # the overview's card: only what is still to acknowledge
            shown = [e for e in shown if (e.get("kind"), e.get("at"), e.get("summary")) in unacked]
        if not filters.desc:
            shown = list(reversed(shown))
        if filters.sort and filters.sort != "at":  # a column's sort, stable over the time order
            field = "run_id" if filters.sort == "run" else filters.sort
            shown.sort(key=lambda e: str(e.get(field) or "").lower(), reverse=filters.desc)
        # One page, never the whole log.
        from web.queries import paginate as _paginate

        pager = _paginate(shown, per=filters.per or max(1, len(shown)), page=filters.page)
        context = {"events": pager["items"], "pager": pager, "total": len(events), "filters": filters,
                   "counts": activity_module.counts_by_kind(events), "unacked": unacked,
                   "activity_path": str(activity_path), "failures_dir": str(failures_dir)}
        return filters, context

    @app.get("/activity/dossier/{name}/download")
    def dossier_download(name: str):
        """The dossier's directory as ONE zip -- report.md, the captured pages, the screenshots, the
        selector audit -- which is what to hand an AI agent (or a person) to fix the selector that
        broke. Only a dossier the failures directory lists: no path is taken
        from the name."""
        import io
        import zipfile

        dossier = next((d for d in failures_module.list_dossiers(failures_dir) if d.name == name), None)
        if dossier is None:
            raise HTTPException(status_code=404, detail=f"no dossier named {name!r}")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(dossier.path.rglob("*")):
                if path.is_file():
                    archive.write(path, arcname=f"{dossier.name}/{path.relative_to(dossier.path).as_posix()}")
        return Response(content=buffer.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{dossier.name}.zip"'})

    @app.post("/activity/acknowledge")
    async def acknowledge(request: Request):
        """Acknowledge the alerts or the failure dossiers: with `at` (and `summary`) that ONE event
        (the Activity page's per-row button); otherwise every one up to `through` (the newest the
        overview's card showed; blank = the newest there is now). The card and the nav badge drop
        them, the Activity log records the acknowledgement (kind `ack`) so it survives a restart
        and is itself on the record, and a newer alert or dossier shows again."""
        form = await request.form()
        kind = str(form.get("kind", "")).strip()
        if kind not in LOUD_KINDS:
            raise HTTPException(status_code=400, detail=f"kind must be one of {', '.join(LOUD_KINDS)}")
        loud = loud_summary()[kind]
        label = activity_module.KINDS[kind].lower()
        at = str(form.get("at", "")).strip()
        if at:
            summary = str(form.get("summary", ""))
            if any(f["at"] == at and f["summary"] == summary for f in loud["events"]):
                act("ack", f"Acknowledged {label}: {summary}", {"kind": kind, "at": at, "summary": summary})
        else:
            through = str(form.get("through", "")).strip() or loud["newest"]
            if through:
                count = sum(1 for f in loud["events"] if f["at"] <= through)
                act("ack", f"Acknowledged {count} {label}(s) through {through}",
                    {"kind": kind, "through": through, "count": count})
        target = str(form.get("next", "/"))
        if not target.startswith("/") or target.startswith("//"):
            target = "/"
        if request.headers.get("HX-Request"):
            # In place, no page load: the Activity page gets its rows
            # again (the button gone, or the row, under "unacknowledged only"), the overview's
            # card gets nothing (it leaves), and both get the nav's Activity link out of band so
            # the badge drops.
            from urllib.parse import urlsplit

            from starlette.datastructures import QueryParams

            parts = urlsplit(target)
            body = ""
            if parts.path.startswith("/activity"):
                _filters, context = activity_context(request, QueryParams(parts.query))
                body = page_no_snapshot(request, "_activity_rows.html", wide=True, **context).body.decode("utf-8")
            link = page_no_snapshot(request, "_nav_activity.html", badges=nav_badges(), path=parts.path,
                                    oob=True).body.decode("utf-8")
            return HTMLResponse(body + link)
        return RedirectResponse(url=target, status_code=303)

    @app.get("/failures")
    def failures_page(request: Request):
        """The Failures page merged into Activity (2026-09-18); the old address lands on the
        dossier rows, all of them."""
        return RedirectResponse(url="/activity?type=dossier&days=0", status_code=303)

    # --- backup / restore (local files only; the ledger is never touched) -------------------------
    backups_dir = Path(backup_dir) if backup_dir else backup_module.BACKUPS_DIR
    repo_root = Path(repo_root_dir) if repo_root_dir else backup_module.ROOT

    def backup_context(**extra):
        archives = []
        for path in backup_module.list_backups(backups_dir):
            manifest = backup_module.read_manifest(path)
            archives.append({"name": path.name, "size_kb": path.stat().st_size // 1024,
                             "created_at": manifest.get("created_at", ""),
                             "git_commit": manifest.get("git_commit", ""),
                             "files": len(manifest.get("files", []))})
        fresh = not (repo_root / "config.json").is_file()
        return {"archives": archives, "members": [m.as_posix() for m in
                                                   backup_module.backup_members(repo_root)],
                "fresh_install": fresh, "backups_dir": str(backups_dir),
                "schedule": backup_module.schedule_from_settings(settings), **extra}

    def page_no_snapshot(request: Request, name: str, **context):
        base = {"request": request, "backend": reader.backend, "source": "", "loaded_at": None,
                "schema_matches": True, "heartbeat": heartbeat()}
        return templates.TemplateResponse(request=request, name=name, context={**base, **context})


    # ---- Tools > Import: a CSV mapped onto the ledger, with a staging sheet (web/importer.py) --------------
    def ledger_index_now() -> dict:
        try:
            return importer.ledger_index(reader.load().rows)
        except Exception:  # noqa: BLE001 -- no ledger yet: nothing to compare against
            return {"keys": set(), "orders": set(), "trackings": {}}

    def ledger_choices_now() -> dict:
        try:
            return cell_choices(reader.load().rows)
        except Exception:  # noqa: BLE001
            return {"values": {}, "card_pairs": []}

    def import_page(request: Request, *, error: str = "", notice: str = "", status: int = 200):
        batch = importer.live_batch(data_dir)
        staging = batch.load_staging() if batch else None
        rows = staging.staged if staging else []
        response = page_no_snapshot(
            request, "tools_import.html", wide=bool(rows), batch=batch, staging=staging, rows=rows,
            gaps={r.id: importer.gap_fields(r) for r in rows}, gap_names={r.id: importer.gaps_for(r) for r in rows},
            editable=True, can_import=writer is not None, choices=importer.choices_for(staging, ledger_choices_now()),
            unfinished=bool(batch and staging is None), error=error, notice=notice,
            step="staging" if rows else ("map" if batch else "upload"))
        response.status_code = status
        return response

    def staged_from_batch(batch: importer.Batch) -> tuple[importer.Staging, list[str]]:
        """The staging sheet a batch's mapping produces (not yet saved), and the orders whose
        order-level amounts were prorated."""
        mapping = batch.load_mapping() or {}
        _headers, raw = importer.read_source(batch)
        rows = importer.stage_rows(raw, mapping.get("mapping") or {}, date_order=str(mapping.get("date_order") or "mdy"),
                                   profile=str(mapping.get("profile") or ""))
        touched = importer.prorate_order_level(rows)
        staging = importer.Staging(id=batch.id, created_at=clock().isoformat(timespec="seconds"),
                                   source_name=batch.source_name, date_order=str(mapping.get("date_order") or "mdy"), rows=rows)
        return staging, touched

    @app.get("/tools/import", response_class=HTMLResponse)
    def tools_import(request: Request):
        return import_page(request, notice=request.query_params.get("notice", ""), error=request.query_params.get("error", ""))

    @app.get("/tools/import/template.csv")
    def tools_import_template():
        return Response(content="\ufeff" + importer.template_csv(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="ledger_import_template.csv"'})

    @app.post("/tools/import/upload", response_class=HTMLResponse)
    async def tools_import_upload(request: Request, source: UploadFile = File(...)):
        payload = await source.read()
        try:
            batch = importer.new_batch(data_dir, source.filename or "upload.csv", payload, clock=clock)
        except importer.StagingError as exc:
            return import_page(request, error=str(exc), status=409 if "already in progress" in str(exc) else 400)
        act("import", f"Import: {batch.source_name} uploaded ({len(payload)} bytes)", {"batch": batch.id})
        return RedirectResponse(url="/tools/import/map", status_code=303)

    def import_map_page(request: Request, batch: importer.Batch, *, error: str = "", status: int = 200):
        mapping = batch.load_mapping() or {}
        columns = importer.source_columns(batch, mapping.get("mapping") or None)
        suggested = {c["header"]: c["suggested"] for c in columns if c["suggested"]}
        try:
            verdict, samples = importer.detect_order(batch, mapping.get("mapping") or suggested)
        except importer.StagingError as exc:
            verdict, samples = None, []
            error = error or str(exc)
        response = page_no_snapshot(request, "tools_import_map.html", batch=batch, columns=columns,
                                    targets=importer.target_options(), verdict=verdict, samples=samples,
                                    date_order=str(mapping.get("date_order") or ""), profile=str(mapping.get("profile") or ""),
                                    profiles=settings_form.profile_labels(), error=error, step="map")
        response.status_code = status
        return response

    @app.get("/tools/import/map", response_class=HTMLResponse)
    def tools_import_map(request: Request):
        batch = importer.live_batch(data_dir)
        if batch is None or batch.load_staging() is not None:
            return RedirectResponse(url="/tools/import", status_code=303)
        return import_map_page(request, batch)

    @app.post("/tools/import/map", response_class=HTMLResponse)
    async def tools_import_map_save(request: Request):
        batch = importer.live_batch(data_dir)
        if batch is None or batch.load_staging() is not None:
            return RedirectResponse(url="/tools/import", status_code=303)
        form = await request.form()
        headers, _rows = importer.read_source(batch)
        try:
            mapping = importer.mapping_from_form(headers, form)
            date_order = str(form.get("date_order", "") or "").strip().lower()
            if date_order not in ("mdy", "dmy"):
                verdict, _samples = importer.detect_order(batch, mapping)
                date_order = verdict or "mdy"
        except importer.StagingError as exc:
            return import_map_page(request, batch, error=str(exc), status=400)
        batch.save_mapping(mapping, date_order=date_order, profile=str(form.get("profile", "") or "").strip())
        return RedirectResponse(url="/tools/import/preview", status_code=303)

    @app.get("/tools/import/preview", response_class=HTMLResponse)
    def tools_import_preview(request: Request):
        batch = importer.live_batch(data_dir)
        if batch is None or batch.load_staging() is not None:
            return RedirectResponse(url="/tools/import", status_code=303)
        if not (batch.load_mapping() or {}).get("mapping"):
            return RedirectResponse(url="/tools/import/map", status_code=303)
        staging, touched = staged_from_batch(batch)
        buckets = importer.classify(staging.rows, ledger_index_now())
        bucket_of = {r.id: name for name, rows in buckets.items() for r in rows}
        warnings = [(r.source_row, w) for r in staging.rows for w in r.warnings]
        return page_no_snapshot(request, "tools_import_preview.html", batch=batch, staging=staging,
                                counts={k: len(v) for k, v in buckets.items()}, bucket_of=bucket_of,
                                gap_names={r.id: importer.gaps_for(r) for r in staging.rows},
                                warnings=warnings[:40], warning_total=len(warnings), touched=touched,
                                rows=staging.rows[:200], editable=writer is not None, step="preview")

    @app.post("/tools/import/run", response_class=HTMLResponse)
    def tools_import_run(request: Request):
        """Stage every row of the mapped file, then import the complete ones (the same commit the
        sheet's button runs)."""
        batch = importer.live_batch(data_dir)
        if batch is None or batch.load_staging() is not None:
            return RedirectResponse(url="/tools/import", status_code=303)
        staging, _touched = staged_from_batch(batch)
        if writer is None:
            # A view-only backend: the sheet holds everything, nothing lands (as /orders/add refuses).
            importer.classify(staging.rows, ledger_index_now())  # the rows' notes, at least
            batch.save_staging(staging)
            act("import", f"Import: {len(staging.staged)} row(s) staged from {batch.source_name}; editing is off", {"batch": batch.id})
            return RedirectResponse(url="/tools/import?" + urlencode({"notice": f"{len(staging.staged)} row(s) staged; editing is off on this "
                                                                            "backend (a CSV snapshot), so nothing was imported"}), status_code=303)
        batch.save_staging(staging)
        return tools_import_commit(request)

    @app.post("/tools/import/cell", response_class=HTMLResponse)
    async def tools_import_cell(request: Request):
        """One cell of the staging sheet (the grid's inline editor); always 200 with the cell,
        an error riding in its data-error, as the Orders and Taxes grids answer."""
        form = await request.form()
        entry_id, field, value = str(form.get("entry_id", "")), str(form.get("field", "")), str(form.get("value", ""))
        expected = form.get("expected")
        batch = importer.live_batch(data_dir)
        staging = batch.load_staging() if batch else None
        row, error = None, ""
        if staging is None:
            error = "no staging sheet (reload the page)"
        else:
            try:
                row = importer.write_staged_cell(staging, entry_id, field, value, expected=None if expected is None else str(expected))
            except KeyError:
                error = "no such row (the sheet is stale: reload the page)"
            except importer.StagingError as exc:
                error = str(exc)
                try:
                    row = staging.row(entry_id)
                except KeyError:
                    row = None
            if not error:
                batch.save_staging(staging)
        return page_no_snapshot(request, "_import_cell.html", row=row, field=field, entry_id=entry_id,
                                gaps=importer.gap_fields(row) if row else set(), editable=True, error=error)

    @app.post("/tools/import/rows/delete")
    async def tools_import_rows_delete(request: Request):
        form = await request.form()
        ids = [str(v) for v in form.getlist("sel")]
        batch = importer.live_batch(data_dir)
        staging = batch.load_staging() if batch else None
        if staging is None or not ids:
            return RedirectResponse(url="/tools/import", status_code=303)
        removed = importer.remove_rows(staging, ids)
        batch.save_staging(staging)
        act("import", f"Import: {len(removed)} staged row(s) dropped", {"batch": batch.id, "rows": [r.id for r in removed]})
        return RedirectResponse(url="/tools/import?" + urlencode({"notice": f"{len(removed)} row(s) dropped from the staging sheet"}), status_code=303)

    @app.post("/tools/import/rows/accept", response_class=HTMLResponse)
    async def tools_import_rows_accept(request: Request):
        """Import anyway on a held row (open order / near duplicate), or `undo` to hold it again.
        Answers the row re-rendered (htmx swaps the <tr>); a plain post goes back to the sheet."""
        form = await request.form()
        row_id, undo = str(form.get("id", "") or ""), str(form.get("undo", "") or "") == "1"
        batch = importer.live_batch(data_dir)
        staging = batch.load_staging() if batch else None
        if staging is None:
            raise HTTPException(status_code=404, detail="no staging sheet")
        rows = importer.accept_rows(staging, [row_id], on=not undo)
        if not rows:
            raise HTTPException(status_code=404, detail="no such staged row")
        importer.classify(staging.rows, ledger_index_now())  # the row's note, as the sheet shows it (an undo gets its hold back)
        batch.save_staging(staging)
        act("import", f"Import: row {rows[0].source_row} {'held again' if undo else 'marked import anyway'}", {"batch": batch.id, "row": row_id})
        if request.headers.get("HX-Request") != "true":
            return RedirectResponse(url="/tools/import", status_code=303)
        r = rows[0]
        return page_no_snapshot(request, "_import_row.html", r=r, n=staging.staged.index(r) + 1,
                                row_gaps=importer.gap_fields(r), row_gap_names=importer.gaps_for(r), editable=True)

    @app.post("/tools/import/commit", response_class=HTMLResponse)
    def tools_import_commit(request: Request):
        batch = importer.live_batch(data_dir)
        staging = batch.load_staging() if batch else None
        if staging is None:
            return RedirectResponse(url="/tools/import", status_code=303)
        if writer is None:
            return import_page(request, error="editing is off: this backend is a CSV snapshot", status=409)
        try:
            result = importer.import_complete(staging, writer, ledger_index_now(), save=batch.save_staging, clock=clock)
        except RunInProgress as exc:
            return import_page(request, error=str(exc), status=423)
        reader.load(force=True)
        act("import", f"Import: {result.notice()} ({batch.source_name})",
            {"batch": batch.id, "imported": [r.cells.get("order_id", "") for r in result.imported],
             "duplicates": len(result.duplicates), "refused": len(result.refused), "staged": result.remaining})
        return RedirectResponse(url="/tools/import?" + urlencode({"notice": result.notice()}), status_code=303)

    @app.post("/tools/import/discard")
    def tools_import_discard(request: Request):
        batch = importer.live_batch(data_dir)
        if batch is not None:
            act("import", f"Import: {batch.source_name} discarded", {"batch": batch.id})
            batch.discard()
        return RedirectResponse(url="/tools/import?" + urlencode({"notice": "the import was discarded"}), status_code=303)

    @app.get("/tools/import/staging.csv")
    def tools_import_staging_csv():
        batch = importer.live_batch(data_dir)
        staging = batch.load_staging() if batch else None
        if staging is None:
            raise HTTPException(status_code=404)
        return Response(content="\ufeff" + importer.staging_csv(staging), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="ledger_import_staging.csv"'})


    # ---- the setup wizard (web/setup_wizard.py): the Settings page's fields, one step at a time ------------
    def setup_page(request: Request, key: str, *, errors: list[str] | None = None, message: str = "",
                   open_section: str = "", status: int = 200, **extra):
        step = setup_wizard.step(key)
        again = request.query_params.get("again") == "1" or setup_wizard.rerunning()
        nxt = setup_wizard.next_step(key)
        prev = setup_wizard.prev_step(key)
        suffix = "?again=1" if again else ""
        rows = settings_form.view(step.scalar_settings(), os.environ) if step.kind in ("scalars", "entries", "password") else []
        today = clock().date().isoformat()
        entries = settings_form.display_entries(step.section, today=today) if step.section else []
        response = page_no_snapshot(
            request, "setup.html", step=step, steps=setup_wizard.STEPS, index=setup_wizard.index(key), again=again,
            next_url=f"/setup/{nxt.key}{suffix}" if nxt else "", prev_url=f"/setup/{prev.key}{suffix}" if prev else "",
            rows=rows, entries=entries, errors=errors or [], message=message, open_section=open_section,
            hidden_envs=settings_form.hidden_envs(), section_title=settings_form.section_title,
            field_label=settings_form.field_label, retailer_keys=settings_form.RETAILER_KEYS,
            auth_retailers=settings_form.AUTH_RETAILERS, profile_labels=settings_form.profile_labels(),
            retailer_names=retailers_module.NAMES, default_rate_text=default_rate_text(),
            retailer_options=retailer_options(), today=today,
            card_choices=card_choices(), section_forms=[], in_container=in_container, auth_on=auth_on,
            restart=setup_wizard.pending_restart(), configured=setup_wizard.is_configured(), **extra)
        response.status_code = status
        return response

    def setup_next(key: str, request: Request) -> RedirectResponse:
        nxt = setup_wizard.next_step(key)
        again = request.query_params.get("again") == "1" or setup_wizard.rerunning()
        return RedirectResponse(url=f"/setup/{nxt.key if nxt else 'done'}{'?again=1' if again else ''}", status_code=303)

    @app.get("/setup")
    def setup_root(request: Request):
        suffix = "?again=1" if request.query_params.get("again") == "1" else ""
        return RedirectResponse(url=f"/setup/restore{suffix}", status_code=303)

    @app.get("/setup/{key}", response_class=HTMLResponse)
    def setup_step(request: Request, key: str):
        try:
            step = setup_wizard.step(key)
        except KeyError:
            raise HTTPException(status_code=404)
        if step.kind == "done":
            setup_wizard.mark_complete(clock)
            setup_wizard.clear_rerun()
            setup_state["needed"] = False
            return setup_page(request, key, message=request.query_params.get("message", ""),
                              restored=request.query_params.get("restored") == "1",
                              summary=setup_summary(), signin_again=request.query_params.get("signin") == "1")
        return setup_page(request, key, message=request.query_params.get("message", ""))

    def setup_summary() -> list[tuple[str, str]]:
        """What the wizard left behind, for the Done step: counts and set / not set, never a secret."""
        from config.loader import config_value

        out = [("Dashboard password", "set" if config_value("web.password") else "not set (no sign-in)"),
               ("Browser-Use key", "set" if config_value("browser_use.api_key") else "not set")]
        for path, label in (("profiles", "Profiles"), ("warehouses", "Buying groups"), ("cards", "Cards")):
            out.append((label, f"{len(settings_form.display_entries(path))} entered"))
        out.append(("Alerts", ", ".join(c for c, on in (("Discord", config_value("alerts.discord_enabled", True) and config_value("alerts.discord_webhook_url")),
                                                        ("Gmail", config_value("alerts.gmail_enabled", True) and config_value("alerts.gmail_address"))) if on) or "none configured"))
        out.append(("Schedule", f"every {config_value('container.run_interval_hours', 6)} h; backups {'on' if config_value('backups.enabled', True) else 'off'}"))
        return out

    @app.post("/setup/restore")
    async def setup_restore(request: Request, archive: UploadFile = File(...), force: str = Form("")):
        result, message = await do_restore(archive, bool(force))
        if "config.json" in result["restored"]:
            # A restored config is a finished setup: the wizard is over, the container re-reads it.
            settings_form.loader.reload_config()
            setup_wizard.mark_complete(clock)
            setup_wizard.note_restart("container")
            setup_state["needed"] = False
            return RedirectResponse(url="/setup/done?restored=1&message=" + message.replace(" ", "+"), status_code=303)
        return RedirectResponse(url="/setup/restore?message=" + message.replace(" ", "+"), status_code=303)

    @app.post("/setup/restart")
    def setup_rerun(request: Request):
        """Settings' "Run the setup wizard again"."""
        setup_wizard.mark_rerun(clock)
        act("settings", "Setup wizard run again from the Settings page")
        return RedirectResponse(url="/setup/restore?again=1", status_code=303)

    @app.post("/setup/password", response_class=HTMLResponse)
    async def setup_password(request: Request):
        form = await request.form()
        password = str(form.get("password", "") or "")
        confirm = str(form.get("confirm", "") or "")
        if password != confirm:
            return setup_page(request, "password", errors=["the two passwords differ"], status=400)
        if not password.strip():
            return setup_next("password", request)  # blank keeps what is set (nothing, on a fresh install)
        step = setup_wizard.step("password")
        try:
            changes = settings_form.apply_scalars({"WEB_PASSWORD": password.strip()}, settings=step.scalar_settings(),
                                                  skip=settings_form.hidden_envs())
        except settings_form.SettingsError as exc:
            return setup_page(request, "password", errors=exc.errors, status=400)
        setup_wizard.note_restart(settings_form.restart_needed(changes))
        act("settings", "Setup: dashboard password set", {"changed": sorted(changes)})
        response = setup_next("password", request)
        if not auth_on:
            # Sign this browser in for the password just set: the token is signed with the
            # persisted secret over the new password, so it is good after the restart at Done.
            secret = session_secret or auth_module.session_secret()
            if isinstance(secret, str):
                secret = secret.encode("utf-8")
            fresh = auth_module.Sessions(secret, password.strip(),
                                         session_hours=float(_setting("web_session_hours", 6)),
                                         remember_days=float(_setting("web_remember_days", 730)),
                                         clock=lambda: clock().timestamp())
            token, lifetime = fresh.issue(remember=True)
            response.set_cookie(auth_module.COOKIE, token, max_age=lifetime, httponly=True, samesite="lax", path="/")
        elif password.strip() != password_now():
            response.headers["location"] = response.headers["location"] + ("&" if "?" in response.headers["location"] else "?") + "signin=1"
        return response

    def password_now() -> str:
        return str(_setting("web_password", "")).strip()

    @app.post("/setup/{key}", response_class=HTMLResponse)
    async def setup_save(request: Request, key: str):
        """A step's scalar settings (Browser-Use, the groups' keys, alerts, the schedule): only the
        step's own settings are written, so an untouched box elsewhere is never read as blank."""
        try:
            step = setup_wizard.step(key)
        except KeyError:
            raise HTTPException(status_code=404)
        if step.kind not in ("scalars", "entries") or not (step.envs or step.sections):
            return setup_next(key, request)
        form = await request.form()
        try:
            changes = settings_form.apply_scalars(form, settings=step.scalar_settings(), skip=settings_form.hidden_envs())
        except settings_form.SettingsError as exc:
            return setup_page(request, key, errors=exc.errors, status=400)
        if changes:
            setup_wizard.note_restart(settings_form.restart_needed(changes))
            act("settings", f"Setup: {step.title} saved: {', '.join(sorted(changes))}", {"changed": sorted(changes)})
        if step.kind == "entries":
            suffix = "?again=1" if (request.query_params.get("again") == "1" or setup_wizard.rerunning()) else ""
            return RedirectResponse(url=f"/setup/{key}{suffix}", status_code=303)
        return setup_next(key, request)

    async def setup_entry(request: Request, key: str, index: int | None, delete: bool = False):
        step = setup_wizard.step(key)
        if not step.section:
            raise HTTPException(status_code=404)
        suffix = "?again=1" if (request.query_params.get("again") == "1" or setup_wizard.rerunning()) else ""
        try:
            if delete:
                label = settings_form.delete_entry(step.section, index)
                act("settings", f"Setup: removed {step.section[:-1]} {label}", {"path": step.section})
            else:
                form = await request.form()
                _landed, label = settings_form.apply_entry(step.section, index, form, today=clock().date().isoformat())
                act("settings", f"Setup: {'added' if index is None else 'saved'} {step.section[:-1]} {label}", {"path": step.section})
        except settings_form.SettingsError as exc:
            return setup_page(request, key, errors=exc.errors, open_section=step.section, status=400)
        setup_state["needed"] = None  # a profile may just have arrived: re-decide on the next request
        return RedirectResponse(url=f"/setup/{key}{suffix}", status_code=303)

    @app.post("/setup/{key}/entry", response_class=HTMLResponse)
    async def setup_add_entry(request: Request, key: str):
        return await setup_entry(request, key, None)

    @app.post("/setup/{key}/entry/{index}", response_class=HTMLResponse)
    async def setup_save_entry(request: Request, key: str, index: int):
        return await setup_entry(request, key, index)

    @app.post("/setup/{key}/entry/{index}/delete", response_class=HTMLResponse)
    async def setup_delete_entry(request: Request, key: str, index: int):
        return await setup_entry(request, key, index, delete=True)


    # The Backup page moved INTO Settings; the old address still lands there.
    @app.get("/backup")
    def backup_page(request: Request):
        message = request.query_params.get("message", "")
        query = f"?message={message.replace(' ', '+')}" if message else ""
        return RedirectResponse(url=f"/settings{query}#s-backup", status_code=303)

    @app.post("/backup")
    def backup_create(request: Request):
        target = backup_module.create_backup(repo_root, backups_dir)
        # The retention rule applies to every backup made, scheduled or by hand.
        deleted = backup_module.prune_backups(backups_dir, settings.backups_keep)
        act("backup", f"Backup {target.name} created"
            + (f"; {len(deleted)} older one(s) deleted" if deleted else ""),
            {"name": target.name, "size_kb": target.stat().st_size // 1024,
             "keep": settings.backups_keep, "deleted": [p.name for p in deleted]})
        message = f"Wrote {target.name}" + (f" and deleted {len(deleted)} older backup(s)" if deleted else "")
        return RedirectResponse(url=f"/settings?message={quote(message)}#s-backup", status_code=303)

    @app.get("/backup/{name}")
    def backup_download(name: str):
        if not name.startswith(backup_module.PREFIX) or not name.endswith(".zip") or "/" in name \
                or "\\" in name or ".." in name:
            raise HTTPException(status_code=404)
        path = backups_dir / name
        if not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(str(path), media_type="application/zip", filename=name)

    def _backup_path(name: str) -> Path:
        if not name.startswith(backup_module.PREFIX) or not name.endswith(".zip") or "/" in name \
                or "\\" in name or ".." in name:
            raise HTTPException(status_code=404)
        path = backups_dir / name
        if not path.is_file():
            raise HTTPException(status_code=404)
        return path

    @app.post("/backup/{name}/delete")
    def backup_delete(name: str):
        """Remove one backup zip. Same name
        rule as the download; confirmed in-page before the form submits."""
        _backup_path(name).unlink()
        act("backup", f"Backup {name} deleted", {"name": name})
        return RedirectResponse(url=f"/settings?message=Deleted+{name}#s-backup", status_code=303)

    async def do_restore(archive: UploadFile, force: bool) -> tuple[dict, str]:
        """Keep the upload under backups/ and restore it onto this host; (result, message). Used
        by the Backup panel and by the setup wizard's first step."""
        backups_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(archive.filename or "upload.zip").name
        if not safe_name.endswith(".zip"):
            safe_name += ".zip"
        saved = backups_dir / f"uploaded_{safe_name}"
        saved.write_bytes(await archive.read())
        result = backup_module.restore_backup(saved, repo_root, force=force)
        message = (f"Restored {len(result['restored'])} file(s), kept {len(result['skipped_existing'])}"
                   " existing" + (" (tick overwrite to replace them)" if result["skipped_existing"]
                                  else "") + ".")
        act("backup", f"Restore from {safe_name}: {message}",
            {"archive": safe_name, "force": force, "restored": result["restored"],
             "kept": result["skipped_existing"]})
        return result, message

    @app.post("/backup/restore")
    async def backup_restore(request: Request, archive: UploadFile = File(...),
                             force: str = Form("")):
        # On ANY host, confirmed
        # in-page first. Existing files are kept unless the overwrite box is ticked, so an
        # accidental upload onto a configured host changes nothing without that tick.
        result, message = await do_restore(archive, bool(force))
        url = f"/settings?message={message.replace(' ', '+')}"
        if "config.json" in result["restored"]:
            url += "&restart=container"  # the run and the dashboard read config.json at start
        return RedirectResponse(url=url + "#s-backup", status_code=303)

    # --- settings (edits config.json in place; never the ledger) ---------------------------------
    from web import settings_form

    def card_choices() -> list[tuple[str, str]]:
        """(last4, "name …last4") for every REAL card: what a virtual number picks its card from (a
        virtual number cannot belong to another virtual number)."""
        return [(str(e["last4"]), f"{e['name']} …{e['last4']}")
                for e in settings_form.display_entries("cards") if not e["virtual"]]

    def cap_usage() -> dict:
        """{(card index, cap index): allowance} over the ledger, for the bars on the card's form;
        {} where there is no ledger yet or the cards do not load (a fresh install, a typo)."""
        from config.cards import load_cards
        from ledger.cashback_caps import allowances
        from models.order import FIELDNAMES

        try:
            cards = load_cards()
            if not any(c.caps for c in cards):
                return {}
            rows = [{f: row.text(f) for f in FIELDNAMES} for row in reader.load().rows]
            return allowances(rows, cards, clock().date().isoformat())
        except Exception:  # noqa: BLE001
            log.exception("could not measure the cards' spend caps for the Settings page")
            return {}

    def settings_context(*, open_section: str = "", section_texts: dict | None = None) -> dict:
        """What settings.html and the in-place section partial both render from."""
        rows_schema = settings_form.schema()
        texts = section_texts or {}
        forms = [(path, shape, help_text, texts.get(path, settings_form.section_text(path)))
                 for path, shape, _model, help_text in settings_form.SECTIONS]
        today = clock().date().isoformat()  # the dated logs' new row, and which period a cap's total is
        entries = {path: settings_form.display_entries(path, today=today) for path in settings_form.CARD_SECTIONS}
        usage = cap_usage()
        for i, card in enumerate(entries.get("cards", [])):  # each cap row learns where its allowance stands
            for row in card.get("rate_rows", []):
                if "cap_index" in row:
                    row["usage"] = usage.get((i, row["cap_index"]))
            if "cap_index" in card.get("cap_all", {}):
                card["cap_all"]["usage"] = usage.get((i, card["cap_all"]["cap_index"]))
        return dict(
            rows=settings_form.view(rows_schema, os.environ),
            sections=settings_form.sections_in_order(rows_schema), section_forms=forms,
            hidden_envs=settings_form.hidden_envs(), in_container=in_container,
            open_section=open_section, config_path=str(settings_form.loader.CONFIG_FILE),
            section_title=settings_form.section_title, field_label=settings_form.field_label,
            entries=entries, today=today,
            retailer_keys=settings_form.RETAILER_KEYS, auth_retailers=settings_form.AUTH_RETAILERS,
            retailer_names=retailers_module.NAMES, default_rate_text=default_rate_text(),
            retailer_options=retailer_options(),
            profile_labels=settings_form.profile_labels(), card_choices=card_choices(),
        )

    def retailer_options() -> list[tuple[str, str]]:
        """(key, name) for every retailer a card's rates and caps may name: the scrapers' own,
        then every other Retailer on the ledger and every other one the cards already name."""
        known = dict(retailers_module.NAMES)
        extra: dict[str, str] = {}

        def add(name: str) -> None:
            key = retailers_module.key_of(name)
            if key and key not in known and key not in extra:
                extra[key] = retailers_module.name_of(name)

        try:
            for row in reader.load().rows:
                add(row.text("retailer"))
        except Exception:  # noqa: BLE001 -- no ledger yet
            pass
        for entry in settings_form.display_entries("cards"):
            for r, _rate in entry.get("retailer_rates", []):
                add(str(r))
        return list(known.items()) + sorted(extra.items(), key=lambda kv: kv[1])

    def default_rate_text() -> str:
        """The global default cashback rate as the page shows it ("2%"): what a card without a
        rate of its own earns."""
        rate = settings.default_cashback_rate or 0
        return f"{rate * 100:g}%"

    def settings_page(request: Request, *, message: str = "", errors: list[str] | None = None,
                      open_section: str = "", section_texts: dict | None = None, status: int = 200,
                      restart: str = ""):
        response = page_no_snapshot(
            request, "settings.html", message=message, errors=errors or [],
            restart=restart if restart in ("container", "dashboard") else "",
            **settings_context(open_section=open_section, section_texts=section_texts), **backup_context())
        response.status_code = status
        return response

    def settings_section(request: Request, path: str, *, message: str = "", errors: list[str] | None = None,
                         open_index=None, open_new: bool = False, status: int = 200):
        """One entry section, re-rendered for an htmx save / add / remove to swap in place."""
        context = settings_context(open_section=path if open_new else "")
        items = context["entries"][path]  # the decorated entries (a card's rows know their allowance)
        context["entries"] = {}  # the partial renders one section from `items`
        response = page_no_snapshot(
            request, "_settings_section.html", path=path, singular=path[:-1],
            items=items, message=message, section_errors=errors or [],
            open_index=open_index, errors=[], **context)
        response.status_code = status
        return response

    def settings_card(request: Request, path: str, index: int, old_key: str, *, message: str = "",
                      errors: list[str] | None = None, status: int = 200):
        """One entry's card, re-rendered for htmx to swap in place of that card alone (HX-Retarget
        by its key before the save), so the other cards keep their unsaved edits."""
        context = settings_context()
        items = context["entries"][path]
        context["entries"] = {}
        item = next((e for e in items if e["index"] == index), None)
        if item is None:  # gone, or a shape the page cannot place: the whole section
            return settings_section(request, path, message=message, errors=errors, open_index=index, status=status)
        children = []
        if path == "cards":
            children = sorted((e for e in items if e["virtual"] and e["virtual_of"] == item["last4"]), key=lambda e: e["own_bonus"])
        response = page_no_snapshot(
            request, "_settings_card.html", path=path, singular=path[:-1], item=item, children=children,
            message=message, card_errors=errors or [], open_index=index, errors=[], **context)
        response.status_code = status
        response.headers["HX-Retarget"] = f'#s-{path} details.entry-card[data-key="{old_key}"]'
        response.headers["HX-Reswap"] = "outerHTML"
        return response

    def settings_added_card(request: Request, path: str, index: int, *, message: str):
        """An in-place add's answer: the new card, inserted before the "+ Add" card (HX-Retarget /
        beforebegin), the count and a reset add form out of band."""
        context = settings_context()
        items = context["entries"][path]
        context["entries"] = {}
        item = next((e for e in items if e["index"] == index), None)
        response = page_no_snapshot(
            request, "_settings_card.html", path=path, singular=path[:-1], item=item, children=[],
            message=message, card_errors=[], open_index=index, errors=[], count=len(items), reset_add=True, **context)
        response.headers["HX-Retarget"] = f"#add-{path}"
        response.headers["HX-Reswap"] = "beforebegin"
        return response

    def settings_removed_card(request: Request, path: str, key: str, *, message: str, count: int):
        """An in-place removal's answer when the card can simply go (HX-Reswap delete): the count and the toast."""
        context = settings_context()
        context["entries"] = {}
        response = page_no_snapshot(
            request, "_settings_card.html", path=path, singular=path[:-1], item=None, children=[],
            message=message, card_errors=[], errors=[], count=count, **context)
        response.headers["HX-Retarget"] = f'#s-{path} details.entry-card[data-key="{key}"]'
        response.headers["HX-Reswap"] = "delete"
        return response

    def entry_key_at(path: str, index: int | None) -> str:
        entries = settings_form.display_entries(path)
        return settings_form.entry_key(path, entries[index]) if index is not None and 0 <= index < len(entries) else ""

    def moved_in_tree(path: str, before: dict, after: dict) -> bool:
        """Did the save change where the card sits: its key, or (a card) the card it belongs to?"""
        if settings_form.entry_key(path, before) != settings_form.entry_key(path, after):
            return True
        return path == "cards" and (bool(before.get("virtual_of")) != bool(after.get("virtual_of"))
                                    or before.get("virtual_of") != after.get("virtual_of"))

    def wants_fragment(request: Request) -> bool:
        return request.headers.get("HX-Request", "").lower() == "true"

    @app.get("/settings", response_class=HTMLResponse)
    def settings_get(request: Request):
        return settings_page(request, message=request.query_params.get("message", ""),
                             restart=request.query_params.get("restart", ""))

    def settings_scalar_swap(request: Request, *, message: str = "", errors: list[str] | None = None,
                             restart: str = "", status: int = 200):
        """An in-place scalar save's answer: the form, the save bar, the restart
        banner and the toast."""
        response = page_no_snapshot(
            request, "_settings_scalar_swap.html", message=message, errors=errors or [],
            restart=restart if restart in ("container", "dashboard") else "",
            **settings_context(), **backup_context())
        response.status_code = status
        return response

    @app.post("/settings", response_class=HTMLResponse)
    async def settings_save(request: Request):
        form = await request.form()
        in_place = request.headers.get("HX-Request", "").lower() == "true"
        try:
            changes = settings_form.apply_scalars(form, skip=settings_form.hidden_envs())
        except settings_form.SettingsError as exc:
            if in_place:
                return settings_scalar_swap(request, errors=exc.errors, status=400)
            return settings_page(request, errors=exc.errors, status=400)
        message = (f"Saved {len(changes)} changed setting(s): {', '.join(sorted(changes))}"
                   if changes else "Saved; nothing had changed.")
        if changes:
            act("settings", f"Settings saved: {', '.join(sorted(changes))}",
                {"changed": sorted(changes)})  # paths only: a value here may be a secret
        restart_kind = settings_form.restart_needed(changes)
        if in_place:
            return settings_scalar_swap(request, message=message, restart=restart_kind)
        url = f"/settings?message={message.replace(' ', '+')}"
        if restart_kind:
            url += f"&restart={restart_kind}"
        return RedirectResponse(url=url, status_code=303)

    @app.post("/settings/section/{path}", response_class=HTMLResponse)
    async def settings_save_section(request: Request, path: str):
        form = await request.form()
        text = str(form.get("text", ""))
        try:
            count = settings_form.apply_section(path, text)
        except settings_form.SettingsError as exc:
            return settings_page(request, errors=exc.errors, open_section=path,
                                 section_texts={path: text}, status=400)
        message = f"Saved {path} ({count} entr{'y' if count == 1 else 'ies'})."
        act("settings", f"Settings: {path} replaced as JSON ({count} entries)", {"path": path})
        return RedirectResponse(url=f"/settings?message={message.replace(' ', '+')}", status_code=303)

    # One entry of a card section: add, replace, delete. Errors re-render the page with the section open and nothing written.
    async def _save_entry(request: Request, path: str, index: int | None):
        form = await request.form()
        entries_before = settings_form.display_entries(path)
        before = entries_before[index] if index is not None else None
        old_key = settings_form.entry_key(path, before) if before else ""
        try:
            landed, label = settings_form.apply_entry(path, index, form, today=clock().date().isoformat())
        except settings_form.SettingsError as exc:
            if wants_fragment(request) and before is not None:  # the one card, its errors inside; the others untouched
                return settings_card(request, path, index, old_key, errors=exc.errors, status=400)
            if wants_fragment(request):
                return settings_section(request, path, errors=exc.errors, open_index=index, open_new=index is None, status=400)
            return settings_page(request, errors=exc.errors, open_section=path, status=400)
        verb = "Added" if index is None else "Saved"
        message = f"{verb} {path[:-1]} {label}."
        act("settings", f"Settings: {verb.lower()} {path[:-1]} {label}", {"path": path, "entry": label})
        if wants_fragment(request):  # in place; the saved entry stays open
            after = next((e for e in settings_form.display_entries(path) if e["index"] == landed), None)
            if before is not None and after is not None and not moved_in_tree(path, before, after):
                return settings_card(request, path, landed, old_key, message=message)  # that card alone
            nested = path == "cards" and after is not None and after.get("virtual_of") in {e.get("last4") for e in entries_before}
            if before is None and entries_before and after is not None and not nested:
                return settings_added_card(request, path, landed, message=message)  # the new card, in place
            return settings_section(request, path, message=message, open_index=landed)  # the first, nested, or moved: the section
        return RedirectResponse(url=f"/settings?message={message.replace(' ', '+')}#s-{path}",
                                status_code=303)

    @app.post("/settings/section/{path}/entry", response_class=HTMLResponse)
    async def settings_add_entry(request: Request, path: str):
        return await _save_entry(request, path, None)

    @app.post("/settings/section/{path}/entry/{index}", response_class=HTMLResponse)
    async def settings_save_entry(request: Request, path: str, index: int):
        return await _save_entry(request, path, index)

    @app.post("/settings/section/profiles/entry/{index}/proxy", response_class=HTMLResponse)
    async def settings_toggle_proxy(request: Request, index: int):
        """The proxy switch on a profile's summary line: flips `proxy.enabled`
        and swaps the section in place."""
        try:
            label, on = settings_form.toggle_proxy(index)
        except settings_form.SettingsError as exc:
            if wants_fragment(request):
                return settings_section(request, "profiles", errors=exc.errors, status=400)
            return settings_page(request, errors=exc.errors, open_section="profiles", status=400)
        message = f"Proxy {'on' if on else 'off'} for profile {label}."
        act("settings", f"Settings: proxy {'on' if on else 'off'} for profile {label}", {"path": "profiles", "entry": label})
        if wants_fragment(request):  # that profile's card alone: the others keep their edits
            return settings_card(request, "profiles", index, f"profiles:{label}", message=message)
        return RedirectResponse(url=f"/settings?message={message.replace(' ', '+')}#s-profiles", status_code=303)

    @app.post("/settings/section/{path}/entry/{index}/delete", response_class=HTMLResponse)
    async def settings_delete_entry(request: Request, path: str, index: int):
        entries_before = settings_form.display_entries(path)
        gone = entries_before[index] if 0 <= index < len(entries_before) else None
        try:
            label = settings_form.delete_entry(path, index)
        except settings_form.SettingsError as exc:
            if wants_fragment(request):
                return settings_section(request, path, errors=exc.errors, status=400)
            return settings_page(request, errors=exc.errors, open_section=path, status=400)
        message = f"Removed {path[:-1]} {label}."
        act("settings", f"Settings: removed {path[:-1]} {label}", {"path": path, "entry": label})
        if wants_fragment(request):
            # the card alone can go when nothing else shifts: it was the last entry (the others' form
            # indexes stay), it leaves some behind, and (a card) nothing was nested under it
            had_children = path == "cards" and gone is not None and any(e.get("virtual_of") == gone.get("last4") for e in entries_before)
            if gone is not None and index == len(entries_before) - 1 and len(entries_before) > 1 and not had_children:
                return settings_removed_card(request, path, settings_form.entry_key(path, gone), message=message, count=len(entries_before) - 1)
            return settings_section(request, path, message=message)
        return RedirectResponse(url=f"/settings?message={message.replace(' ', '+')}#s-{path}",
                                status_code=303)

    @app.post("/settings/restart-container", response_class=HTMLResponse)
    def settings_restart_container(request: Request):
        """Restart the whole container (see _signal_container). Refused while a run is in
        progress, with the same lock the writers honour."""
        from web.ledger_writer import run_in_progress

        if not in_container:
            return settings_page(request, errors=["Not running in a container: restart the process "
                                                  "yourself on this machine."], status=400)
        if run_in_progress(logs_dir):
            return settings_page(request, errors=["A scheduled run is in progress (logs/.run.lock); "
                                                  "restarting now would abort it. Try again when the "
                                                  "heartbeat shows it finished."], status=423)
        act("settings", "Container restart requested from the Settings page")
        restart_container()
        return page(request, "restarting.html", what="container", seconds=90, grace=8,
                    note="The scheduler and the dashboard come back in about half a minute; "
                         "config.json is re-read on the way up.")

    @app.post("/settings/restart", response_class=HTMLResponse)
    def settings_restart(request: Request):
        act("settings", "Dashboard restart requested from the Settings page")
        restart()
        return page(request, "restarting.html", what="dashboard", seconds=20, grace=2,
                    note="Back in a few seconds." if in_container
                    else "Back once you run python -m web again on this machine.")

    @app.get("/health")
    def health(request: Request):
        info = reader.health()
        rows = None
        schema = None
        error = None
        try:
            snapshot = reader.load()
            rows, schema = len(snapshot.rows), snapshot.schema_matches
            info["source"] = snapshot.source
            if not schema:
                info["missing_columns"] = list(snapshot.missing_columns)
                info["extra_columns"] = list(snapshot.extra_columns)
            info.update(reader.health())  # the cache state AFTER the load above
        except Exception as exc:  # noqa: BLE001 -- /health must answer, and say what is wrong
            error = f"{type(exc).__name__}: {exc}"
        body = {
            "ok": error is None,
            "read_only": True,
            "password_protected": auth_on,
            "rows": rows,
            "schema_matches": schema,
            "heartbeat": heartbeat(),
            **info,
        }
        if error:
            body["error"] = error
        return JSONResponse(body, status_code=200 if error is None else 503)

    return app


def _default_app() -> FastAPI:
    return create_app()


class _LazyApp:
    """`uvicorn web.app:app` without building the reader at import time (importing this module in
    a test must not resolve a snapshot or open a ledger)."""

    _app: FastAPI | None = None

    async def __call__(self, scope, receive, send):
        if self._app is None:
            self._app = _default_app()
        await self._app(scope, receive, send)


app = _LazyApp()

__all__ = ["app", "create_app", "money", "percent", "cell", "ROUTES"]
