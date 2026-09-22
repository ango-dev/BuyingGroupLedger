"""The SQLite ledger wearing a worksheet face -- how the app runs off the database.

Every writer in this codebase -- the scrapers' upsert (ledger/sync.sync_csv_to_ledger), the sort,
the buying-group sync (payouts, insurance, the submitted tick), the BFMR auto-reply's read, the
dashboard's cell editor, the backfill scripts -- addresses the ledger as a POSITIONAL GRID through
a handful of worksheet methods (the contract inherited from the spreadsheet this file replaced,
2026-09-18):

    get_all_values() / get_values(value_render_option=...)   the grid, formatted or stored values
    update(range, values, value_input_option=...)            a block from an A1 anchor
    batch_update([{"range", "values"}], value_input_option)  single cells
    sort(*(column, direction), range=...)                    the ledger block, blanks last
    delete_rows(n) / append_row / add_rows / add_cols / row_count / col_count / title

So rather than rewrite 3,000 lines of money-path code, THIS class implements that surface over the
SQLite file: the grid is HEADER at row 1 and one ledger row per `sheet_row` beneath it, every write
lands in the file at once (the whole table, one transaction -- the ledger is a few hundred rows),
and `ledger.sync._get_worksheet()` hands it out. The writers do not know the difference, and the
tests that pin their behaviour against the fake worksheet pin it against this one too.

WHAT THE CONTRACT ASKS OF IT:

  * FORMULAS. The sync stamps `=IF(...)` into COGS and Total Profit and reads their RESULTS back.
    The two columns are never stored as text: a write of formula text (or anything) into them
    is ignored, and every read computes them from the row with web.ledger_reader.cogs_of /
    profit_of -- the Python mirrors of the two formulas, pinned by test against the formulas' own
    cell references. So the number is the ledger's number, live, for every row, always.
  * RENDERING. A FORMATTED read returns strings (a bool as TRUE / FALSE, a number as its shortest
    text, blank as ""); an UNFORMATTED read returns the stored types -- and a blank cell is ""
    there too, never None, because every reader does str(cell).strip(). The sync's own parser
    (_parse_display_number) reads both.
  * NONE MEANS SKIP. `update` leaves a cell alone for None (RAW) and clears it for "" under
    USER_ENTERED -- the two conventions _blank_to_none and _clear_cells rely on.
  * THE GRID IS ELASTIC. There is no 400 past the last row; row_count / add_rows exist so the
    callers' capacity checks keep working, and the numbers are honest.
  * ROWS WITHOUT AN ORDER ID are not ledger rows (the sync says so) and cannot be keyed, so they
    are dropped on write rather than stored; the table's primary key stays the upsert key.
"""

from __future__ import annotations

import enum
import re
from typing import Any

from models.order import FIELDNAMES
from ledger.sync import HEADER, _BOOL_FIELDS, _INT_FIELDS, _NUMERIC_FIELDS

from ledger_db.store import FORMULA_FIELDS, LedgerDb, LedgerStale

class ValueInputOption(str, enum.Enum):
    """How a write's values are taken: RAW as typed, USER_ENTERED parsed the way a hand edit is
    (the two conventions the writers were written against; DbWorksheet reads `.value`)."""

    raw = "RAW"
    user_entered = "USER_ENTERED"


class ValueRenderOption(str, enum.Enum):
    """What a read hands back: FORMATTED text, the UNFORMATTED stored values, or a formula."""

    formatted = "FORMATTED_VALUE"
    unformatted = "UNFORMATTED_VALUE"
    formula = "FORMULA"


_A1 = re.compile(r"^(?:'[^']*'!|[^!]+!)?\$?([A-Z]+)\$?(\d+)(?::\$?([A-Z]+)?\$?(\d+)?)?$")
_KEY_INDEX = FIELDNAMES.index("order_id")
_FORMULA_INDEXES = {FIELDNAMES.index(f) for f in FORMULA_FIELDS}


def _col_number(letters: str) -> int:
    number = 0
    for char in letters:
        number = number * 26 + (ord(char) - ord("A") + 1)
    return number


def parse_a1(range_name: str) -> tuple[int, int]:
    """(row, column), 1-based, of a range's top-left cell ("A12", "AB3:AB3", "Orders!C4")."""
    match = _A1.match(str(range_name).strip())
    if not match:
        raise ValueError(f"not an A1 range: {range_name!r}")
    return int(match.group(2)), _col_number(match.group(1))


def _formatted(value: Any) -> str:
    """What a formatted read hands back for a stored value: text, as the contract always had it."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    return str(value)


def _typed(field: str, value: Any) -> Any:
    """Store a cell typed: blanks as None, a bool column as bool, numbers as
    numbers; anything else as its text."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if field in _BOOL_FIELDS:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        return True if text in ("true", "1", "yes", "checked") else False if text in (
            "false", "0", "no", "unchecked") else str(value)
    if field in _NUMERIC_FIELDS and not isinstance(value, bool):
        if isinstance(value, (int, float)):
            return int(value) if field in _INT_FIELDS and float(value).is_integer() else value
        from ledger.sync import _parse_display_number

        number = _parse_display_number(value)
        if number is None:
            return str(value)
        return int(number) if field in _INT_FIELDS and float(number).is_integer() else number
    return str(value)


class ReadOnly(RuntimeError):
    """A write on the read-only adapter (the audit's and the dashboard reader's view)."""


class DbWorksheet:
    """See the module docstring. `db` is a ledger_db.store.LedgerDb; `read_only` refuses writes."""

    HEADROOM = 200

    def __init__(self, db: LedgerDb | None = None, *, read_only: bool = False):
        self.db = db or LedgerDb()
        self.read_only = read_only
        self.title = self.db.path.name
        self._rows: list[list[Any]] = []
        self._load()

    # --- the grid --------------------------------------------------------------------------------
    def _load(self) -> None:
        self._version = self.db.version()  # BEFORE the rows: a write between the two only makes the next persist retry
        self._rows = [list(HEADER)]
        for record in self.db.fetch_rows():
            # SQLite holds a bool column as 0 / 1; the grid holds bools, as the grid's stored
            # values always have (the sync's _parse_checkbox and the tracking sync's tick check read them).
            self._rows.append([
                (bool(record.get(f)) if f in _BOOL_FIELDS and record.get(f) is not None
                 else record.get(f))
                for f in FIELDNAMES
            ])
        self.row_count = len(self._rows) + self.HEADROOM
        self.col_count = len(HEADER)

    def _row(self, number: int) -> list[Any]:
        while len(self._rows) < number:
            self._rows.append([None] * len(FIELDNAMES))
        row = self._rows[number - 1]
        if len(row) < len(FIELDNAMES):
            row.extend([None] * (len(FIELDNAMES) - len(row)))
        return row

    def _ledger_row(self, values: list[Any], number: int):
        from web.ledger_reader import LedgerRow

        cells = {f: _formatted(v) for f, v in zip(FIELDNAMES, values)}
        numbers = {f: (float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None)
                   for f, v in zip(FIELDNAMES, values) if f in _NUMERIC_FIELDS}
        return LedgerRow(cells=cells, row_number=number, numbers=numbers)

    def _computed(self, number: int) -> list[Any]:
        """A data row with its two formula columns computed from the rest of it."""
        values = list(self._row(number))
        if number == 1:
            return values
        row = self._ledger_row(values, number)
        cogs = FIELDNAMES.index("cogs")
        profit = FIELDNAMES.index("total_profit")
        values[cogs] = None
        values[profit] = None
        from web.ledger_reader import cogs_of, profit_of

        values[cogs] = cogs_of(row)
        values[profit] = profit_of(self._ledger_row(values, number))
        return values

    def _persist(self) -> None:
        if self.read_only:
            raise ReadOnly("this ledger view is read-only")
        records = []
        position = 0
        for number in range(2, len(self._rows) + 1):
            values = self._computed(number)
            if not _formatted(values[_KEY_INDEX]).strip():
                continue  # not a ledger row: nothing to key it by
            position += 1
            record = {f: v for f, v in zip(FIELDNAMES, values)}
            record["sheet_row"] = position + 1
            records.append(record)
        try:
            self.db.replace_rows(records, backend="db", source=self.title, log_run=False,
                                 expected_version=self._version)
        except LedgerStale:
            self._load()  # the grid follows the file again; the caller re-applies its write
            raise
        self._load()

    # --- reads -----------------------------------------------------------------------------------
    def get_all_values(self) -> list[list[str]]:
        return [[_formatted(v) for v in self._computed(n)] for n in range(1, len(self._rows) + 1)]

    def get_values(self, range_name: str | None = None, value_render_option=None, **_kwargs):
        """The grid. UNFORMATTED keeps the stored types (a number, a bool) but a BLANK cell is ""
        in every render, as the worksheet contract has it: the readers do `str(cell).strip()`, and
        a None here became the text "None" -- six blank tracking numbers went to BFMR as the
        package "None" on the first run after the cutover (2026-09-18)."""
        option = str(getattr(value_render_option, "value", value_render_option) or "").upper()
        if "UNFORMATTED" in option:
            return [[("" if v is None else v) for v in self._computed(n)] if n > 1 else list(HEADER)
                    for n in range(1, len(self._rows) + 1)]
        return self.get_all_values()

    def data_rows(self) -> list[list[Any]]:
        return [[("" if v is None else v) for v in self._computed(n)]
                for n in range(2, len(self._rows) + 1)]

    # --- writes ----------------------------------------------------------------------------------
    def _guard(self) -> None:
        if self.read_only:
            raise ReadOnly("this ledger view is read-only; nothing was written")

    def _put(self, row_number: int, col_number: int, value: Any, option: str) -> None:
        if row_number == 1:
            return  # the header is HEADER, always
        index = col_number - 1
        if index >= len(FIELDNAMES) or index in _FORMULA_INDEXES:
            return  # beyond the schema, or a formula column: computed, never stored
        if value is None:
            return  # RAW None: leave the cell alone
        row = self._row(row_number)
        if isinstance(value, str) and value.strip() == "":
            if option == "USER_ENTERED":
                row[index] = None  # USER_ENTERED "": clear
            return  # RAW "": nothing (the sync never sends it; _blank_to_none turns it into None)
        row[index] = _typed(FIELDNAMES[index], value)

    def update(self, range_name: str | None = None, values: list | None = None,
               value_input_option=None, **kwargs) -> dict:
        self._guard()
        if range_name is None:
            range_name = kwargs.get("range")
        if values is None:
            values = kwargs.get("values", [])
        option = str(getattr(value_input_option, "value", value_input_option) or "RAW").upper()
        top, left = parse_a1(range_name)
        for r, block_row in enumerate(values):
            for c, value in enumerate(block_row):
                self._put(top + r, left + c, value, option)
        self._persist()
        return {"updatedRange": range_name}

    def batch_update(self, data: list[dict], value_input_option=None, **_kwargs) -> dict:
        self._guard()
        option = str(getattr(value_input_option, "value", value_input_option) or "RAW").upper()
        for entry in data:
            top, left = parse_a1(entry["range"])
            for r, block_row in enumerate(entry.get("values") or []):
                for c, value in enumerate(block_row):
                    self._put(top + r, left + c, value, option)
        self._persist()
        return {"responses": [{} for _ in data]}

    def append_row(self, values: list, **_kwargs) -> dict:
        self._guard()
        if [str(v) for v in values] == list(HEADER) and len(self._rows) == 1:
            return {}  # the header is already row 1
        self._rows.append([None] * len(FIELDNAMES))
        for c, value in enumerate(values):
            self._put(len(self._rows), c + 1, value, "RAW")
        self._persist()
        return {}

    def append_rows(self, rows: list[list], **_kwargs) -> dict:
        self._guard()
        for values in rows:
            self._rows.append([None] * len(FIELDNAMES))
            for c, value in enumerate(values):
                self._put(len(self._rows), c + 1, value, "RAW")
        self._persist()
        return {}

    def delete_rows(self, start: int, end: int | None = None) -> dict:
        self._guard()
        end = end or start
        if start <= 1:
            raise ValueError("row 1 is the header")
        del self._rows[start - 1:end]
        self._persist()
        return {}

    def sort(self, *specs, range: str | None = None, **_kwargs) -> dict:
        """The grid sort: `specs` are (column number, "asc" | "des"); blanks last either way."""
        self._guard()
        first, last = 2, len(self._rows)
        if range:
            bounds = str(range).split(":")
            first = parse_a1(bounds[0])[0]
            if len(bounds) > 1 and any(ch.isdigit() for ch in bounds[1]):
                last = parse_a1(bounds[1])[0]
        last = min(last, len(self._rows))

        def cell(row: list, column: int):
            return row[column - 1] if column - 1 < len(row) else None

        def key(row: list, column: int):
            value = cell(row, column)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return (0, value)
            return (1, _formatted(value))

        block = self._rows[first - 1:last]
        for column, direction in reversed(specs):
            block.sort(key=lambda r, c=column: key(r, c), reverse=(str(direction).lower() == "des"))
            block.sort(key=lambda r, c=column: _formatted(cell(r, c)).strip() == "")
        self._rows[first - 1:last] = block
        self._persist()
        return {}

    def add_rows(self, count: int) -> None:
        self.row_count += int(count)

    def add_cols(self, count: int) -> None:
        self.col_count += int(count)

    def __repr__(self) -> str:
        return f"<DbWorksheet {self.title} rows={len(self._rows) - 1}{' read-only' if self.read_only else ''}>"
