"""The FastAPI application: five GET routes over one read-only reader, plus the Backup page.

The ledger routes are GET-only. The two POSTs on /backup write LOCAL files only (a zip under
backups/, or a restore into a fresh clone); nothing here can write the Sheet or call a third party.

`create_app()` is a factory so tests can hand it a SnapshotReader over a temporary CSV, a temporary
logs/ and failures/ directory, and a fixed clock. The module-level `app` is what `uvicorn web.app:app`
or `python -m web` serves, built from config.json's `web` section.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from scripts import backup as backup_module

from web import failures as failures_module
from web import heartbeat as heartbeat_module
from web.ledger_reader import FIELD_TO_HEADER, LedgerReader, Snapshot, reader_from_settings
from web.queries import Filters, column_headings, facets, filter_rows, order_view, sort_rows
from web.summary import overview

HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = HERE / "templates"
STATIC_DIR = HERE / "static"

#: The routes, for the read-only test that asserts every one of them refuses any non-GET method.
ROUTES = ("/", "/orders", "/orders/{order_id}", "/failures", "/health")


# --------------------------------------------------------------------------------------------------
# Template filters
# --------------------------------------------------------------------------------------------------


def money(value) -> str:
    """$1,234.56; negatives as -$12.00; blank for None / non-numbers. The sheet's own formatting,
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
    if name in ("total_cost", "insurance", "payout_amount", "cost_per_item", "shipping",
                "sales_tax", "gift_card", "rewards_used"):
        return money(row.number(name))
    if name == "cashback_rate":
        return percent(row.number(name))
    if name == "tracking_submitted":
        return "✓" if row.tracking_submitted else ""
    return row.text(name)


# --------------------------------------------------------------------------------------------------
# App factory
# --------------------------------------------------------------------------------------------------


def create_app(reader: LedgerReader | None = None, *, settings=None,
               logs_dir: Path | None = None, failures_dir: Path | None = None,
               backup_dir: Path | None = None, repo_root_dir: Path | None = None,
               clock: Callable[[], datetime] | None = None) -> FastAPI:
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

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["money"] = money
    templates.env.filters["percent"] = percent
    templates.env.filters["cell"] = cell
    templates.env.globals["header_of"] = FIELD_TO_HEADER.get

    def heartbeat() -> dict:
        return heartbeat_module.read_heartbeat(logs_dir, now=clock(),
                                              interval_hours=interval_hours)

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

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        snapshot = load(request)
        return page(request, "overview.html", snapshot=snapshot, summary=overview(snapshot))

    @app.get("/orders", response_class=HTMLResponse)
    def orders(request: Request):
        snapshot = load(request)
        filters = Filters.from_query(request.query_params)
        rows = sort_rows(filter_rows(snapshot.rows, filters), filters)
        context = {
            "snapshot": snapshot, "filters": filters, "rows": rows, "total": len(snapshot.rows),
            "facets": facets(snapshot.rows), "columns": column_headings(),
        }
        # htmx asks for just the table; a plain browser request gets the whole page.
        if request.headers.get("HX-Request", "").lower() == "true":
            return page(request, "_orders_table.html", **context)
        return page(request, "orders.html", **context)

    @app.get("/orders/{order_id}", response_class=HTMLResponse)
    def order(request: Request, order_id: str):
        snapshot = load(request)
        view = order_view(snapshot.by_order(order_id))
        if view is None:
            raise HTTPException(status_code=404, detail=f"No ledger row carries Order ID {order_id!r}")
        return page(request, "order.html", snapshot=snapshot, order=view)

    @app.get("/failures", response_class=HTMLResponse)
    def failures(request: Request):
        dossiers = failures_module.list_dossiers(failures_dir)
        return page(request, "failures.html", dossiers=dossiers, failures_dir=str(failures_dir))

    # --- backup / restore (local files only; the Sheet is never touched) -------------------------
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
                "fresh_install": fresh, "backups_dir": str(backups_dir), **extra}

    def page_no_snapshot(request: Request, name: str, **context):
        base = {"request": request, "backend": reader.backend, "source": "", "loaded_at": None,
                "schema_matches": True, "heartbeat": heartbeat()}
        return templates.TemplateResponse(request=request, name=name, context={**base, **context})

    @app.get("/backup", response_class=HTMLResponse)
    def backup_page(request: Request):
        return page_no_snapshot(request, "backup.html", **backup_context(
            message=request.query_params.get("message", "")))

    @app.post("/backup")
    def backup_create(request: Request):
        target = backup_module.create_backup(repo_root, backups_dir)
        return RedirectResponse(url=f"/backup?message=Wrote+{target.name}", status_code=303)

    @app.get("/backup/{name}")
    def backup_download(name: str):
        if not name.startswith(backup_module.PREFIX) or not name.endswith(".zip") or "/" in name \
                or "\\" in name or ".." in name:
            raise HTTPException(status_code=404)
        path = backups_dir / name
        if not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(str(path), media_type="application/zip", filename=name)

    @app.post("/backup/restore")
    async def backup_restore(request: Request, archive: UploadFile = File(...),
                             force: str = Form("")):
        # Only while the clone is FRESH: an unauthenticated page must not be able to replace a live
        # host's config.json. On a configured host, restore with `python -m scripts.backup
        # --restore` on the machine itself.
        if (repo_root / "config.json").is_file():
            raise HTTPException(status_code=409, detail="config.json exists; restore from the "
                                "command line on this host (python -m scripts.backup --restore).")
        backups_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(archive.filename or "upload.zip").name
        if not safe_name.endswith(".zip"):
            safe_name += ".zip"
        saved = backups_dir / f"uploaded_{safe_name}"
        saved.write_bytes(await archive.read())
        result = backup_module.restore_backup(saved, repo_root, force=bool(force))
        message = (f"Restored {len(result['restored'])} file(s), kept {len(result['skipped_existing'])}"
                   " existing. Restart the app so the restored config.json is read.")
        return RedirectResponse(url=f"/backup?message={message.replace(' ', '+')}", status_code=303)

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
            info.update(reader.health())  # the cache / mirror state AFTER the load above
        except Exception as exc:  # noqa: BLE001 -- /health must answer, and say what is wrong
            error = f"{type(exc).__name__}: {exc}"
        body = {
            "ok": error is None,
            "read_only": True,
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
    a test must not resolve a snapshot or open a sheet)."""

    _app: FastAPI | None = None

    async def __call__(self, scope, receive, send):
        if self._app is None:
            self._app = _default_app()
        await self._app(scope, receive, send)


app = _LazyApp()

__all__ = ["app", "create_app", "money", "percent", "cell", "ROUTES"]
