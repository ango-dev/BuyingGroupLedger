"""The Taxes and Expenses pages (web/tax_inputs.py, 2026-09-18): prompts derived from the setup,
answers stored per year, the year laid out on Schedule C; the card `virtual` flag."""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from models.card import Card  # noqa: E402
from models.profile import ProfileConfig  # noqa: E402
from web import tax_inputs  # noqa: E402
from web.tax_inputs import (  # noqa: E402
    YearInputs, bonus_prompts, load_year, membership_prompts, parse_form, save_year, schedule_c,
)

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


class TestPrompts:
    def test_memberships_follow_the_profiles_logins(self):
        profiles = [ProfileConfig(label="alpha", retailers=["costco", "amazon-business", "bestbuy"]),
                    ProfileConfig(label="charlie", retailers=["amazon"])]
        prompts = membership_prompts(profiles)
        assert [p.label for p in prompts] == ["Costco Executive membership — alpha",
                                              "Prime Business Rewards membership — alpha",
                                              "Prime (Young Adult) membership — charlie"]
        assert prompts[0].key == "membership:alpha:costco" and prompts[0].kind == "membership"

    def test_bonuses_skip_virtual_cards(self):
        cards = [Card(last4="0315", name="USB Prime Business", cashback_rate=0.05),
                 Card(last4="9999", name="Citi (virtual)", cashback_rate=0.02, virtual=True)]
        prompts = bonus_prompts(cards)
        assert [p.label for p in prompts] == ["USB Prime Business …0315 sign-up bonus"]
        assert prompts[0].key == "bonus:0315:USB Prime Business"


class TestStorage:
    def test_round_trip_per_year(self, tmp_path):
        path = tmp_path / "data" / "tax_inputs.json"
        inputs = YearInputs(memberships={"membership:a:costco": 130.0}, bonuses={"bonus:1:x": 200.0},
                            sites={"Rakuten": 55.5}, other=[{"label": "boxes", "amount": 12.0, "kind": "expense"}],
                            notes="hi")
        save_year(path, 2026, inputs)
        save_year(path, 2025, YearInputs(sites={"TopCashback": 1.0}))
        assert load_year(path, 2026) == inputs and load_year(path, 2025).sites == {"TopCashback": 1.0}
        assert load_year(path, 2024) == YearInputs()
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert set(raw) == {"2025", "2026"}

    def test_a_damaged_file_or_entry_reads_as_empty(self, tmp_path):
        path = tmp_path / "tax_inputs.json"
        path.write_text("not json", encoding="utf-8")
        assert load_year(path, 2026) == YearInputs()
        path.write_text(json.dumps({"2026": {"sites": {"Rakuten": "lots"}, "other": [{"amount": 1}]}}), encoding="utf-8")
        assert load_year(path, 2026) == YearInputs()

    def test_parse_form(self):
        prompts = [tax_inputs.Prompt("membership", "membership:a:costco", "Costco"),
                   tax_inputs.Prompt("bonus", "bonus:1:x", "x")]
        inputs = parse_form({"membership:a:costco": "$130.00", "bonus:1:x": "", "site.0.name": "Rakuten",
                             "site.0.amount": "55.50", "site.1.name": "", "site.1.amount": "9",
                             "other.0.label": "boxes", "other.0.amount": "12", "other.0.kind": "expense",
                             "other.1.label": "refund", "other.1.amount": "3", "other.1.kind": "income",
                             "other.2.label": "", "notes": " n "}, prompts)
        assert inputs.memberships == {"membership:a:costco": 130.0} and inputs.bonuses == {}
        assert inputs.sites == {"Rakuten": 55.5}
        assert inputs.other == [{"label": "boxes", "amount": 12.0, "kind": "expense"},
                                {"label": "refund", "amount": 3.0, "kind": "income"}]
        assert inputs.notes == "n" and inputs.other_income == 3.0 and inputs.other_expense == 12.0
        with pytest.raises(ValueError, match="Costco: not a number"):
            parse_form({"membership:a:costco": "lots"}, prompts)


class TestScheduleC:
    def test_the_lines_add_up(self):
        report = {"year": 2026, "basis": "cash", "straddling": {},
                  "totals": {"payouts": 1000.0, "payout_rows": 3, "cogs": 700.0, "insurance": 10.0,
                             "gross_cost": 720.0, "shipping": 5.0, "sales_tax": 0.0, "returns": 0.0,
                             "gift_card": 0.0, "cashback": 25.0}}
        inputs = YearInputs(memberships={"m": 130.0}, bonuses={"b": 200.0}, sites={"Rakuten": 50.0},
                            other=[{"label": "boxes", "amount": 12.0, "kind": "expense"},
                                   {"label": "refund", "amount": 3.0, "kind": "income"}])
        s = schedule_c(report, inputs)
        by = {(l["part"], l["line"]): l["amount"] for l in s["lines"]}
        assert by[("I", "1")] == 1000.0 and by[("I", "4")] == 700.0
        assert by[("I", "6")] == 253.0 and s["other_income"] == 253.0      # 50 + 200 + 3
        assert by[("I", "7")] == 553.0                                       # 1000 - 700 + 253
        assert by[("II", "15")] == 10.0 and by[("II", "27a")] == 142.0      # 130 + 12
        assert by[("II", "28")] == 152.0 and by[("II", "31")] == 401.0 and s["net"] == 401.0
        assert by[("III", "36")] == 725.0 and by[("III", "—")] == -25.0 and by[("III", "42")] == 700.0


# --------------------------------------------------------------------------------------------------
# The pages
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
                       {"last4": "9999", "name": "Virtual", "cashback_rate": 0.02, "virtual": True}])
    snap = tw.write_snapshot(tmp_path / "sheet_backup_20260918T000000Z.csv", *tw.LEDGER_ROWS)
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
        assert "Gross receipts or sales" in body and "$500.00" in body  # row 5: paid with a Payout Date in 2026 (row 6, MOD, carries none)
        assert "Costco Executive membership — alpha" in body and "Prime Business Rewards membership — alpha" in body
        assert "USB Prime Business …0315 sign-up bonus" in body and "Virtual …9999" not in body
        assert 'name="site.0.name" value="TopCashback"' in body
        assert 'href="/taxes?year=2025"' in body or 'href="/taxes?year=2026"' in body
        assert client.get("/taxes").status_code == 200  # this year by default

    def test_saving_persists_and_shows_on_the_summary(self, client, tmp_path):
        response = client.post("/taxes/save", data={
            "year": "2026", "membership:alpha:costco": "130", "bonus:0315:USB Prime Business": "200",
            "site.0.name": "TopCashback", "site.0.amount": "40", "site.5.name": "Honey", "site.5.amount": "2.5",
            "other.0.label": "boxes", "other.0.amount": "12", "other.0.kind": "expense", "notes": "for Pat",
        }, follow_redirects=False)
        assert response.status_code == 303 and "/taxes?year=2026" in response.headers["location"]
        saved = json.loads((tmp_path / "data" / "tax_inputs.json").read_text(encoding="utf-8"))
        assert saved["2026"]["memberships"] == {"membership:alpha:costco": 130.0}
        assert saved["2026"]["sites"] == {"TopCashback": 40.0, "Honey": 2.5}
        body = client.get("/taxes", params={"year": "2026"}).text
        assert 'value="130.0"' in body and 'name="site.5.name" value="Honey"' in body
        assert "$242.50" in body   # line 6: 40 + 2.5 + 200
        assert "$142.00" in body   # line 27a: 130 + 12
        assert "for Pat" in body
        # a bad amount is refused with the field named, nothing written
        bad = client.post("/taxes/save", data={"year": "2026", "membership:alpha:costco": "lots"})
        assert bad.status_code == 200 and "Costco Executive membership — alpha: not a number" in bad.text
        assert json.loads((tmp_path / "data" / "tax_inputs.json").read_text(encoding="utf-8"))["2026"]["memberships"] == {"membership:alpha:costco": 130.0}

    def test_it_is_in_the_nav_and_refuses_the_wrong_methods(self, client):
        body = client.get("/").text
        assert 'href="/taxes"' in body and 'href="/expenses"' in body
        assert client.put("/taxes").status_code == 405


class TestExpensesPage:
    def test_the_years_cost_rows_in_the_orders_view(self, client):
        body = client.get("/expenses", params={"year": "2026"}).text
        assert "<h1>Expenses</h1>" in body and 'data-param="year"' in body
        table = body.split('id="orders-table"')[1]
        # placed in 2026 and carrying money: rows 2-6, 8, 9 (cancelled 7 and superseded 10 are not)
        assert table.count('<tr class="status-') == 7
        assert "1399000019" not in table and "111-0000004-0000004" not in table
        assert "Schedule C line 4" in body and 'href="/taxes?year=2026"' in body
        empty = client.get("/expenses", params={"year": "2024"}).text
        assert "Nothing to show." in empty
        # the year rides on the sort links
        assert "year=2026" in table.split("Total Cost")[0][-400:]


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
