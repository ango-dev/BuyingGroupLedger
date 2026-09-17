"""The FastAPI application: five GET routes over one read-only reader. No other HTTP method exists.

`create_app()` is a factory so tests can hand it a SnapshotReader over a temporary CSV, a temporary
logs/ and failures/ directory, and a fixed clock. The module-level `app` is what `uvicorn web.app:app`
or `python -m web` serves, built from config.json's `web` section.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

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
            if reader.backend == "sheet":
                info.update(reader.health())  # the cache age AFTER the load above
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
