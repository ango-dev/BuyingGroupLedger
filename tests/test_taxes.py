"""The Taxes page (web/tax_inputs.py, 2026-09-18): prompts derived from the setup and the year's
ledger rows, answers and the expense list stored per year, the year laid out on Schedule C; the
card `virtual` flag."""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from models.card import Card  # noqa: E402
from models.order import FIELDNAMES  # noqa: E402
from models.profile import ProfileConfig  # noqa: E402
from web import tax_inputs  # noqa: E402
from web.ledger_reader import LedgerRow  # noqa: E402
from web.tax_inputs import (  # noqa: E402
    YearInputs, add_expense, card_prompts, load_year, parse_form, program_prompts, receipt_path,
    remove_expense, save_year, schedule_c,
)

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def _row(**values) -> LedgerRow:
    cells = {f: str(values.get(f, "")) for f in FIELDNAMES}
    return LedgerRow(cells=cells, row_number=2)


class TestPrompts:
    def test_programs_follow_the_profiles_logins(self):
        profiles = [ProfileConfig(label="alpha", retailers=["costco", "amazon-business", "bestbuy"]),
                    ProfileConfig(label="charlie", retailers=["amazon"])]
        prompts = program_prompts(profiles)
        assert [p.label for p in prompts] == ["Costco Executive — alpha", "Prime Business — alpha",
                                              "Prime — charlie"]
        assert prompts[0].key == "program:alpha:costco" and prompts[0].kind == "program"

    def test_bonuses_are_every_card_used_in_the_year_minus_virtual_ones(self):
        rows = [
            _row(order_date="2026-03-01", card_last4="0315", card_name="USB Prime Business", status="paid"),
            _row(order_date="2026-05-01", card_last4="0315", card_name="", status="shipped"),
            _row(order_date="2026-06-01", card_last4="4444", card_name="", status="delivered"),  # not in settings
            _row(order_date="2026-07-01", card_last4="9999", card_name="Citi virtual", status="paid"),
            _row(order_date="2025-12-01", card_last4="5555", card_name="Old", status="paid"),         # another year
            _row(order_date="2026-08-01", card_last4="6666", card_name="Cancelled", status="cancelled"),
            _row(order_date="2026-08-02", card_last4="", card_name="", status="paid"),
        ]
        cards = [Card(last4="0315", name="USB Prime Business"), Card(last4="9999", name="Citi", virtual=True),
                 Card(last4="4444", name="From settings")]
        prompts = card_prompts(rows, 2026, cards)
        assert [p.label for p in prompts] == ["From settings …4444", "USB Prime Business …0315"]
        assert prompts[1].key == "card:0315" and prompts[1].last4 == "0315" and prompts[1].kind == "card"
        # with no settings at all the ledger alone decides, and an unnamed card is still asked for
        assert [p.label for p in card_prompts(rows, 2026)] == [
            "Card …4444", "Citi virtual …9999", "USB Prime Business …0315"]


class TestStorage:
    def test_round_trip_per_year(self, tmp_path):
        path = tmp_path / "data" / "tax_inputs.json"
        inputs = YearInputs(programs={"program:a:costco": 130.0}, bonuses={"bonus:0315": 200.0},
                            fees={"fee:0315": 95.0},
                            sites={"Rakuten": 55.5}, other=[{"label": "refund", "amount": 3.0, "kind": "income"}],
                            expenses=[{"id": "abc", "date": "2026-02-01", "description": "boxes", "amount": 12.0,
                                       "category": "", "profile": "alpha", "email": "a@b.co",
                                       "receipt": {"url": "https://x"}, "added_at": "2026-09-18"}],
                            notes="hi")
        save_year(path, 2026, inputs)
        save_year(path, 2025, YearInputs(sites={"TopCashback": 1.0}))
        assert load_year(path, 2026) == inputs and load_year(path, 2025).sites == {"TopCashback": 1.0}
        assert load_year(path, 2024) == YearInputs()
        assert set(json.loads(path.read_text(encoding="utf-8"))) == {"2025", "2026"}

    def test_a_damaged_file_or_entry_reads_as_empty(self, tmp_path):
        path = tmp_path / "tax_inputs.json"
        path.write_text("not json", encoding="utf-8")
        assert load_year(path, 2026) == YearInputs()
        path.write_text(json.dumps({"2026": {"sites": {"Rakuten": "lots"}, "other": [{"amount": 1}],
                                             "expenses": [{"amount": 5}]}}), encoding="utf-8")
        assert load_year(path, 2026) == YearInputs()

    def test_parse_form(self):
        prompts = [tax_inputs.Prompt("program", "program:a:costco", "Costco Executive — a"),
                   tax_inputs.Prompt("card", "card:0315", "USB …0315")]
        programs, bonuses, fees, sites, other, notes = parse_form(
            {"program:a:costco": "$130.00", "bonus:0315": "", "fee:0315": "95", "site.0.name": "Rakuten",
             "site.0.amount": "55.50", "site.1.name": "", "site.1.amount": "9", "other.0.label": "refund",
             "other.0.amount": "3", "other.1.label": "", "notes": " n "}, prompts)
        assert programs == {"program:a:costco": 130.0} and bonuses == {} and fees == {"fee:0315": 95.0}
        assert sites == {"Rakuten": 55.5} and notes == "n"
        assert other == [{"label": "refund", "amount": 3.0, "kind": "income"}]  # income only
        with pytest.raises(ValueError, match="Costco Executive — a: not a number"):
            parse_form({"program:a:costco": "lots"}, prompts)
        with pytest.raises(ValueError, match="USB …0315 annual fee: not a number"):
            parse_form({"fee:0315": "lots"}, prompts)


class TestExpenses:
    FIELDS = {"date": "2026-03-04", "description": "boxes", "amount": "12.50", "profile": "alpha",
              "email": "a@b.co", "category": "supplies"}

    def test_every_field_is_required_and_the_date_must_be_in_the_year(self, tmp_path):
        inputs = YearInputs()
        with pytest.raises(ValueError) as exc:
            add_expense(inputs, {}, year=2026, data_dir=tmp_path)
        text = str(exc.value)
        for needle in ("Date must be written", "Description is required", "Amount is required",
                       "Profile is required", "Email is required", "A receipt is required"):
            assert needle in text
        with pytest.raises(ValueError, match="fall in 2026"):
            add_expense(inputs, {**self.FIELDS, "date": "2025-03-04", "receipt_url": "https://x"},
                        year=2026, data_dir=tmp_path)
        assert inputs.expenses == []

    def test_an_upload_is_kept_beside_the_ledger_and_a_link_is_kept_as_is(self, tmp_path):
        inputs = YearInputs()
        entry = add_expense(inputs, self.FIELDS, year=2026, data_dir=tmp_path,
                            receipt_file=("my receipt (1).pdf", b"%PDF-1.4 x"))
        assert entry["receipt"]["file"].startswith("expenses/2026/") and entry["receipt"]["name"] == "my_receipt_1.pdf"
        assert (tmp_path / entry["receipt"]["file"]).read_bytes() == b"%PDF-1.4 x"
        assert receipt_path(inputs, entry["id"], data_dir=tmp_path) == (tmp_path / entry["receipt"]["file"]).resolve()
        linked = add_expense(inputs, {**self.FIELDS, "receipt_url": "https://drive/x"}, year=2026, data_dir=tmp_path)
        assert linked["receipt"] == {"url": "https://drive/x"} and receipt_path(inputs, linked["id"], data_dir=tmp_path) is None
        assert inputs.expense_total == 25.0
        removed = remove_expense(inputs, entry["id"], data_dir=tmp_path)
        assert removed is entry and not (tmp_path / entry["receipt"]["file"]).exists()
        assert remove_expense(inputs, "nope", data_dir=tmp_path) is None and len(inputs.expenses) == 1


class TestScheduleC:
    def test_the_lines_add_up(self):
        report = {"year": 2026, "basis": "cash", "straddling": {},
                  "totals": {"payouts": 1000.0, "payout_rows": 3, "cogs": 700.0, "insurance": 10.0,
                             "gross_cost": 720.0, "shipping": 5.0, "sales_tax": 0.0, "returns": 0.0,
                             "gift_card": 0.0, "cashback": 25.0}}
        inputs = YearInputs(programs={"p": 30.0}, bonuses={"b": 200.0}, fees={"f": 5.0}, sites={"Rakuten": 50.0},
                            expenses=[{"id": "1", "date": "2026-01-01", "description": "boxes", "amount": 12.0,
                                       "category": "", "profile": "a", "email": "a@b.co", "receipt": {}, "added_at": ""}],
                            other=[{"label": "fee", "amount": 8.0, "kind": "expense"},
                                   {"label": "refund", "amount": 3.0, "kind": "income"}])
        s = schedule_c(report, inputs)
        by = {(l["part"], l["line"]): l["amount"] for l in s["lines"]}
        assert by[("I", "1")] == 1000.0 and by[("I", "4")] == 700.0
        assert by[("I", "6")] == 283.0 and s["other_income"] == 283.0      # 30 + 50 + 200 + 3
        assert by[("I", "7")] == 583.0                                       # 1000 - 700 + 283
        assert by[("II", "15")] == 10.0 and by[("II", "27a")] == 25.0       # 12 + 5 + 8 (legacy expense row)
        assert by[("II", "28")] == 35.0 and by[("II", "31")] == 548.0 and s["net"] == 548.0
        assert by[("III", "36")] == 725.0 and by[("III", "—")] == -25.0 and by[("III", "42")] == 700.0


# --------------------------------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, config_file):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import test_web as tw

    from config.settings import settings
    from web.app import create_app
    from web.ledger_reader import SnapshotReader

    config_file(profiles=[{"label": "alpha", "profile_id": "", "retailers": ["costco", "amazon-business"]}],
                cards=[{"last4": "0315", "name": "USB Prime Business", "cashback_rate": 0.05},
                       {"last4": "4351", "name": "Venmo Visa", "cashback_rate": 0.09, "virtual": True}])
    snap = tw.write_snapshot(tmp_path / "ledger_backup_20260918T000000Z.csv", *tw.LEDGER_ROWS)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "failures").mkdir()
    app = create_app(SnapshotReader(snap), logs_dir=logs, failures_dir=logs / "failures",
                     repo_root_dir=tmp_path, backup_dir=tmp_path / "backups", clock=lambda: NOW,
                     settings=dataclasses.replace(settings, container_run_interval_hours=6))
    return TestClient(app)


class TestTaxesPage:
    def test_the_summary_and_the_prompts(self, client):
        body = client.get("/taxes", params={"year": "2026"}).text
        assert "<h1>Taxes</h1>" in body and "Schedule C Summary — 2026" in body
        assert "Gross receipts or sales" in body and "$500.00" in body  # row 5: a dated payout in 2026
        assert "Costco Executive — alpha" in body and "Prime Business — alpha" in body
        # the year's cards: 0315 (rows 2, 9), 4331 (rows 3, 4); 4351 is virtual in the settings; row 8 has none
        assert "USB Prime Business …0315" in body and "Amex Business Gold …4331" in body
        assert 'name="bonus:0315"' in body and 'name="fee:0315"' in body and "Sign-up Bonus" in body
        assert "…4351" not in body
        assert 'name="site.0.name" value="TopCashback"' in body
        assert "No expenses entered for 2026 yet." in body and 'action="/taxes/expense?year=2026"' in body
        assert client.get("/taxes").status_code == 200  # this year by default
        assert 'href="/expenses"' not in client.get("/").text and 'href="/taxes"' in client.get("/").text

    def test_saving_the_amounts_persists_and_shows_on_the_summary(self, client, tmp_path):
        response = client.post("/taxes/save", data={
            "year": "2026", "program:alpha:costco": "30", "bonus:0315": "200", "fee:4331": "95",
            "site.0.name": "TopCashback", "site.0.amount": "40", "site.5.name": "Honey", "site.5.amount": "2.5",
            "other.0.label": "refund", "other.0.amount": "8", "notes": "for Pat",
        }, follow_redirects=False)
        assert response.status_code == 303 and "/taxes?year=2026" in response.headers["location"]
        saved = json.loads((tmp_path / "data" / "tax_inputs.json").read_text(encoding="utf-8"))["2026"]
        assert saved["programs"] == {"program:alpha:costco": 30.0} and saved["sites"] == {"TopCashback": 40.0, "Honey": 2.5}
        body = client.get("/taxes", params={"year": "2026"}).text
        assert 'value="30.0"' in body and 'name="site.5.name" value="Honey"' in body and "for Pat" in body
        assert "$280.50" in body   # line 6: 30 + 40 + 2.5 + 200 + 8
        assert "$95.00" in body    # line 27a: the annual fee
        bad = client.post("/taxes/save", data={"year": "2026", "program:alpha:costco": "lots"})
        assert bad.status_code == 200 and "Costco Executive — alpha: not a number" in bad.text
        assert json.loads((tmp_path / "data" / "tax_inputs.json").read_text(encoding="utf-8"))["2026"]["programs"] == {"program:alpha:costco": 30.0}

    def test_an_expense_with_an_uploaded_receipt(self, client, tmp_path):
        response = client.post("/taxes/expense", params={"year": "2026"},
                               data={"date": "2026-03-04", "description": "boxes", "amount": "12.50",
                                     "profile": "alpha", "email": "a@b.co", "category": "supplies"},
                               files={"receipt_file": ("receipt.pdf", b"%PDF-1.4 x", "application/pdf")},
                               follow_redirects=False)
        assert response.status_code == 303 and "Added" in response.headers["location"]
        saved = json.loads((tmp_path / "data" / "tax_inputs.json").read_text(encoding="utf-8"))["2026"]["expenses"]
        assert len(saved) == 1 and saved[0]["receipt"]["file"].startswith("expenses/2026/")
        entry_id = saved[0]["id"]
        body = client.get("/taxes", params={"year": "2026"}).text
        assert "boxes" in body and "$12.50" in body and f'href="/taxes/receipt/{entry_id}?year=2026"' in body
        assert "the expense list 12.50 (1 receipt(s))" in body
        served = client.get(f"/taxes/receipt/{entry_id}", params={"year": "2026"})
        assert served.status_code == 200 and served.content == b"%PDF-1.4 x"
        # the amounts form saves without touching the list
        client.post("/taxes/save", data={"year": "2026", "notes": "x"})
        assert len(json.loads((tmp_path / "data" / "tax_inputs.json").read_text(encoding="utf-8"))["2026"]["expenses"]) == 1
        gone = client.post(f"/taxes/expense/{entry_id}/delete", params={"year": "2026"}, follow_redirects=False)
        assert gone.status_code == 303
        assert json.loads((tmp_path / "data" / "tax_inputs.json").read_text(encoding="utf-8"))["2026"]["expenses"] == []
        assert not list((tmp_path / "data" / "expenses" / "2026").glob("*"))
        assert client.get(f"/taxes/receipt/{entry_id}", params={"year": "2026"}).status_code == 404

    def test_an_expense_missing_its_receipt_or_email_is_refused_with_the_draft_kept(self, client):
        response = client.post("/taxes/expense", params={"year": "2026"},
                               data={"date": "2026-03-04", "description": "boxes", "amount": "12.50",
                                     "profile": "alpha", "email": "not-an-email"})
        assert response.status_code == 200
        assert "A receipt is required" in response.text and "Email is required" in response.text
        assert 'value="boxes"' in response.text  # the draft survives

    def test_it_refuses_the_wrong_methods(self, client):
        assert client.put("/taxes").status_code == 405


class TestVirtualCardOnTheSettingsPage:
    def test_the_flag_round_trips_through_the_entry_card(self, config_file):
        from web import settings_form

        config_file(cards=[{"last4": "0315", "name": "USB Prime Business", "cashback_rate": 0.05}])
        settings_form.apply_entry("cards", 0, {"last4": "0315", "name": "USB Prime Business",
                                               "cashback_rate": "5%", "virtual": "on"})
        assert settings_form.display_entries("cards")[0]["virtual"] is True
        settings_form.apply_entry("cards", 0, {"last4": "0315", "name": "USB Prime Business",
                                               "cashback_rate": "5%"})
        assert settings_form.display_entries("cards")[0]["virtual"] is False
        from config.loader import load_config

        assert "virtual" not in load_config()["cards"][0]
