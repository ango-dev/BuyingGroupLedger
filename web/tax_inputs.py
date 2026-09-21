"""The Taxes page's hand-entered side, and the Schedule C summary it produces.

WHAT THE PAGE ASKS FOR, AND WHY EACH IS DERIVED OR TYPED.

- **Program cashback**: Prime, Prime Business and Costco Executive pay cashback of their own,
  separate from any card or portal. One row per (profile, retailer) the config says is logged in,
  so the page asks for exactly what the setup implies.
- **Cards**: one row per card USED in the tax year -- every distinct Card Last 4 on the year's
  ledger rows, whether or not the Cards settings know it -- minus any last-4 the settings mark `virtual` (a
  virtual number of another card has no bonus of its own). One amount each: the sign-up bonus
  received (income). A card's ANNUAL FEE is an expense and goes in the expense list with its
  receipt, like every other expense; an older year file's `fees` section is ignored.
- **Cashback sites**: the usual portals plus any the user adds.
- **Expenses**: the user's own list of everything spent for the business in the year beyond the
  ledger's purchases -- each with a date, a description, an amount, who paid (the profile OR the
  email of the account: one or the other, user 2026-09-18), and a receipt (an uploaded file, kept
  under data/expenses/, or a link). All of those are REQUIRED. An entry can be edited in place; its receipt stays unless a
  new file or link replaces it.
- **Other income**: an open list of income lines for what fits nowhere above. It is income
  ONLY: every expense goes through the expense list, with its receipt.

The answers live PER TAX YEAR in data/tax_inputs.json (inside every backup, beside the ledger),
never in config.json, which is setup rather than a year's figures.

THE SUMMARY. scripts/tax_report.build_report's cash-basis figures (payouts by Payout Date; COGS
and insurance by Order Date) laid out on Schedule C's lines, with the hand-entered items placed
where they most plausibly belong: program cashback, portal cashback, bonuses and other income on
line 6; the expense list and other expenses on line 27a; insurance on 15; Part III shows the card
cashback netted from cost. A summary for a preparer, not tax advice -- every line says what it
holds.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping

#: The cashback sites the page always offers a row for (the user names the rest).
DEFAULT_SITES = ("TopCashback", "Rakuten", "ShopBack", "RetailMeNot", "Capital One Shopping")

#: The cashback program a retailer login implies.
PROGRAMS = {  # the programs' own names for what they pay
    "costco": "Costco Executive Cashback",
    "amazon": "Prime Young Adult Cashback",
    "amazon-business": "Prime Business Rewards",
}

FILE_NAME = "tax_inputs.json"
EXPENSES_DIR = "expenses"  # under data/: data/expenses/<year>/<id>_<file>

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class Prompt:
    kind: str    # "program" | "card"
    key: str     # stable id the stored amount is keyed by ("card:<last4>": the bonus: field)
    label: str
    hint: str = ""

    @property
    def last4(self) -> str:
        return self.key.rsplit(":", 1)[-1]


@dataclass
class YearInputs:
    programs: dict[str, float] = field(default_factory=dict)   # prompt key -> cashback received
    bonuses: dict[str, float] = field(default_factory=dict)    # "bonus:<last4>" -> bonus received
    sites: dict[str, float] = field(default_factory=dict)      # site name -> cashback received
    expenses: list[dict] = field(default_factory=list)         # see add_expense
    other: list[dict] = field(default_factory=list)            # {label, amount, kind}
    notes: str = ""

    @property
    def program_total(self) -> float:
        return round(sum(self.programs.values()), 2)

    @property
    def bonus_total(self) -> float:
        return round(sum(self.bonuses.values()), 2)

    @property
    def site_total(self) -> float:
        return round(sum(self.sites.values()), 2)

    @property
    def expense_total(self) -> float:
        return round(sum(float(e.get("amount") or 0) for e in self.expenses), 2)

    @property
    def other_income(self) -> float:
        return round(sum(float(o.get("amount") or 0) for o in self.other if o.get("kind") == "income"), 2)

    @property
    def other_expense(self) -> float:
        return round(sum(float(o.get("amount") or 0) for o in self.other if o.get("kind") != "income"), 2)

    def to_json(self) -> dict:
        return {"programs": self.programs, "bonuses": self.bonuses, "sites": self.sites,
                "expenses": self.expenses, "other": self.other, "notes": self.notes}

    @classmethod
    def from_json(cls, payload: Mapping) -> "YearInputs":
        def amounts(section) -> dict[str, float]:
            out = {}
            for k, v in (payload.get(section) or {}).items():
                try:
                    out[str(k)] = round(float(v), 2)
                except (TypeError, ValueError):
                    continue
            return out

        other = []
        for o in payload.get("other") or []:
            if not isinstance(o, dict) or not str(o.get("label", "")).strip():
                continue
            try:
                amount = round(float(o.get("amount") or 0), 2)
            except (TypeError, ValueError):
                continue
            other.append({"label": str(o["label"]).strip(), "amount": amount,
                          "kind": "income" if o.get("kind") == "income" else "expense"})
        expenses = []
        for e in payload.get("expenses") or []:
            if not isinstance(e, dict) or not e.get("id") or not e.get("date"):
                continue
            try:
                amount = round(float(e.get("amount") or 0), 2)
            except (TypeError, ValueError):
                continue
            expenses.append({
                "id": str(e["id"]), "date": str(e["date"]), "description": str(e.get("description") or ""),
                "amount": amount, "category": str(e.get("category") or ""),
                "profile": str(e.get("profile") or ""), "email": str(e.get("email") or ""),
                "receipt": dict(e.get("receipt") or {}), "added_at": str(e.get("added_at") or ""),
            })
        expenses.sort(key=lambda e: (e["date"], e["added_at"]))
        return cls(programs=amounts("programs"), bonuses=amounts("bonuses"),
                   sites=amounts("sites"), expenses=expenses, other=other,
                   notes=str(payload.get("notes") or ""))


# --------------------------------------------------------------------------------------------------
# What the page asks for
# --------------------------------------------------------------------------------------------------


def program_prompts(profiles: Iterable) -> list[Prompt]:
    """One per (profile, retailer login) that has a cashback program of its own."""
    out = []
    for profile in profiles:
        for retailer in getattr(profile, "retailers", None) or []:
            name = PROGRAMS.get(str(retailer).strip().lower())
            if name is None:
                continue
            out.append(Prompt("program", f"program:{profile.label}:{retailer}", f"{name} — {profile.label}"))
    return out


def card_prompts(rows: Iterable, year: int, cards: Iterable = ()) -> list[Prompt]:
    """One per card USED in `year`: every distinct Card Last 4 on rows placed in the year (with
    the card name the ledger recorded, or the settings' name), minus the last-4s the Cards
    settings mark virtual. Each takes two amounts: `bonus:<last4>` and `fee:<last4>`."""
    # a virtual number has no bonus of its own -- unless it does (an Amex employee card: own_bonus)
    virtual = {str(getattr(c, "last4", "")).strip() for c in cards
               if getattr(c, "virtual", False) and not getattr(c, "own_bonus", False)}
    names = {str(getattr(c, "last4", "")).strip(): str(getattr(c, "name", "")) for c in cards}
    seen: dict[str, str] = {}
    for row in rows:
        if not row.order_date.startswith(str(year)) or getattr(row, "is_money_free", False):
            continue
        last4 = row.text("card_last4").strip()
        if not last4 or last4 in virtual:
            continue
        name = row.text("card_name").strip() or names.get(last4, "")
        if last4 not in seen or (name and not seen[last4]):
            seen[last4] = name
    return [Prompt("card", f"card:{last4}", f"{name or 'Card'} …{last4}")
            for last4, name in sorted(seen.items(), key=lambda kv: (kv[1].lower(), kv[0]))]


bonus_prompts = card_prompts  # the earlier name


def site_names(inputs: YearInputs) -> list[str]:
    names = list(DEFAULT_SITES)
    for name in inputs.sites:
        if name not in names:
            names.append(name)
    return names


# --------------------------------------------------------------------------------------------------
# Storage: data/tax_inputs.json, {year: YearInputs}
# --------------------------------------------------------------------------------------------------


def load_all(path: Path) -> dict[int, YearInputs]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for year, entry in (payload or {}).items():
        try:
            out[int(year)] = YearInputs.from_json(entry or {})
        except (TypeError, ValueError):
            continue
    return out


def load_year(path: Path, year: int) -> YearInputs:
    return load_all(path).get(int(year), YearInputs())


def save_year(path: Path, year: int, inputs: YearInputs) -> None:
    path = Path(path)
    everything = {str(y): v.to_json() for y, v in load_all(path).items()}
    everything[str(int(year))] = inputs.to_json()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(everything, indent=2, sort_keys=True), encoding="utf-8")


def _amount(text) -> float | None:
    text = str(text or "").strip().replace("$", "").replace(",", "")
    if not text:
        return None
    return round(float(text), 2)


def parse_form(form: Mapping[str, str], prompts: Iterable[Prompt]) -> tuple[dict, dict, dict, list, str]:
    """The save form's amounts -> (programs, bonuses, sites, other, notes). A blank amount
    is "none"; a bad one raises ValueError naming the field. The expense list is not on this form
    (it has its own add / delete routes) and is left as stored. The open list is income only."""
    errors: list[str] = []
    programs: dict[str, float] = {}
    bonuses: dict[str, float] = {}
    sites: dict[str, float] = {}
    other: list[dict] = []
    for p in prompts:
        if p.kind == "card":
            key = f"bonus:{p.last4}"
            try:
                value = _amount(form.get(key))
            except ValueError:
                errors.append(f"{p.label} sign-up bonus: not a number")
                continue
            if value is not None:
                bonuses[key] = value
            continue
        try:
            value = _amount(form.get(p.key))
        except ValueError:
            errors.append(f"{p.label}: not a number")
            continue
        if value is not None:
            programs[p.key] = value
    for i in _indexed(form, "site"):
        name = str(form.get(f"site.{i}.name") or "").strip()
        if not name:
            continue
        try:
            value = _amount(form.get(f"site.{i}.amount"))
        except ValueError:
            errors.append(f"{name}: not a number")
            continue
        if value is not None:
            sites[name] = value
    for i in _indexed(form, "other"):
        label = str(form.get(f"other.{i}.label") or "").strip()
        if not label:
            continue
        try:
            value = _amount(form.get(f"other.{i}.amount")) or 0.0
        except ValueError:
            errors.append(f"{label}: not a number")
            continue
        other.append({"label": label, "amount": value, "kind": "income"})
    if errors:
        raise ValueError("; ".join(errors))
    return programs, bonuses, sites, other, str(form.get("notes") or "").strip()


def apply_form(inputs: YearInputs, form: Mapping[str, str], prompts: Iterable[Prompt]) -> YearInputs:
    programs, bonuses, sites, other, notes = parse_form(form, prompts)
    return YearInputs(programs=programs, bonuses=bonuses, sites=sites,
                      expenses=list(inputs.expenses), other=other, notes=notes)


def _indexed(form: Mapping[str, str], prefix: str) -> list[int]:
    indexes = set()
    for name in form.keys():
        if name.startswith(prefix + "."):
            middle = name[len(prefix) + 1:].split(".", 1)[0]
            if middle.isdigit():
                indexes.add(int(middle))
    return sorted(indexes)


# --------------------------------------------------------------------------------------------------
# The expense list: every field required, the receipt kept beside the ledger
# --------------------------------------------------------------------------------------------------


def safe_filename(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(str(name or "")).name)
    name = re.sub(r"_+", "_", name)
    name = re.sub(r"_(?=\.)", "", name).strip("._") or "receipt"
    return name[:80]


def _expense_fields(fields: Mapping[str, str], *, year: int) -> tuple[dict, list[str]]:
    """The cleaned fields of the expense form and what is wrong with them (add and edit share
    it). Who paid is the profile OR the email of the account -- one is enough."""
    errors = []
    when = str(fields.get("date") or "").strip()
    if not _ISO_DATE.match(when):
        errors.append("Date must be written as YYYY-MM-DD")
    else:
        try:
            parsed = date.fromisoformat(when)
        except ValueError:
            errors.append("Date is not a real day")
            parsed = None
        if parsed is not None and parsed.year != int(year):
            errors.append(f"Date must fall in {year}")
    description = str(fields.get("description") or "").strip()
    if not description:
        errors.append("Description is required")
    try:
        amount = _amount(fields.get("amount"))
    except ValueError:
        amount = None
        errors.append("Amount is not a number")
    if amount is None:
        errors.append("Amount is required")
    profile = str(fields.get("profile") or "").strip()
    email = str(fields.get("email") or "").strip()
    if not profile and not email:
        errors.append("Who paid is required: the profile, or the email of the account")
    if email and not _EMAIL.match(email):
        errors.append("Email is not an address")
    clean = {"date": when, "description": description, "amount": amount,
             "category": str(fields.get("category") or "").strip(), "profile": profile, "email": email,
             "link": str(fields.get("receipt_url") or "").strip()}
    return clean, errors


def _store_receipt(receipt_file: tuple[str, bytes], *, year: int, entry_id: str, data_dir: Path) -> dict:
    filename, payload = receipt_file
    rel = Path(EXPENSES_DIR) / str(year) / f"{entry_id}_{safe_filename(filename)}"
    target = Path(data_dir) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return {"file": rel.as_posix(), "name": safe_filename(filename)}


def _unlink_receipt(receipt: Mapping, *, data_dir: Path) -> None:
    rel = (receipt or {}).get("file")
    if rel:
        try:
            (Path(data_dir) / rel).unlink()
        except OSError:
            pass


def add_expense(inputs: YearInputs, fields: Mapping[str, str], *, year: int, data_dir: Path,
                receipt_file: tuple[str, bytes] | None = None) -> dict:
    """Validate and append one expense. `receipt_file` = (filename, bytes) from the upload, or
    None when a link was given. Raises ValueError naming what is missing."""
    clean, errors = _expense_fields(fields, year=year)
    if not receipt_file and not clean["link"]:
        errors.append("A receipt is required: upload the file or give its link")
    if errors:
        raise ValueError("; ".join(errors))
    entry_id = uuid.uuid4().hex[:10]
    receipt = (_store_receipt(receipt_file, year=year, entry_id=entry_id, data_dir=data_dir) if receipt_file
               else {"url": clean["link"]})
    entry = {
        "id": entry_id, "date": clean["date"], "description": clean["description"], "amount": clean["amount"],
        "category": clean["category"], "profile": clean["profile"], "email": clean["email"],
        "receipt": receipt, "added_at": date.today().isoformat(),
    }
    inputs.expenses.append(entry)
    inputs.expenses.sort(key=lambda e: (e["date"], e["added_at"]))
    return entry


def update_expense(inputs: YearInputs, entry_id: str, fields: Mapping[str, str], *, year: int,
                   data_dir: Path, receipt_file: tuple[str, bytes] | None = None) -> dict:
    """Edit one expense in place: the
    fields are validated as on add; the receipt it has stays unless a new file or a different
    link replaces it (a replaced uploaded file is deleted). Returns the entry. ValueError names
    what is wrong; KeyError when no entry has the id."""
    entry = next((e for e in inputs.expenses if e["id"] == entry_id), None)
    if entry is None:
        raise KeyError(entry_id)
    clean, errors = _expense_fields(fields, year=year)
    current = dict(entry.get("receipt") or {})
    if not receipt_file and not clean["link"] and not (current.get("file") or current.get("url")):
        errors.append("A receipt is required: upload the file or give its link")
    if errors:
        raise ValueError("; ".join(errors))
    if receipt_file:
        _unlink_receipt(current, data_dir=data_dir)
        receipt = _store_receipt(receipt_file, year=year, entry_id=entry_id, data_dir=data_dir)
    elif clean["link"] and clean["link"] != current.get("url"):
        _unlink_receipt(current, data_dir=data_dir)
        receipt = {"url": clean["link"]}
    else:
        receipt = current
    entry.update({"date": clean["date"], "description": clean["description"], "amount": clean["amount"],
                  "category": clean["category"], "profile": clean["profile"], "email": clean["email"],
                  "receipt": receipt})
    inputs.expenses.sort(key=lambda e: (e["date"], e["added_at"]))
    return entry


#: The expenses table's editable columns, in the table's order.
EXPENSE_CELL_FIELDS = ("date", "description", "category", "profile", "email", "receipt_url", "amount")


def expense_raw(entry: Mapping, field: str) -> str:
    """What a cell of the expenses table shows for `field`, as text (the inline editor's
    data-raw): the amount to the cent, the receipt's link (blank when it is an uploaded file)."""
    if not entry:
        return ""
    if field == "amount":
        return f"{float(entry.get('amount') or 0):.2f}"
    if field == "receipt_url":
        return str((entry.get("receipt") or {}).get("url") or "")
    return str(entry.get(field) or "")


def update_expense_field(inputs: YearInputs, entry_id: str, field: str, value: str, *, year: int,
                         data_dir: Path, expected: str | None = None) -> dict:
    """ONE cell of the expenses table (the inline editor): the entry's other fields stay, the
    whole entry is validated as on add, and `expected` -- what the cell showed -- must still be
    what the entry has, or the write is refused as a conflict (as the Orders grid does). A link
    typed into the receipt cell replaces an uploaded file; a blank one leaves the receipt alone
    (a receipt is required). KeyError for an unknown id, ValueError naming what is wrong."""
    if field not in EXPENSE_CELL_FIELDS:
        raise ValueError(f"{field} is not editable")
    entry = next((e for e in inputs.expenses if e["id"] == entry_id), None)
    if entry is None:
        raise KeyError(entry_id)
    if expected is not None and expense_raw(entry, field) != expected:
        raise ValueError(f"the cell changed meanwhile: it now reads {expense_raw(entry, field)!r}; reload and try again")
    fields = {f: expense_raw(entry, f) for f in EXPENSE_CELL_FIELDS}
    fields[field] = value
    return update_expense(inputs, entry_id, fields, year=year, data_dir=data_dir)


def sort_expenses(expenses: list[dict], field: str, desc: bool = False) -> list[dict]:
    """The expenses list in the order a column header asks for: amounts as numbers, the rest as text, an
    unknown field leaving the stored order (date, then when added)."""
    if field not in EXPENSE_CELL_FIELDS:
        return list(expenses)
    if field == "amount":
        key = lambda e: float(e.get("amount") or 0)  # noqa: E731
    else:
        key = lambda e: expense_raw(e, field).lower()  # noqa: E731
    return sorted(expenses, key=key, reverse=desc)


def replace_receipt(inputs: YearInputs, entry_id: str, receipt_file: tuple[str, bytes], *, year: int,
                    data_dir: Path) -> dict:
    """The receipt cell's upload button: the uploaded file becomes the entry's receipt, the old file (if any)
    is deleted, every other field stays. KeyError for an unknown id."""
    entry = next((e for e in inputs.expenses if e["id"] == entry_id), None)
    if entry is None:
        raise KeyError(entry_id)
    fields = {f: expense_raw(entry, f) for f in EXPENSE_CELL_FIELDS}
    return update_expense(inputs, entry_id, fields, year=year, data_dir=data_dir, receipt_file=receipt_file)


def remove_expenses(inputs: YearInputs, entry_ids: Iterable[str], *, data_dir: Path) -> list[dict]:
    """Drop every listed expense (the table's selected rows, one confirmation). Unknown ids are
    skipped. Returns what went."""
    gone = []
    for entry_id in entry_ids:
        entry = remove_expense(inputs, entry_id, data_dir=data_dir)
        if entry is not None:
            gone.append(entry)
    return gone


def expense_choices(inputs: YearInputs) -> dict:
    """The previous answers per choice column of the expenses table, for the inline editor's
    dropdown (the same #cell-choices shape the Orders grid reads)."""
    values = {}
    for field in ("category", "profile", "email"):
        seen = sorted({str(e.get(field) or "").strip() for e in inputs.expenses} - {""}, key=str.lower)
        values[field] = seen
    return {"values": values, "card_pairs": []}


def remove_expense(inputs: YearInputs, entry_id: str, *, data_dir: Path) -> dict | None:
    """Drop one expense (and its uploaded receipt file). Returns it, or None if unknown."""
    for entry in inputs.expenses:
        if entry["id"] == entry_id:
            inputs.expenses.remove(entry)
            _unlink_receipt(entry.get("receipt") or {}, data_dir=data_dir)
            return entry
    return None


def receipt_path(inputs: YearInputs, entry_id: str, *, data_dir: Path) -> Path | None:
    """The stored receipt file of an expense, if it has one and it is inside data/expenses/."""
    for entry in inputs.expenses:
        if entry["id"] == entry_id:
            rel = (entry.get("receipt") or {}).get("file")
            if not rel:
                return None
            base = (Path(data_dir) / EXPENSES_DIR).resolve()
            path = (Path(data_dir) / rel).resolve()
            if base not in path.parents:
                return None
            return path if path.is_file() else None
    return None


# --------------------------------------------------------------------------------------------------
# The summary on Schedule C's lines
# --------------------------------------------------------------------------------------------------


def schedule_c(report: dict, inputs: YearInputs) -> dict:
    """The year's figures laid out on Schedule C. `report` is scripts.tax_report.build_report's."""
    t = report["totals"]
    payouts = float(t.get("payouts") or 0)
    cogs = float(t.get("cogs") or 0)
    insurance = float(t.get("insurance") or 0)
    other_income = round(inputs.program_total + inputs.site_total + inputs.bonus_total + inputs.other_income, 2)
    gross_income = round(payouts - cogs + other_income, 2)
    other_expenses = round(inputs.expense_total + inputs.other_expense, 2)
    total_expenses = round(insurance + other_expenses, 2)
    net = round(gross_income - total_expenses, 2)
    lines = [
        {"part": "I", "line": "1", "name": "Gross receipts or sales", "amount": round(payouts, 2),
         "what": f"buying-group payouts dated in {report['year']} ({t.get('payout_rows', 0)} rows)"},
        {"part": "I", "line": "4", "name": "Cost of goods sold (from Part III)", "amount": round(cogs, 2),
         "what": f"COGS of orders placed in {report['year']}: cost + shipping + tax, net of gift cards and "
                 "card cashback (Part III below)"},
        {"part": "I", "line": "6", "name": "Other income", "amount": other_income,
         "what": f"program cashback {inputs.program_total:,.2f} + cashback sites {inputs.site_total:,.2f} + "
                 f"sign-up bonuses {inputs.bonus_total:,.2f} + other income {inputs.other_income:,.2f} "
                 "(suggested placement)"},
        {"part": "I", "line": "7", "name": "Gross income", "amount": gross_income,
         "what": "line 1 − line 4 + line 6"},
        {"part": "II", "line": "15", "name": "Insurance (other than health)", "amount": round(insurance, 2),
         "what": "buying-group shipment insurance premiums"},
        {"part": "II", "line": "27a", "name": "Other expenses", "amount": other_expenses,
         "what": f"the expense list {inputs.expense_total:,.2f} ({len(inputs.expenses)} receipt(s)) -- "
                 "card annual fees go in that list, with their receipts"
                 + (f" + other expense rows {inputs.other_expense:,.2f}" if inputs.other_expense else "")},
        {"part": "II", "line": "28", "name": "Total expenses", "amount": total_expenses,
         "what": "lines 8 through 27a"},
        {"part": "II", "line": "31", "name": "Net profit or (loss)", "amount": net,
         "what": "line 7 − line 28"},
        {"part": "III", "line": "36", "name": "Purchases less cost of items withdrawn for personal use",
         "amount": round(float(t.get("gross_cost") or 0) + float(t.get("shipping") or 0)
                         + float(t.get("sales_tax") or 0) - float(t.get("returns") or 0)
                         - float(t.get("gift_card") or 0), 2),
         "what": "gross cost + shipping + sales tax − returns − gift-card tender, before cashback"},
        {"part": "III", "line": "—", "name": "Card cashback netted from cost", "amount": -round(float(t.get("cashback") or 0), 2),
         "what": "the rates on the Cards settings, applied per row; rewards spent stay in the cost"},
        {"part": "III", "line": "42", "name": "Cost of goods sold", "amount": round(cogs, 2),
         "what": "carried to line 4"},
    ]
    return {"year": report["year"], "lines": lines, "net": net, "gross_income": gross_income,
            "other_income": other_income, "other_expenses": other_expenses,
            "straddling": report.get("straddling", {}), "basis": report.get("basis", "")}
