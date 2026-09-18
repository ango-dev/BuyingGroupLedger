"""The FastAPI application: five GET routes over one read-only reader, plus the Backup page.

The ledger routes are GET-only. The two POSTs on /backup write LOCAL files only (a zip under
backups/, or a restore into a fresh clone); nothing here can write the Sheet or call a third party.

`create_app()` is a factory so tests can hand it a SnapshotReader over a temporary CSV, a temporary
logs/ and failures/ directory, and a fixed clock. The module-level `app` is what `uvicorn web.app:app`
or `python -m web` serves, built from config.json's `web` section.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

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


def _exit_soon(delay: float = 0.5) -> None:
    """Leave the process shortly after the current response is sent. In the container,
    docker/entrypoint.sh's loop starts the dashboard again within seconds, with config.json
    re-read; on a desktop the operator runs `python -m web` again."""
    import threading

    threading.Timer(delay, lambda: os._exit(0)).start()


def create_app(reader: LedgerReader | None = None, *, settings=None,
               logs_dir: Path | None = None, failures_dir: Path | None = None,
               backup_dir: Path | None = None, repo_root_dir: Path | None = None,
               clock: Callable[[], datetime] | None = None,
               restarter: Callable[[], None] | None = None,
               writer=None) -> FastAPI:
    """`writer` is the ONE sheet-write path (web/ledger_writer.SheetCellWriter): cell edits on
    the Orders page. None = the page is view-only (the snapshot backend, or a test)."""
    restart = restarter or _exit_soon
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

    # The sheet writer: cell edits on the Orders page. The snapshot backend is a CSV, so there is
    # nothing to write to and the page stays view-only there.
    if writer is None and reader.backend != "snapshot":
        from web.ledger_writer import SheetCellWriter

        writer = SheetCellWriter(logs_dir=logs_dir)
    app.state.writer = writer
    import json

    from web.ledger_writer import EDITABLE_FIELDS, EditError
    from web.queries import LINK_FIELDS, MONEY_FIELDS

    templates.env.globals["EDITABLE_FIELDS"] = EDITABLE_FIELDS
    templates.env.globals["LINK_FIELDS"] = LINK_FIELDS
    templates.env.globals["MONEY_FIELDS"] = MONEY_FIELDS
    templates.env.globals["EDIT_FIELD_HEADINGS"] = [(f, FIELD_TO_HEADER[f]) for f in EDITABLE_FIELDS]

    from web.queries import PER_PAGE_CHOICES, order_cards, paginate

    templates.env.globals["PER_PAGE_CHOICES"] = PER_PAGE_CHOICES

    def orders_context(request: Request, params=None, **extra) -> dict:
        snapshot = load(request)
        filters = Filters.from_query(params if params is not None else request.query_params)
        rows = sort_rows(filter_rows(snapshot.rows, filters), filters)
        context = {
            "snapshot": snapshot, "filters": filters, "rows": rows, "total": len(snapshot.rows),
            "facets": facets(snapshot.rows), "columns": column_headings(),
            "editable": writer is not None, "wide": True, **extra,
        }
        if filters.view == "cards":
            context["pager"] = paginate(order_cards(rows), filters.per, filters.page)
        return context

    def partial_for(filters: Filters) -> str:
        return "_orders_cards.html" if filters.view == "cards" else "_orders_table.html"

    @app.get("/orders", response_class=HTMLResponse)
    def orders(request: Request):
        context = orders_context(request, notice=request.query_params.get("notice", ""))
        # htmx asks for just the table / cards; a plain browser request gets the whole page.
        if request.headers.get("HX-Request", "").lower() == "true":
            return page(request, partial_for(context["filters"]), **context)
        return page(request, "orders.html", **context)

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

    @app.post("/orders/bulk", response_class=HTMLResponse)
    async def orders_bulk(request: Request):
        """Set one field to one value on every selected row."""
        form = await request.form()
        keys = selected_keys(form)
        field, value = str(form.get("field", "")), str(form.get("value", ""))
        if writer is None:
            return table_after(request, form, error="editing is off: this backend is a CSV snapshot")
        try:
            result = writer.write_cells(keys, field, value)
        except EditError as exc:
            return table_after(request, form, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return table_after(request, form, error=f"{type(exc).__name__}: {exc}")
        reader.load(force=True)
        notice = (f"Set {FIELD_TO_HEADER.get(field, field)} on {result['written']} row(s)"
                  + (f"; {len(result['errors'])} skipped: " + "; ".join(result["errors"])
                     if result["errors"] else ""))
        return table_after(request, form, notice=notice)

    @app.post("/orders/delete", response_class=HTMLResponse)
    async def orders_delete(request: Request):
        """Delete every selected row (the page confirms first)."""
        form = await request.form()
        keys = selected_keys(form)
        if writer is None:
            return table_after(request, form, error="editing is off: this backend is a CSV snapshot")
        try:
            result = writer.remove_rows(keys)
        except EditError as exc:
            return table_after(request, form, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return table_after(request, form, error=f"{type(exc).__name__}: {exc}")
        reader.load(force=True)
        return table_after(request, form, notice=f"Deleted {result['deleted']} row(s)")

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
        is stored first and its PAR link becomes the row's Receipt Link."""
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
        notice = f"Added {result['key']['order_id']} at sheet row {result['row_number']}"
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
        error = ""
        if writer is None:
            error = "editing is off: this backend is a CSV snapshot"
        elif field not in EDITABLE_FIELDS:
            error = f"{field} is not editable"
        else:
            try:
                writer.write_cell(key, field, value,
                                  None if expected is None else str(expected))
            except EditError as exc:
                error = str(exc)
            except Exception as exc:  # noqa: BLE001 -- the page must show why, not a 500
                error = f"{type(exc).__name__}: {exc}"
        snapshot = reader.load(force=not error)
        row = next((r for r in snapshot.rows
                    if r.order_id == key["order_id"] and r.order_date == key["order_date"]
                    and r.item_name == key["item_name"] and r.shipment == key["shipment"]), None)
        return page(request, "_cell.html", snapshot=snapshot, row=row, col=field, key=key,
                    editable=writer is not None, error=error)

    @app.get("/orders/{order_id}", response_class=HTMLResponse)
    def order(request: Request, order_id: str):
        snapshot = load(request)
        view = order_view(snapshot.by_order(order_id))
        if view is None:
            raise HTTPException(status_code=404, detail=f"No ledger row carries Order ID {order_id!r}")
        return page(request, "order.html", snapshot=snapshot, order=view,
                    editable=writer is not None,
                    notice=request.query_params.get("notice", ""),
                    error=request.query_params.get("error", ""))

    @app.post("/orders/{order_id}/receipt")
    async def order_receipt(request: Request, order_id: str):
        """Upload a receipt for an EXISTING order: store it, then write the link into Receipt Link
        on every row of that order (the link is per order -- docs/data-model.md)."""
        form = await request.form()
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
        except (receipts_upload.UploadError, EditError) as exc:
            return RedirectResponse(url=f"/orders/{order_id}?" + urlencode({"error": str(exc)}),
                                    status_code=303)
        reader.load(force=True)
        notice = f"Receipt stored and linked on {result['written']} row(s)"
        return RedirectResponse(url=f"/orders/{order_id}?" + urlencode({"notice": notice}),
                                status_code=303)

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

    # --- settings (edits config.json in place; never the Sheet) ---------------------------------
    from web import settings_form

    def settings_page(request: Request, *, message: str = "", errors: list[str] | None = None,
                      open_section: str = "", section_texts: dict | None = None, status: int = 200,
                      restart: str = ""):
        rows_schema = settings_form.schema()
        texts = section_texts or {}
        forms = [(path, shape, help_text, texts.get(path, settings_form.section_text(path)))
                 for path, shape, _model, help_text in settings_form.SECTIONS]
        response = page_no_snapshot(
            request, "settings.html", message=message, errors=errors or [],
            rows=settings_form.view(rows_schema, os.environ),
            sections=settings_form.sections_in_order(rows_schema), section_forms=forms,
            open_section=open_section, config_path=str(settings_form.loader.CONFIG_FILE),
            restart=restart if restart in ("container", "dashboard") else "")
        response.status_code = status
        return response

    @app.get("/settings", response_class=HTMLResponse)
    def settings_get(request: Request):
        return settings_page(request, message=request.query_params.get("message", ""),
                             restart=request.query_params.get("restart", ""))

    @app.post("/settings", response_class=HTMLResponse)
    async def settings_save(request: Request):
        form = await request.form()
        try:
            changes = settings_form.apply_scalars(form)
        except settings_form.SettingsError as exc:
            return settings_page(request, errors=exc.errors, status=400)
        message = (f"Saved {len(changes)} changed setting(s): {', '.join(sorted(changes))}"
                   if changes else "Saved; nothing had changed.")
        restart_kind = settings_form.restart_needed(changes)
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
        return RedirectResponse(url=f"/settings?message={message.replace(' ', '+')}", status_code=303)

    @app.post("/settings/restart", response_class=HTMLResponse)
    def settings_restart(request: Request):
        restart()
        return HTMLResponse("<!doctype html><meta http-equiv='refresh' content='6;url=/settings'>"
                            "<p style='font-family:system-ui;padding:20px'>Restarting the dashboard; "
                            "this page reloads in a few seconds. On a desktop, run "
                            "<code>python -m web</code> again.</p>")

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
