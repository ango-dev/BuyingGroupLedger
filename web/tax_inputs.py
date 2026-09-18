"""The Taxes page's hand-entered side, and the Schedule C summary it produces.

WHAT IS ASKED, AND WHY IT IS DERIVED. The memberships are one per (profile, retailer) the config
says that profile is logged into, the sign-up bonuses one per configured card that is not
`virtual`, so the page asks for exactly what the setup implies and nothing has to be typed twice.
The cashback-site rows start from the usual names and keep whatever else was added. The answers
are stored PER TAX YEAR in data/tax_inputs.json (inside every backup, beside the ledger) -- never
in config.json, which is setup, not a year's figures.

WHAT THE SUMMARY IS. scripts/tax_report.build_report's cash-basis figures for the year (payouts by
Payout Date; COGS and insurance by Order Date) laid out on Schedule C's lines, with the hand-entered
items on the lines they most plausibly belong to. It is a summary for a preparer, not tax advice:
every line says what it holds, and the placement of the hand-entered items is a suggestion.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

#: The cashback sites the page always offers a row for (the user names the rest).
DEFAULT_SITES = ("TopCashback", "Rakuten", "ShopBack", "RetailMeNot", "Capital One Shopping")

#: Which membership a retailer login implies, in the words the user used.
MEMBERSHIPS = {
    "costco": "Costco Executive membership",
    "amazon": "Prime (Young Adult) membership",
    "amazon-business": "Prime Business Rewards membership",
}

FILE_NAME = "tax_inputs.json"


@dataclass(frozen=True)
class Prompt:
    kind: str    # "membership" | "bonus"
    key: str     # stable id the stored amount is keyed by
    label: str
    hint: str = ""


@dataclass
class YearInputs:
    memberships: dict[str, float] = field(default_factory=dict)  # prompt key -> amount
    bonuses: dict[str, float] = field(default_factory=dict)      # prompt key -> amount
    sites: dict[str, float] = field(default_factory=dict)        # site name -> amount
    other: list[dict] = field(default_factory=list)              # {label, amount, kind}
    notes: str = ""

    @property
    def membership_total(self) -> float:
        return round(sum(self.memberships.values()), 2)

    @property
    def bonus_total(self) -> float:
        return round(sum(self.bonuses.values()), 2)

    @property
    def site_total(self) -> float:
        return round(sum(self.sites.values()), 2)

    @property
    def other_income(self) -> float:
        return round(sum(float(o.get("amount") or 0) for o in self.other if o.get("kind") == "income"), 2)

    @property
    def other_expense(self) -> float:
        return round(sum(float(o.get("amount") or 0) for o in self.other if o.get("kind") != "income"), 2)

    def to_json(self) -> dict:
        return {"memberships": self.memberships, "bonuses": self.bonuses, "sites": self.sites,
                "other": self.other, "notes": self.notes}

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
        return cls(memberships=amounts("memberships"), bonuses=amounts("bonuses"),
                   sites=amounts("sites"), other=other, notes=str(payload.get("notes") or ""))


# --------------------------------------------------------------------------------------------------
# What the page asks for
# --------------------------------------------------------------------------------------------------


def membership_prompts(profiles: Iterable) -> list[Prompt]:
    """One per (profile, retailer) the config says is logged in: Costco Executive for a Costco
    login, Prime for an Amazon one, Prime Business Rewards for an Amazon Business one."""
    out = []
    for profile in profiles:
        for retailer in getattr(profile, "retailers", None) or []:
            name = MEMBERSHIPS.get(str(retailer).strip().lower())
            if name is None:
                continue
            out.append(Prompt("membership", f"membership:{profile.label}:{retailer}",
                              f"{name} — {profile.label}", "annual fee paid this year, if any"))
    return out


def bonus_prompts(cards: Iterable) -> list[Prompt]:
    """One per configured card that is not virtual (a virtual number is another card's; it earns
    no sign-up bonus of its own)."""
    out = []
    for card in cards:
        if getattr(card, "virtual", False):
            continue
        out.append(Prompt("bonus", f"bonus:{card.last4}:{card.name}",
                          f"{card.name} …{card.last4} sign-up bonus", "received this year, if any"))
    return out


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


def parse_form(form: Mapping[str, str], prompts: Iterable[Prompt]) -> YearInputs:
    """The page's form -> YearInputs. A blank amount is "none"; a bad one raises ValueError with the
    field named. Prompt amounts are read by key; sites by `site.<i>.name` / `.amount`; the open
    rows by `other.<i>.label` / `.amount` / `.kind`."""
    errors: list[str] = []
    inputs = YearInputs(notes=str(form.get("notes") or "").strip())
    for p in prompts:
        try:
            value = _amount(form.get(p.key))
        except ValueError:
            errors.append(f"{p.label}: not a number")
            continue
        if value is not None:
            (inputs.memberships if p.kind == "membership" else inputs.bonuses)[p.key] = value
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
            inputs.sites[name] = value
    for i in _indexed(form, "other"):
        label = str(form.get(f"other.{i}.label") or "").strip()
        if not label:
            continue
        try:
            value = _amount(form.get(f"other.{i}.amount")) or 0.0
        except ValueError:
            errors.append(f"{label}: not a number")
            continue
        kind = "income" if str(form.get(f"other.{i}.kind") or "") == "income" else "expense"
        inputs.other.append({"label": label, "amount": value, "kind": kind})
    if errors:
        raise ValueError("; ".join(errors))
    return inputs


def _indexed(form: Mapping[str, str], prefix: str) -> list[int]:
    indexes = set()
    for name in form.keys():
        if name.startswith(prefix + "."):
            middle = name[len(prefix) + 1:].split(".", 1)[0]
            if middle.isdigit():
                indexes.add(int(middle))
    return sorted(indexes)


# --------------------------------------------------------------------------------------------------
# The summary on Schedule C's lines
# --------------------------------------------------------------------------------------------------


def schedule_c(report: dict, inputs: YearInputs) -> dict:
    """The year's figures laid out on Schedule C. `report` is scripts.tax_report.build_report's."""
    t = report["totals"]
    payouts = float(t.get("payouts") or 0)
    cogs = float(t.get("cogs") or 0)
    insurance = float(t.get("insurance") or 0)
    other_income = round(inputs.site_total + inputs.bonus_total + inputs.other_income, 2)
    gross_income = round(payouts + other_income, 2)
    other_expenses = round(inputs.membership_total + inputs.other_expense, 2)
    total_expenses = round(insurance + other_expenses, 2)
    net = round(gross_income - cogs - total_expenses, 2)
    lines = [
        {"part": "I", "line": "1", "name": "Gross receipts or sales", "amount": round(payouts, 2),
         "what": f"buying-group payouts dated in {report['year']} ({t.get('payout_rows', 0)} rows)"},
        {"part": "I", "line": "4", "name": "Cost of goods sold (from Part III)", "amount": round(cogs, 2),
         "what": f"COGS of orders placed in {report['year']}: cost + shipping + tax, net of gift cards and "
                 "card cashback (Part III below)"},
        {"part": "I", "line": "6", "name": "Other income", "amount": other_income,
         "what": "cashback sites + card sign-up bonuses + the open income rows (suggested placement)"},
        {"part": "I", "line": "7", "name": "Gross income", "amount": round(gross_income - cogs, 2),
         "what": "line 1 − line 4 + line 6"},
        {"part": "II", "line": "15", "name": "Insurance (other than health)", "amount": round(insurance, 2),
         "what": "buying-group shipment insurance premiums"},
        {"part": "II", "line": "27a", "name": "Other expenses", "amount": other_expenses,
         "what": "memberships (Costco Executive, Prime, Prime Business Rewards) + the open expense rows"},
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
