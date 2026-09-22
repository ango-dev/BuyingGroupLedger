"""Tools > Import: a CSV of order history mapped onto the ledger's columns, with a STAGING sheet
for the rows that are not complete yet.

THE RULE FOR WHAT LANDS. A row goes into the ledger only when it carries everything the audit's
`mandatory_by_stage` check would ask of it -- the same function (`scripts.audit_ledger.mandatory_gaps`)
decides both, so the two can never drift; COGS and Total Profit are formulas the ledger computes
and are never asked of the user, and Total Cost is derived from Quantity x Cost Per Item. Rows
with a gap, rows whose status is still open (the scrapers own open orders), and rows that look
like an order the ledger already holds under another key are STAGED: written to
`data/imports/<batch>/staging.json`, shown on the Import page as an editable grid (the Orders
page's own sheet machinery, static/edit.js) with the missing cells highlighted, and imported once
complete. An exact-key duplicate (Order ID + Order Date + Item Name + Shipment) is skipped and
reported. The directory is under data/, so every backup carries it, and the page finds it again on
start.

THE WRITE PATH is `web.ledger_writer.LedgerCellWriter.add_row`, one row at a time: it refuses while a
scheduled run holds the lock, refuses a duplicate key, stamps the formula cells and records every
cell as a hand edit -- the imported data is the user's and a later scrape leaves it alone.

`scripts/import_history.py` (the command line) keeps its own, stricter normaliser; this module reuses
its cell helpers only. Where that one invents a value (a blank status becomes delivered, a blank
quantity 1, a missing order id is synthesised) this one leaves the cell blank: blanks are what the
staging sheet is for.
"""

from __future__ import annotations

import csv
import io
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping

from ledger.sync import HEADER, _parse_display_number
from models.order import FIELDNAMES, normalize_shipment
from scripts import import_history as ih
from web.ledger_writer import EditError, RunInProgress, valid_iso_date, validate

__all__ = ["BATCHES_DIR", "Batch", "ImportResult", "StagedRow", "Staging", "StagingError"]

BATCHES_DIR = "imports"
#: Never asked of the user: the ledger computes them.
IMPORT_EXEMPT = ("COGS", "Total Profit")
FORMULA_FIELDS = ("cogs", "total_profit", "last_scraped_at")
#: Every cell a staged row carries (and the staging grid shows), in ledger order.
STAGING_FIELDS = tuple(f for f in FIELDNAMES if f not in FORMULA_FIELDS)
#: Editable on the staging sheet though the ledger never lets them change: the row is not on the
#: ledger yet, so its key is still the user's to fix.
KEY_EDITABLE = ("order_id", "order_date", "item_name", "shipment")
DERIVED = ("total_cost",)
OPEN_STATUSES = ("ordered", "shipped")
#: Order-level amounts the sync prorates across an order's rows by Total Cost.
ORDER_LEVEL = ("shipping", "sales_tax", "gift_card", "rewards_used")
DATE_FIELDS = ("order_date", "delivery_date", "payout_date", "return_date")
MONEY_FIELDS = ("cost_per_item", "shipping", "sales_tax", "gift_card", "rewards_used", "insurance",
                "payout_amount", "expected_payout")
INT_FIELDS = ("quantity", "return_quantity")
#: The cells a box row of a split cannot carry until each box's Quantity is known: the order's
#: figures would otherwise land whole on EVERY box.
PER_BOX = ("quantity", "total_cost", "insurance", "payout_amount", "expected_payout")
_DISPLAY = dict(zip(FIELDNAMES, HEADER))
_FIELD_OF = {h: f for f, h in zip(FIELDNAMES, HEADER)}
_FIELD_OF_NORM = {ih.norm(h): f for f, h in zip(FIELDNAMES, HEADER) if f in STAGING_FIELDS}
_MAX_SOURCE_BYTES = 25 * 1024 * 1024


class StagingError(ValueError):
    """A refused action; the message is the page's."""


# --------------------------------------------------------------------------------------------------
# The store: data/imports/<stamp>/{source.csv, mapping.json, staging.json}
# --------------------------------------------------------------------------------------------------


@dataclass
class StagedRow:
    id: str
    source_row: int
    cells: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    status: str = "staged"  # staged | imported | duplicate
    imported_at: str | None = None
    ledger_row: int | None = None
    note: str = ""
    #: "Import anyway" (user-set on the sheet): the open-order and near-duplicate holds no longer
    #: apply to this row; it lands as soon as it is complete. An exact-key duplicate stays refused.
    accepted: bool = False

    def to_json(self) -> dict:
        return {"id": self.id, "source_row": self.source_row, "cells": dict(self.cells),
                "warnings": list(self.warnings), "status": self.status, "imported_at": self.imported_at,
                "ledger_row": self.ledger_row, "note": self.note, "accepted": self.accepted}

    @classmethod
    def from_json(cls, payload: Mapping) -> "StagedRow":
        return cls(id=str(payload.get("id", "")), source_row=int(payload.get("source_row", 0) or 0),
                   cells={str(k): str(v if v is not None else "") for k, v in (payload.get("cells") or {}).items()},
                   warnings=[str(w) for w in (payload.get("warnings") or [])],
                   status=str(payload.get("status") or "staged"), imported_at=payload.get("imported_at"),
                   ledger_row=payload.get("ledger_row"), note=str(payload.get("note") or ""),
                   accepted=bool(payload.get("accepted", False)))


@dataclass
class Staging:
    id: str
    created_at: str
    source_name: str
    date_order: str
    rows: list = field(default_factory=list)

    def row(self, row_id: str) -> StagedRow:
        for r in self.rows:
            if r.id == row_id:
                return r
        raise KeyError(row_id)

    @property
    def staged(self) -> list:
        return [r for r in self.rows if r.status == "staged"]

    def to_json(self) -> dict:
        return {"version": 1, "id": self.id, "created_at": self.created_at, "source_name": self.source_name,
                "date_order": self.date_order, "rows": [r.to_json() for r in self.rows]}

    @classmethod
    def from_json(cls, payload: Mapping) -> "Staging":
        return cls(id=str(payload.get("id", "")), created_at=str(payload.get("created_at", "")),
                   source_name=str(payload.get("source_name", "")), date_order=str(payload.get("date_order", "mdy")),
                   rows=[StagedRow.from_json(r) for r in (payload.get("rows") or [])])


@dataclass
class Batch:
    dir: Path

    @property
    def id(self) -> str:
        return self.dir.name

    @property
    def source_path(self) -> Path:
        return self.dir / "source.csv"

    @property
    def mapping_path(self) -> Path:
        return self.dir / "mapping.json"

    @property
    def staging_path(self) -> Path:
        return self.dir / "staging.json"

    @property
    def source_name(self) -> str:
        mapping = self.load_mapping()
        return str((mapping or {}).get("source_name") or "source.csv")

    def load_mapping(self) -> dict | None:
        return _read_json(self.mapping_path)

    def save_mapping(self, mapping: dict[str, str], *, date_order: str, profile: str, source_name: str = "") -> None:
        current = self.load_mapping() or {}
        _write_json(self.mapping_path, {"mapping": dict(mapping), "date_order": date_order, "profile": profile,
                                        "source_name": source_name or current.get("source_name", "")})

    def load_staging(self) -> Staging | None:
        payload = _read_json(self.staging_path)
        return Staging.from_json(payload) if payload else None

    def save_staging(self, staging: Staging) -> None:
        _write_json(self.staging_path, staging.to_json())

    def discard(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, payload: dict) -> None:
    """Atomic: a crash mid-write never leaves a half sheet."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def batches(data_dir: Path) -> list[Batch]:
    root = Path(data_dir) / BATCHES_DIR
    if not root.is_dir():
        return []
    return [Batch(p) for p in sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True)]


def live_batch(data_dir: Path) -> Batch | None:
    """The batch the page is about: the newest with staged rows left, or an upload that never got
    past mapping / preview. None = nothing in progress."""
    for batch in batches(data_dir):
        staging = batch.load_staging()
        if staging is not None:
            if staging.staged:
                return batch
            continue
        if batch.source_path.is_file():
            return batch
    return None


_count_cache: dict[str, tuple[float, int]] = {}


def staged_count(data_dir: Path) -> int:
    """The nav badge: staged rows of the live batch. Cached on the sheet's mtime."""
    batch = live_batch(data_dir)
    if batch is None or not batch.staging_path.is_file():
        return 0
    try:
        stamp = batch.staging_path.stat().st_mtime
    except OSError:
        return 0
    cached = _count_cache.get(str(batch.staging_path))
    if cached and cached[0] == stamp:
        return cached[1]
    staging = batch.load_staging()
    count = len(staging.staged) if staging else 0
    _count_cache[str(batch.staging_path)] = (stamp, count)
    return count


def new_batch(data_dir: Path, filename: str, payload: bytes, *, clock: Callable[[], datetime]) -> Batch:
    """Keep an uploaded CSV as a new batch. Refused while another batch is live (one staging sheet
    at a time), for a file that is not UTF-8 text, or one with no header row."""
    if live_batch(data_dir) is not None:
        raise StagingError("an import is already in progress: finish or discard it first (Tools -> Import)")
    if len(payload) > _MAX_SOURCE_BYTES:
        raise StagingError("that file is over 25 MB; split it")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise StagingError("the file is not UTF-8 text: save it as CSV UTF-8 and upload again") from exc
    try:
        headers = next(csv.reader(io.StringIO(text)), [])
        # Walk the whole file once here, so a cell the csv module refuses (over its 128 KB field
        # limit) is a message at upload rather than a 500 on every later screen.
        for _row in csv.reader(io.StringIO(text)):
            pass
    except csv.Error as exc:
        raise StagingError(f"the file could not be read as CSV ({exc}): a cell is probably far too long") from exc
    if not any(str(h).strip() for h in headers):
        raise StagingError("the file has no header row")
    # Two columns under one name: DictReader keeps the LAST, and the mapping is keyed by header
    # text, so one of the two would silently read the other's column.
    seen_headers: dict[str, int] = {}
    for i, h in enumerate(headers, start=1):
        name = str(h).strip()
        if name and name in seen_headers:
            raise StagingError(f"two columns are both named {name!r} (columns {seen_headers[name]} and {i}): "
                               f"rename one and upload again")
        seen_headers[name] = i
    stamp = clock().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    batch = Batch(Path(data_dir) / BATCHES_DIR / stamp)
    batch.dir.mkdir(parents=True, exist_ok=True)
    batch.source_path.write_text(text, encoding="utf-8", newline="")
    batch.save_mapping({}, date_order="", profile="", source_name=Path(filename or "upload.csv").name)
    return batch


def read_source(batch: Batch) -> tuple[list[str], list[dict]]:
    return ih.read_rows(batch.source_path)


def template_csv() -> str:
    """The ledger's columns as a CSV header, for the user who would rather fill the template."""
    return ",".join(HEADER) + "\n"


# --------------------------------------------------------------------------------------------------
# Mapping
# --------------------------------------------------------------------------------------------------


def target_options() -> list[tuple[str, str]]:
    return [("", "— ignore —")] + [(f, _DISPLAY[f]) for f in STAGING_FIELDS] + [("source_profit", "Profit (checked, not imported)")]


def source_columns(batch: Batch, overrides: Mapping[str, str] | None = None) -> list[dict]:
    """One entry per source column: its header, three sample values and the ledger column the
    import would take it for (scripts.import_history's aliases; blank = ignored)."""
    headers, rows = read_source(batch)
    # A column named exactly as the ledger names it wins its column outright, before the looser
    # aliases are tried in file order ("Paid" would otherwise take Payout Date from "Payout Date").
    exact = {h: _FIELD_OF_NORM[ih.norm(h)] for h in headers if ih.norm(h) in _FIELD_OF_NORM}
    try:
        suggested, _unmapped = ih.map_headers(headers, {**exact, **dict(overrides or {})})
    except ih.ImportRefused:
        suggested, _unmapped = ih.map_headers(headers, exact)
    taken = set(exact.values()) | set((overrides or {}).values())
    suggested = {h: ("" if (f in taken and h not in exact and h not in (overrides or {})) else f) for h, f in suggested.items()}
    out = []
    for h in headers:
        samples = []
        for r in rows:
            value = ih.clean(r.get(h, ""))
            if value and value not in samples:
                samples.append(value)
            if len(samples) == 3:
                break
        out.append({"header": h, "samples": samples, "suggested": suggested.get(h, "")})
    return out


def mapping_from_form(headers: list[str], form: Mapping[str, str]) -> dict[str, str]:
    """{source header: ledger field} from the mapping page's `map.<i>` picks. A target used twice,
    or one that is not a staging column, is refused."""
    mapping: dict[str, str] = {}
    allowed = set(STAGING_FIELDS) | {"source_profit"}
    for i, h in enumerate(headers):
        target = str(form.get(f"map.{i}", "") or "").strip()
        if not target:
            continue
        if target not in allowed:
            raise StagingError(f"{target!r} is not a ledger column")
        if target in mapping.values():
            raise StagingError(f"{_DISPLAY.get(target, target)} is mapped from two columns ({[k for k, v in mapping.items() if v == target][0]!r} and {h!r})")
        mapping[h] = target
    if not mapping:
        raise StagingError("map at least one column")
    return mapping


def detect_order(batch: Batch, mapping: Mapping[str, str]) -> tuple[str | None, list[str]]:
    """The file's day/month order from its slash dates ('mdy' / 'dmy' / None when never
    disambiguated), and a few sample dates. A file that mixes orders refuses (StagingError)."""
    _headers, rows = read_source(batch)
    inverse = {f: h for h, f in mapping.items()}
    values = []
    for r in rows:
        for f in DATE_FIELDS:
            h = inverse.get(f)
            if h:
                v = ih.clean(r.get(h, ""))
                if v:
                    values.append(v)
    try:
        verdict = ih.detect_date_order(values)
    except ih.ImportRefused as exc:
        raise StagingError(str(exc)) from exc
    samples = []
    for v in values:
        if v not in samples:
            samples.append(v)
        if len(samples) == 4:
            break
    return verdict, samples


# --------------------------------------------------------------------------------------------------
# Staging rows from source rows
# --------------------------------------------------------------------------------------------------


def _num_text(value: float | int | None) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return f"{value:.2f}" if isinstance(value, float) else str(value)


def stage_rows(raw_rows: list[dict], mapping: Mapping[str, str], *, date_order: str, profile: str = "") -> list[StagedRow]:
    """Source rows -> staged rows with every cell as ledger text. Nothing is invented: a cell the
    source does not have, or one that does not parse, stays blank with a warning, and the gap
    shows on the sheet. A cell holding several tracking numbers becomes one row per box (the
    CLI's rule), shipments numbered per order."""
    inverse = {f: h for h, f in mapping.items()}
    date_order = date_order or "mdy"
    out: list[StagedRow] = []
    ship_no: dict[tuple[str, str], int] = {}
    next_no: dict[str, int] = {}

    def shipment_for(order_id: str, tracking: str, fallback: int) -> str:
        if not order_id:
            return str(fallback)
        key = (order_id, tracking)
        if key not in ship_no:
            next_no[order_id] = next_no.get(order_id, 0) + 1
            ship_no[key] = next_no[order_id]
        return str(ship_no[key])

    for n, raw in enumerate(raw_rows, start=1):
        if not any(ih.clean(v) for k, v in raw.items() if k):
            continue
        get = lambda f: ih.clean(raw.get(inverse.get(f, ""), "")) if f in inverse else ""  # noqa: E731
        cells = {f: "" for f in STAGING_FIELDS}
        warnings: list[str] = []
        for f in DATE_FIELDS:
            try:
                cells[f] = ih.to_iso(get(f), date_order)
            except ValueError:
                warnings.append(f"{_DISPLAY[f]} {get(f)!r} not understood -- fill it")
        raw_status = get("status")
        if raw_status:
            try:
                cells["status"] = ih.normalise_status(raw_status)
            except ValueError:
                warnings.append(f"Status {raw_status!r} not understood -- pick one")
        for f in ("order_id", "item_name", "retailer", "buying_group", "delivery_address", "order_url",
                  "tracking_url", "receipt_url", "package_id"):
            cells[f] = get(f)
        cells["profile_label"] = get("profile_label") or profile
        for f in INT_FIELDS:
            text = get(f)
            if text:
                number = ih.money(text)
                if number is None or number != int(number):
                    warnings.append(f"{_DISPLAY[f]} {text!r} is not a whole number")
                else:
                    cells[f] = str(int(number))
        quantity = int(cells["quantity"]) if cells["quantity"] else None
        for f in MONEY_FIELDS + ("cashback_rate", "promo_rate"):
            text = get(f)
            if text:
                number = ih.money(text)
                if number is None:
                    warnings.append(f"{_DISPLAY[f]} {text!r} is not a number")
                elif f in ("gift_card", "insurance"):
                    cells[f] = _num_text(abs(number))
                else:
                    cells[f] = _num_text(number)
        total = ih.money(get("total_cost")) if "total_cost" in inverse else None
        if not cells["cost_per_item"] and total is not None and quantity:
            cells["cost_per_item"] = _num_text(round(total / quantity, 2))
        if cells["cost_per_item"] and quantity is not None:
            cells["total_cost"] = _num_text(round(float(cells["cost_per_item"]) * quantity, 2))
        elif total is not None:
            cells["total_cost"] = _num_text(total)
        card_name, last4 = ih.split_card(get("card_name"))
        cells["card_name"] = card_name
        cells["card_last4"] = get("card_last4") or last4
        cells["tracking_submitted"] = "TRUE" if ih.truthy(get("tracking_submitted")) else ""
        source_profit = ih.money(get("source_profit")) if "source_profit" in inverse else None
        if source_profit is not None and cells["payout_amount"] and cells["total_cost"]:
            rate = float(cells["cashback_rate"] or 0)
            expected = float(cells["payout_amount"]) - float(cells["total_cost"]) * (1 - rate) - float(cells["insurance"] or 0)
            if abs(expected - source_profit) > 0.011:
                warnings.append(f"the file's profit {source_profit:.2f} disagrees with payout - COGS - insurance "
                                f"({expected:.2f}): check the money cells")
        trackings = ih.tracking_numbers(get("tracking_number"))
        pieces = trackings or [""]
        # A split needs a Quantity of at least one per box to share the money out; with none, or
        # fewer units than boxes, the per-box cells stay blank for the user rather than the
        # order's figures landing whole on every box or a qty-0 / $0 box row passing as complete.
        split_known = len(pieces) > 1 and quantity is not None and quantity >= len(pieces)
        if len(pieces) > 1:
            warnings.append(f"{len(pieces)} tracking numbers in one cell -- split into one row per box"
                            + ("" if split_known else
                               f": Quantity {'is blank' if quantity is None else 'is ' + str(quantity)} for {len(pieces)} boxes, "
                               f"so each box's Quantity, Total Cost, Insurance and payout are left for you"))
        shares = ih.distribute(quantity, len(pieces)) if split_known else [quantity] * len(pieces)
        for k, (tracking, qty) in enumerate(zip(pieces, shares), start=1):
            row_cells = dict(cells)
            row_cells["tracking_number"] = tracking
            row_cells["shipment"] = get("shipment") if (len(pieces) == 1 and get("shipment")) else shipment_for(cells["order_id"], tracking, k)
            if split_known:
                share = qty / quantity
                row_cells["quantity"] = str(qty)
                row_cells["total_cost"] = _num_text(round(float(cells["cost_per_item"]) * qty, 2)) if cells["cost_per_item"] else ""
                for f in ("insurance", "payout_amount", "expected_payout"):
                    if cells[f]:
                        row_cells[f] = _num_text(round(float(cells[f]) * share, 2))
            elif len(pieces) > 1:
                for f in PER_BOX:
                    row_cells[f] = ""
            out.append(StagedRow(id=f"r{n:04d}-{k}", source_row=n, cells=row_cells, warnings=list(warnings)))
    return out


def prorate_order_level(rows: list[StagedRow]) -> list[str]:
    """Shipping, sales tax, gift card and rewards written as the ORDER's total on every row of an
    order (what an order page shows, and what the sync's proration assumes) become each row's
    share by Total Cost. Applied only when every row of the order carries the same non-blank
    figure and a Total Cost. Returns the order ids touched."""
    by_order: dict[str, list[StagedRow]] = {}
    for r in rows:
        if r.cells.get("order_id"):
            by_order.setdefault(r.cells["order_id"], []).append(r)
    touched = []
    for order_id, group in by_order.items():
        if len(group) < 2:
            continue
        costs = [ih.money(r.cells.get("total_cost", "")) for r in group]
        if any(c is None for c in costs) or not sum(costs):
            continue
        weights = [c / sum(costs) for c in costs]
        changed = False
        for f in ORDER_LEVEL:
            values = {r.cells.get(f, "") for r in group}
            if len(values) != 1 or not next(iter(values)):
                continue
            total = ih.money(next(iter(values)))
            if total is None:
                continue
            shares = [round(total * w, 2) for w in weights]
            shares[-1] = round(total - sum(shares[:-1]), 2)
            for r, share in zip(group, shares):
                r.cells[f] = _num_text(share)
            changed = True
        if changed:
            touched.append(order_id)
    return touched


# --------------------------------------------------------------------------------------------------
# What a row still needs
# --------------------------------------------------------------------------------------------------


def gaps_for(row: StagedRow) -> list[str]:
    """The cells the audit would fail this row for, as display names ("Tracking Submitted (not
    ticked)" for the tick), COGS / Total Profit left out."""
    from scripts.audit_ledger import mandatory_gaps

    missing, unticked = mandatory_gaps(row.cells)
    return [m for m in missing if m not in IMPORT_EXEMPT] + [f"{n} (not ticked)" for n in unticked]


def gap_fields(row: StagedRow) -> set[str]:
    from scripts.audit_ledger import mandatory_gaps

    missing, unticked = mandatory_gaps(row.cells)
    return {_FIELD_OF[m] for m in missing if m not in IMPORT_EXEMPT and m in _FIELD_OF} | {_FIELD_OF[n] for n in unticked if n in _FIELD_OF}


def key_of(row: StagedRow) -> tuple[str, str, str, str]:
    c = row.cells
    return (c.get("order_id", "").strip(), c.get("order_date", "").strip(), c.get("item_name", "").strip(),
            normalize_shipment(str(c.get("shipment", "") or "")) or "1")


def ledger_index(ledger_rows: Iterable) -> dict:
    """What classify() compares against, from the dashboard's rows: exact keys, order ids and
    which order holds each tracking number."""
    keys, orders, trackings = set(), set(), {}
    for r in ledger_rows:
        key = (r.order_id.strip(), r.order_date.strip(), r.item_name.strip(), normalize_shipment(str(r.shipment or "")) or "1")
        keys.add(key)
        orders.add(key[0])
        number = (r.tracking_number or "").strip()
        if number:
            trackings.setdefault(number, key[0])
    return {"keys": keys, "orders": orders, "trackings": trackings}


NOTES = {
    "duplicate": "already on the ledger under this key: skipped",
    "staged_open": "open order (ordered / shipped): the scrapers own it -- import by hand once you are sure",
    "staged_near": "the ledger already holds this order under another item name or shipment, or another order "
                   "holds this tracking number: check before importing",
}


def classify(rows: list[StagedRow], index: Mapping) -> dict[str, list[StagedRow]]:
    """Every staged row into one bucket: duplicate (exact key on the ledger, or twice in the
    file), staged_open, staged_near, incomplete (a mandatory gap), complete. The open and near
    buckets carry their note on the row so the sheet shows why the row waits.

    An order THIS BATCH imported is not "already on the ledger" for its own remaining rows: item 1
    of a multi-item order lands on the first run, and item 2 -- filled in later -- must follow it,
    not be held as a near duplicate of its sibling for ever. A row the user
    marked "import anyway" skips both holds; nothing skips the exact-key refusal."""
    out: dict[str, list[StagedRow]] = {"complete": [], "incomplete": [], "duplicate": [], "staged_open": [], "staged_near": []}
    seen: set[tuple] = set()
    own_orders = {key_of(r)[0] for r in rows if r.status == "imported"}
    for row in rows:
        if row.status != "staged":
            continue
        key = key_of(row)
        if key in index["keys"] or key in seen:
            row.note = NOTES["duplicate"]
            out["duplicate"].append(row)
            continue
        seen.add(key)
        status = row.cells.get("status", "").strip().lower()
        tracking = row.cells.get("tracking_number", "").strip()
        elsewhere = key[0] and key[0] in index["orders"] and key[0] not in own_orders
        held = tracking and index["trackings"].get(tracking) not in (None, key[0])
        if row.accepted:
            pass
        elif status in OPEN_STATUSES:
            row.note = NOTES["staged_open"]
            out["staged_open"].append(row)
            continue
        elif elsewhere or held:
            row.note = NOTES["staged_near"]
            out["staged_near"].append(row)
            continue
        if gaps_for(row):
            row.note = ""
            out["incomplete"].append(row)
        else:
            row.note = ""
            out["complete"].append(row)
    return out


# --------------------------------------------------------------------------------------------------
# Editing the sheet
# --------------------------------------------------------------------------------------------------


def write_staged_cell(staging: Staging, row_id: str, field: str, value: str, *, expected: str | None = None,
                      index: Mapping | None = None) -> StagedRow:
    """Write one cell of a staged row. The key cells are editable here (the row is not on the
    ledger yet); every other cell is checked as the ledger's editor would check it; Total Cost is
    derived and follows Quantity x Cost Per Item. `expected` is the text the editor showed.

    A key cell may not be edited ONTO a key the ledger (`index`) or another row of the sheet
    already holds: the commit would mark such a row a duplicate and it would leave the sheet for
    good, its cells with it. Refused here, the row keeps its
    old key and stays in view."""
    row = staging.row(row_id)
    if field not in STAGING_FIELDS:
        raise StagingError(f"{field} is not a column of the sheet")
    current = row.cells.get(field, "")
    if expected is not None and str(expected) != current:
        raise StagingError(f"the cell changed meanwhile: it reads {current!r} now; reload the page")
    text = (value or "").strip()
    if field in DERIVED:
        raise StagingError("Total Cost is computed from Quantity x Cost Per Item")
    if field == "order_date":
        if text and not valid_iso_date(text):
            raise StagingError("Order Date must be a real calendar date written as YYYY-MM-DD")
        stored = text
    elif field == "shipment":
        if text and not text.isdigit():
            raise StagingError("Shipment must be a number (1, 2, ...)")
        stored = text
    elif field in KEY_EDITABLE:
        stored = text
    else:
        try:
            coerced = validate(field, text)
        except EditError as exc:
            raise StagingError(str(exc)) from exc
        if coerced is True:
            stored = "TRUE"
        elif coerced is False:
            stored = "FALSE"
        elif isinstance(coerced, (int, float)):
            stored = _num_text(coerced)
        else:
            stored = str(coerced)
    if field in KEY_EDITABLE and stored != current:
        probe = StagedRow(id=row.id, source_row=row.source_row, cells={**row.cells, field: stored})
        new_key = key_of(probe)
        if all(new_key[:3]):
            if index is not None and new_key in index["keys"]:
                raise StagingError(f"the ledger already holds a row with this key ({new_key[0]} / {new_key[1]} / "
                                   f"{new_key[2]} / shipment {new_key[3]}): the row would be dropped as a duplicate")
            for other in staging.rows:
                if other.id != row.id and other.status != "duplicate" and key_of(other) == new_key:
                    raise StagingError(f"row {other.source_row} of the sheet already has this key: "
                                       f"the row would be dropped as a duplicate")
    row.cells[field] = stored
    if field in ("quantity", "cost_per_item"):
        q, c = row.cells.get("quantity", ""), row.cells.get("cost_per_item", "")
        row.cells["total_cost"] = _num_text(round(float(q) * float(c), 2)) if q and c else ""
    row.note = ""
    return row


def remove_rows(staging: Staging, ids: Iterable[str]) -> list[StagedRow]:
    wanted = set(ids)
    removed = [r for r in staging.rows if r.id in wanted and r.status == "staged"]  # an import record is never dropped
    gone = {r.id for r in removed}
    staging.rows = [r for r in staging.rows if r.id not in gone]
    return removed


def accept_rows(staging: Staging, ids: Iterable[str], *, on: bool = True) -> list[StagedRow]:
    """Mark staged rows "import anyway" (or take the mark off): the open-order / near-duplicate
    holds stop applying, and the row lands with the next commit once it is complete."""
    wanted = set(ids)
    rows = [r for r in staging.staged if r.id in wanted]
    for r in rows:
        r.accepted = on
        r.note = ""
    return rows


def staging_csv(staging: Staging) -> str:
    """The staged rows in the ledger's columns, to fix in a spreadsheet and upload again."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(HEADER)
    for r in staging.staged:
        writer.writerow([r.cells.get(f, "") for f in FIELDNAMES])
    return out.getvalue()


def choices_for(staging: Staging | None, ledger_choices: Mapping) -> dict:
    """#cell-choices for the sheet: the ledger's own answers per choice column plus the sheet's."""
    values = {k: list(v) for k, v in (ledger_choices.get("values") or {}).items()}
    for r in (staging.rows if staging else []):
        for f in ("status", "retailer", "buying_group", "card_name", "card_last4", "profile_label"):
            v = r.cells.get(f, "")
            if v and v not in values.setdefault(f, []):
                values[f].append(v)
    return {"values": {k: sorted(v, key=str.lower) for k, v in values.items()},
            "card_pairs": list(ledger_choices.get("card_pairs") or [])}


# --------------------------------------------------------------------------------------------------
# Importing the complete rows
# --------------------------------------------------------------------------------------------------


@dataclass
class ImportResult:
    imported: list = field(default_factory=list)
    duplicates: list = field(default_factory=list)
    refused: list = field(default_factory=list)  # (row, message)
    remaining: int = 0

    def notice(self) -> str:
        parts = [f"{len(self.imported)} row(s) imported"]
        if self.duplicates:
            parts.append(f"{len(self.duplicates)} already on the ledger, skipped")
        if self.refused:
            parts.append(f"{len(self.refused)} refused by the ledger")
        parts.append(f"{self.remaining} staged")
        return "; ".join(parts)


def import_complete(staging: Staging, writer, index: Mapping, *, save: Callable[[Staging], None],
                    clock: Callable[[], datetime]) -> ImportResult:
    """Write every complete staged row through `writer.add_row` (the Orders page's own write
    path: run-lock refusal, duplicate refusal, hand-edit protection). The sheet is saved after
    every row, so a crash or a refusal mid-way leaves it exact. A run holding the lock stops the
    import where it is and is re-raised for the page to say so."""
    result = ImportResult()
    buckets = classify(staging.rows, index)
    for row in buckets["duplicate"]:
        row.status = "duplicate"
        result.duplicates.append(row)
    save(staging)
    for row in buckets["complete"]:
        try:
            written = writer.add_row(dict(row.cells))
        except RunInProgress:
            save(staging)
            result.remaining = len(staging.staged)
            raise
        except EditError as exc:
            if "already on the ledger" in str(exc):
                row.status = "duplicate"
                row.note = NOTES["duplicate"]
                result.duplicates.append(row)
            else:
                row.note = f"refused by the ledger: {exc}"
                result.refused.append((row, str(exc)))
        else:
            row.status = "imported"
            row.ledger_row = written.get("row_number")
            row.imported_at = clock().isoformat(timespec="seconds")
            row.note = ""
            result.imported.append(row)
        save(staging)
    result.remaining = len(staging.staged)
    return result
