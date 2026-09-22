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
        assert [p.label for p in prompts] == ["Costco Executive Cashback — alpha", "Prime Business Rewards — alpha",
                                              "Prime Young Adult Cashback — charlie"]  # the programs' own names
        assert all(p.hint == "" for p in prompts)  # no "cashback the program paid this year" beside the row
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
                 Card(last4="7777", name="Amex employee", virtual_of="0315", own_bonus=True),  # asked: its own bonus
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
                            sites={"Rakuten": 55.5}, other=[{"label": "refund", "amount": 3.0, "kind": "income", "entries": []}],
                            expenses=[{"id": "abc", "date": "2026-02-01", "description": "boxes", "amount": 12.0,
                                       "category": "", "profile": "alpha", "email": "a@b.co",
                                       "receipt": {"url": "https://x"}, "added_at": "2026-09-18"}],
                            notes="hi")
        save_year(path, 2026, inputs)
        save_year(path, 2025, YearInputs(sites={"TopCashback": 1.0}))
        assert load_year(path, 2026) == inputs and load_year(path, 2025).sites == {"TopCashback": 1.0}
        assert load_year(path, 2024) == YearInputs()
        assert set(json.loads(path.read_text(encoding="utf-8"))) == {"2025", "2026"}
        logged = YearInputs(programs={"p": 42.5}, program_entries={"p": [{"date": "2026-01-01", "amount": 30.0, "note": ""},
                                                                          {"date": "2026-04-02", "amount": 12.5, "note": "Q1"}]},
                            sites={"Rakuten": 5.0}, site_entries={"Rakuten": [{"date": "2026-02-02", "amount": 5.0, "note": ""}]})
        save_year(path, 2027, logged)
        assert load_year(path, 2027) == logged
        programs, sites, _bonuses = tax_inputs.log_entries(load_year(path, 2027), 2027)
        assert programs["p"][0]["date"] == "2026-04-02" and sites["Rakuten"][0]["amount"] == 5
        programs, sites, bonuses = tax_inputs.log_entries(YearInputs(programs={"q": 7.0}, bonuses={"bonus:1": 2.0}), 2025)  # an older single amount
        assert bonuses == {"bonus:1": [{"date": "2025-01-01", "amount": 2, "note": "", "year": "2025", "month": "2025-01", "month_label": "January 2025"}]}
        assert [(e["date"], e["amount"], e["month_label"]) for e in programs["q"]] == [("2025-01-01", 7, "January 2025")] and sites == {}

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
        programs, bonuses, sites, other, notes = parse_form(
            {"program:a:costco": "$130.00", "bonus:0315": "", "fee:0315": "95", "site.0.name": "Rakuten",
             "site.0.amount": "55.50", "site.1.name": "", "site.1.amount": "9", "other.0.label": "refund",
             "other.0.amount": "3", "other.1.label": "", "notes": " n "}, prompts)
        assert programs == {"program:a:costco": 130.0} and bonuses == {}  # fee:0315 is ignored: a fee is an expense
        assert sites == {"Rakuten": 55.5} and notes == "n"
        # the dated log's rows, in place of a plain amount
        programs, _b, sites, _o, _n = parse_form(
            {"program:a:costco.0.date": "2026-01-01", "program:a:costco.0.amount": "10", "program:a:costco.0.remove": "on",
             "program:a:costco.new.date": "2026-05-05", "program:a:costco.new.amount": "5",
             "site.0.name": "Rakuten", "site.0.amount.new.amount": "2"}, prompts, today="2026-09-18")
        assert programs == {"program:a:costco": 5.0} and sites == {"Rakuten": 2.0}
        assert other == [{"label": "refund", "amount": 3.0, "kind": "income", "entries": []}]  # income only
        with pytest.raises(ValueError, match="Costco Executive — a: not a number"):
            parse_form({"program:a:costco": "lots"}, prompts)
        with pytest.raises(ValueError, match="USB …0315 sign-up bonus: not a number"):
            parse_form({"bonus:0315": "lots"}, prompts)
        # an older year file's fees section is dropped on read
        legacy = YearInputs.from_json({"bonuses": {"bonus:0315": 1}, "fees": {"fee:0315": 95}})
        assert legacy.bonuses == {"bonus:0315": 1.0} and not hasattr(legacy, "fees") and "fees" not in legacy.to_json()


class TestExpenses:
    FIELDS = {"date": "2026-03-04", "description": "boxes", "amount": "12.50", "profile": "alpha",
              "email": "a@b.co", "category": "supplies"}

    def test_every_field_is_required_and_the_date_must_be_in_the_year(self, tmp_path):
        inputs = YearInputs()
        with pytest.raises(ValueError) as exc:
            add_expense(inputs, {}, year=2026, data_dir=tmp_path)
        text = str(exc.value)
        for needle in ("Date must be written", "Description is required", "Amount is required",
                       "Who paid is required: the profile, or the email of the account", "A receipt is required"):
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
        inputs = YearInputs(programs={"p": 30.0}, bonuses={"b": 200.0}, sites={"Rakuten": 50.0},
                            expenses=[{"id": "1", "date": "2026-01-01", "description": "boxes", "amount": 12.0,
                                       "category": "", "profile": "a", "email": "a@b.co", "receipt": {}, "added_at": ""}],
                            other=[{"label": "fee", "amount": 8.0, "kind": "expense"},
                                   {"label": "refund", "amount": 3.0, "kind": "income"}])
        s = schedule_c(report, inputs)
        by = {(l["part"], l["line"]): l["amount"] for l in s["lines"]}
        assert by[("I", "1")] == 1000.0 and by[("I", "4")] == 700.0
        assert by[("I", "6")] == 283.0 and s["other_income"] == 283.0      # 30 + 50 + 200 + 3
        assert by[("I", "7")] == 583.0                                       # 1000 - 700 + 283
        assert by[("II", "15")] == 10.0 and by[("II", "27a")] == 20.0       # 12 + 8 (legacy expense row)
        assert by[("II", "28")] == 30.0 and by[("II", "31")] == 553.0 and s["net"] == 553.0
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
                     settings=dataclasses.replace(settings, container_run_interval_hours=6, web_password=""))
    return TestClient(app)


class TestTaxesPage:
    def test_the_summary_and_the_prompts(self, client):
        body = client.get("/taxes", params={"year": "2026"}).text
        assert "<h1>Taxes</h1>" in body and "Schedule C Summary — 2026" in body
        # every panel folds, open by default
        assert body.count('<details class="panel"') == 6 and body.count('<details class="panel" id="s-') == 6
        assert 'id="s-expenses" open>' in body and "<summary><h2>Expenses" in body and "<section" not in body
        assert "Gross receipts or sales" in body and "$500.00" in body  # row 5: a dated payout in 2026
        assert "Costco Executive Cashback — alpha" in body and "Prime Business Rewards — alpha" in body
        assert "cashback the program paid this year" not in body
        # the year's cards: 0315 (rows 2, 9), 4331 (rows 3, 4); 4351 is virtual in the settings; row 8 has none
        assert "USB Prime Business …0315" in body and "Amex Business Gold …4331" in body
        assert 'data-log="bonus:0315"' in body and "Sign-up Bonus" in body  # the bonus is a dated log (2026-09-20)
        assert 'name="fee:0315"' not in body and "Annual Fee" not in body  # a fee is an expense, with a receipt
        assert "Annual fees go in Expenses" in body  # the panel says where they go (shortened 2026-09-19)
        # the Add-an-Expense form is one four-column grid
        assert body.count('class="egrid"') == 1 and body.count('class="span-2"') == 3 and 'class="lbl span-2"' in body
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
        # the amounts are dated logs now: a plain total shows as one entry dated the year's first day
        assert 'name="program:alpha:costco.0.date" value="2026-01-01"' in body and 'name="program:alpha:costco.0.amount" value="30"' in body
        assert 'name="site.5.name" value="Honey"' in body and "for Pat" in body
        assert "$280.50" in body   # line 6: 30 + 40 + 2.5 + 200 + 8
        assert "$95.00" not in body and "fees" not in saved  # fee:4331 was ignored: a fee is an expense
        bad = client.post("/taxes/save", data={"year": "2026", "program:alpha:costco": "lots"})
        assert bad.status_code == 200 and "Costco Executive Cashback — alpha: not a number" in bad.text
        assert json.loads((tmp_path / "data" / "tax_inputs.json").read_text(encoding="utf-8"))["2026"]["programs"] == {"program:alpha:costco": 30.0}

    def test_the_inputs_save_in_place(self, client, tmp_path):
        """an htmx
        post answers with the form, the summary out of band and a toast; a refusal the same, 400."""
        response = client.post("/taxes/save", data={"year": "2026", "program:alpha:costco": "30"}, headers={"HX-Request": "true"})
        assert response.status_code == 200
        body = response.text
        assert body.startswith('<form method="post" action="/taxes/save" class="tax-form" id="tax-form" hx-post="/taxes/save"') or 'id="tax-form" hx-post="/taxes/save" hx-target="#tax-form" hx-swap="outerHTML"' in body
        assert '<details class="panel" id="s-schedule-c" open hx-swap-oob="true">' in body and "$30.00" in body
        # innerHTML swap: the announcement lands INSIDE the page's persistent live region (2026-09-21)
        assert '<div id="toast" hx-swap-oob="innerHTML"><div class="toast ok" role="status">Saved 2026</div></div>' in body
        assert '<div class="savebar fixed tax-savebar">' in body and 'id="tax-dirty">No unsaved changes</span>' in body  # the bar follows the page (2026-09-21)
        assert "<html" not in body and json.loads((tmp_path / "data" / "tax_inputs.json").read_text(encoding="utf-8"))["2026"]["programs"] == {"program:alpha:costco": 30.0}
        refused = client.post("/taxes/save", data={"year": "2026", "program:alpha:costco": "lots"}, headers={"HX-Request": "true"})
        assert refused.status_code == 400 and "Nothing was saved: Costco Executive Cashback — alpha: not a number" in refused.text
        page = client.get("/taxes", params={"year": "2026"}).text  # the page itself: one summary, one form, a toast slot
        assert page.count('id="s-schedule-c"') == 1 and page.count('id="tax-form"') == 1 and '<div id="toast" role="status" aria-live="polite"></div>' in page and 'hx-swap-oob' not in page

    def test_the_year_downloads_as_one_organised_zip(self, client, tmp_path):
        """The Schedule C lines, both order bases, the expenses with their uploaded
        receipts, every dated income entry, the notes, and a README that says what is NOT here."""
        import io
        import zipfile

        r = client.post("/taxes/expense", data={"year": "2026", "date": "2026-03-03", "description": "shipping boxes",
                                                "amount": "12.50", "profile": "alpha", "email": "", "category": "supplies"},
                        files={"receipt_file": ("boxes.pdf", b"%PDF-1.4 x", "application/pdf")}, follow_redirects=False)
        assert r.status_code == 303, r.text[:200]
        client.post("/taxes/save", data={"year": "2026", "program:alpha:costco": "30", "site.0.name": "TopCashback",
                                         "site.0.amount": "40", "other.0.label": "refund", "other.0.amount": "8",
                                         "notes": "for Pat"}, follow_redirects=False)
        response = client.get("/taxes/export", params={"year": "2026"})
        assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
        assert response.headers["content-disposition"] == 'attachment; filename="tax_2026.zip"'
        zf = zipfile.ZipFile(io.BytesIO(response.content))
        names = zf.namelist()
        for expected in ("tax_2026/README.txt", "tax_2026/schedule_c.csv", "tax_2026/orders_placed_2026.csv",
                         "tax_2026/orders_paid_out_2026.csv", "tax_2026/expenses.csv", "tax_2026/income.csv",
                         "tax_2026/notes.txt"):
            assert expected in names, names
        receipts = [n for n in names if n.startswith("tax_2026/expense_receipts/")]
        assert len(receipts) == 1 and receipts[0].endswith("boxes.pdf") and zf.read(receipts[0]) == b"%PDF-1.4 x"
        placed = zf.read("tax_2026/orders_placed_2026.csv").decode("utf-8").splitlines()
        assert len(placed) == 1 + 9 and placed[0].startswith("Order Date,")  # every fixture row was placed in 2026
        paid = zf.read("tax_2026/orders_paid_out_2026.csv").decode("utf-8").splitlines()
        assert len(paid) == 2 and "1399000017" in paid[1]  # the one payout dated in 2026
        assert "Gross receipts or sales,500.00" in zf.read("tax_2026/schedule_c.csv").decode("utf-8")
        expenses = zf.read("tax_2026/expenses.csv").decode("utf-8")
        assert "2026-03-03,shipping boxes,supplies,alpha,12.50,expense_receipts/" in expenses
        income = zf.read("tax_2026/income.csv").decode("utf-8")
        assert "Program cashback,Costco Executive Cashback \u2014 alpha,2026-01-01,30.00" in income
        assert "Cashback site,TopCashback," in income and "Other income,refund," in income
        assert zf.read("tax_2026/notes.txt").decode("utf-8") == "for Pat\n"
        readme = zf.read("tax_2026/README.txt").decode("utf-8")
        assert "tax year 2026" in readme and "(9 rows)" in readme and "(1 rows)" in readme
        assert "web links, not files on this machine" in readme and "BBY01-800000000001: https://" in readme
        assert "Orders with no receipt recorded" in readme and "1399000018 (paid)" in readme
        assert client.get("/taxes/export", params={"year": "2031"}).status_code == 200  # an empty year still zips

    def test_the_bundle_packs_a_receipt_held_on_disk_and_names_a_missing_one(self, tmp_path):
        import io
        import zipfile
        from datetime import datetime, timezone

        from web import export

        on_disk = tmp_path / "costco" / "2026-08" / "1399000017.pdf"
        on_disk.parent.mkdir(parents=True)
        on_disk.write_bytes(b"%PDF-1.4 receipt")
        rows = [_row(order_id="1399000017", order_date="2026-08-20", payout_date="2026-09-01", status="paid",
                     receipt_url="/receipts/costco/2026-08/1399000017.pdf"),
                _row(order_id="111-1", order_date="2026-08-01", status="delivered",
                     receipt_url="/receipts/amazon/2026-08/111-1.pdf")]  # named, not on disk
        data = export.year_bundle(
            2026, rows=rows, cell=lambda row, name: row.text(name), summary={"lines": [], "straddling": {}},
            inputs=YearInputs(), labels={}, program_logs={}, site_logs={}, bonus_logs={}, other_logs=[],
            expense_file=lambda entry_id: None,
            receipt_file=lambda rel: tmp_path / rel if (tmp_path / rel).is_file() else None,
            generated_at=datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc))
        zf = zipfile.ZipFile(io.BytesIO(data))
        assert zf.read("tax_2026/order_receipts/costco/2026-08/1399000017.pdf") == b"%PDF-1.4 receipt"
        readme = zf.read("tax_2026/README.txt").decode("utf-8")
        assert "(1 files)" in readme and "names but that are not on disk" in readme and "111-1: /receipts/amazon" in readme
        assert "tax_2026/notes.txt" not in zf.namelist()  # no notes, no file

    def test_twin_receipt_names_are_numbered_across_years(self, client, tmp_path):
        def add(year, name):
            r = client.post("/taxes/expense", data={"year": str(year), "date": f"{year}-03-03", "description": name, "amount": "1",
                                                    "profile": "alpha", "email": "", "category": ""},
                            files={"receipt_file": (name, b"%PDF-1.4 x", "application/pdf")}, follow_redirects=False)
            assert r.status_code == 303, r.text[:200]

        add(2025, "Order.pdf")
        store = lambda: json.loads((tmp_path / "data" / "tax_inputs.json").read_text(encoding="utf-8"))  # noqa: E731
        assert store()["2025"]["expenses"][0]["receipt"]["name"] == "Order.pdf"  # alone: untouched
        add(2026, "Order.pdf")
        first, second = store()["2025"]["expenses"][0]["receipt"], store()["2026"]["expenses"][0]["receipt"]
        assert first["name"] == "Order-0001.pdf" and second["name"] == "Order-0002.pdf"
        assert first["file"].endswith("_Order-0001.pdf") and (tmp_path / "data" / first["file"]).exists()  # the file followed
        assert second["file"].endswith("_Order-0002.pdf") and (tmp_path / "data" / second["file"]).exists()
        add(2026, "Order.pdf")
        assert store()["2026"]["expenses"][1]["receipt"]["name"] == "Order-0003.pdf"
        add(2026, "Invoice.pdf")
        assert store()["2026"]["expenses"][2]["receipt"]["name"] == "Invoice.pdf"  # a different name stays bare
        from web import tax_inputs as ti
        assert ti._receipt_base("Order-0002.pdf") == ("Order", ".pdf") and ti._receipt_base("scan") == ("scan", "")

    def test_the_add_form_suggests_from_every_years_expenses(self, client, tmp_path):
        """suggestions on the expense form, drawn from ALL expenses."""
        client.post("/taxes/expense", data={"year": "2025", "date": "2025-03-03", "description": "Shipping boxes", "amount": "12",
                                            "profile": "alpha", "email": "", "receipt_url": "https://x/r.pdf", "category": "supplies"}, follow_redirects=False)
        body = client.get("/taxes", params={"year": "2026"}).text
        assert 'name="description" value="" placeholder="what it was" required data-choices="description" autocomplete="off"' in body
        assert 'data-choices="category"' in body and 'data-choices="email"' in body
        choices = json.loads(body.split('id="cell-choices">')[1].split("</script>")[0])
        assert choices["values"]["description"] == ["Shipping boxes"] and choices["values"]["category"] == ["supplies"]  # 2025's, on 2026's page
        assert "amount" not in choices["values"] and 'data-choices="amount"' not in body  # the date, amount and link are the row's own (2026-09-21)

    def test_an_expense_adds_in_place(self, client, tmp_path):
        """the expense add posts in place -- the panel, the summary and a toast; a refusal keeps what was typed."""
        response = client.post("/taxes/expense", data={"year": "2026", "date": "2026-03-03", "description": "boxes", "amount": "12",
                                                       "profile": "alpha", "email": "", "receipt_url": "https://x/r.pdf", "category": ""},
                               headers={"HX-Request": "true"})
        assert response.status_code == 200, response.text[:300]
        body = response.text
        assert body.lstrip().startswith('<details class="panel" id="s-expenses" open>') and "<html" not in body
        assert '<details class="panel" id="s-schedule-c" open hx-swap-oob="true">' in body
        assert '<div class="toast ok" role="status">Added boxes</div>' in body and "boxes" in body
        refused = client.post("/taxes/expense", data={"year": "2026", "date": "2026-03-03", "description": "tape", "amount": "lots"},
                              headers={"HX-Request": "true"})
        assert refused.status_code == 400 and "Nothing was added:" in refused.text and 'value="tape"' in refused.text
        assert 'class="expense-form" hx-post="/taxes/expense?year=2026" hx-encoding="multipart/form-data"' in refused.text

    def test_a_year_closes_unless_it_is_current_or_has_rows(self, client, tmp_path):
        page = client.get("/taxes", params={"year": "2026"}).text
        assert '<form method="post" action="/taxes/close" class="inline-form close-year"' in page
        assert 'disabled title="2026 is the current year">Close year</button>' in page
        refused = client.post("/taxes/close", data={"year": "2026"})
        assert refused.status_code == 200 and "2026 cannot be closed: 2026 is the current year." in refused.text
        import re

        listed = {int(y) for y in page.split('data-years="')[1].split('"')[0].split(",")}
        for y in listed - {2026}:  # every other listed year comes from the ledger's rows: refused too
            body = client.post("/taxes/close", data={"year": str(y)}).text
            assert f"{y} cannot be closed:" in body and "ledger row(s)" in body
        client.post("/taxes/save", data={"year": "2019", "site.0.name": "Rakuten", "site.0.amount": "5"}, follow_redirects=False)
        client.post("/taxes/expense", data={"year": "2019", "date": "2019-03-03", "description": "boxes", "amount": "12",
                                            "profile": "alpha", "email": "", "receipt_url": "https://x/r.pdf", "category": ""}, follow_redirects=False)
        page = client.get("/taxes", params={"year": "2019"}).text
        assert "2019" in page.split('data-years="')[1].split('"')[0] and 'title="delete this year' in page
        assert "disabled" not in page.split('class="inline-form close-year"')[1].split("</form>")[0]
        closed = client.post("/taxes/close", data={"year": "2019"}, follow_redirects=False)
        assert closed.status_code == 303 and "notice=Closed+2019" in closed.headers["location"]
        assert "2019" not in json.loads((tmp_path / "data" / "tax_inputs.json").read_text(encoding="utf-8"))
        assert "2019" not in client.get("/taxes").text.split('data-years="')[1].split('"')[0]
        assert client.post("/taxes/close", data={"year": "2019"}, follow_redirects=False).status_code == 303  # nothing saved: still fine

    def test_a_years_logs_stay_inside_the_year(self, client, tmp_path):
        """a past year's page offered a 2026 date; "it should not be possible to
        set dates for different years"."""
        body = client.get("/taxes", params={"year": "2025"}).text
        assert 'name="program:alpha:costco.new.date" value="2025-12-31"' in body  # today (2026) is not in 2025
        assert 'name="program:alpha:costco.new.date" value="2026-09-18"' in client.get("/taxes", params={"year": "2026"}).text
        bad = client.post("/taxes/save", data={"year": "2025", "program:alpha:costco.new.date": "2026-01-05", "program:alpha:costco.new.amount": "5"})
        assert bad.status_code == 200 and "Costco Executive Cashback — alpha: 2026-01-05 is not in 2025" in bad.text
        assert "2025" not in json.loads((tmp_path / "data" / "tax_inputs.json").read_text(encoding="utf-8")) if (tmp_path / "data" / "tax_inputs.json").exists() else True
        ok = client.post("/taxes/save", data={"year": "2025", "program:alpha:costco.new.date": "", "program:alpha:costco.new.amount": "5"}, follow_redirects=False)
        assert ok.status_code == 303
        assert json.loads((tmp_path / "data" / "tax_inputs.json").read_text(encoding="utf-8"))["2025"]["program_entries"] == {
            "program:alpha:costco": [{"date": "2025-12-31", "amount": 5.0, "note": ""}]}  # a blank date: the year's last day
        # any year opens from the box, orders or not
        assert '<form method="get" action="/taxes" class="inline-form open-year">' in body
        assert client.get("/taxes", params={"year": "2021"}).status_code == 200

    def test_a_year_with_saved_inputs_stays_on_the_list(self, client):
        """this
        year is always offered; a year with saved inputs but no rows stays too."""
        years = lambda: client.get("/taxes").text.split('data-years="')[1].split('"')[0].split(",")  # noqa: E731
        assert "2026" in years() and "2023" not in years()
        client.post("/taxes/save", data={"year": "2023", "site.0.name": "Rakuten", "site.0.amount": "5"}, follow_redirects=False)
        assert "2023" in years()
        # the year steps like the overview's month: arrows through the listed years, the right one
        # greyed on the latest listed year, "this year" back from another
        body = client.get("/taxes", params={"year": "2023"}).text
        assert '<span class="month-label year-label">2023</span>' in body and 'href="/taxes?year=2026">this year</a>' in body
        assert '<span class="button small disabled">‹</span>' in body or 'title="' in body.split('year-label">2023')[0][-200:]
        latest = client.get("/taxes", params={"year": "2026"}).text
        assert '<span class="button small disabled">›</span>' in latest and "this year</a>" not in latest
        client.post("/taxes/save", data={"year": "2027", "site.0.name": "Rakuten", "site.0.amount": "1"}, follow_redirects=False)  # a future year opened
        body = client.get("/taxes", params={"year": "2026"}).text
        assert 'href="/taxes?year=2027" title="2027">›</a>' in body  # the arrow reaches it; 2027 is the latest and greys its own
        assert '<span class="button small disabled">›</span>' in client.get("/taxes", params={"year": "2027"}).text

    def test_the_amounts_keep_a_dated_log(self, client, tmp_path):
        """program cashback and cashback sites change often -- a log of what was
        added or taken back and when, the total kept for the summary."""
        client.post("/taxes/save", data={"year": "2026", "program:alpha:costco": "30"}, follow_redirects=False)
        response = client.post("/taxes/save", data={
            "year": "2026",
            "program:alpha:costco.0.date": "2026-01-01", "program:alpha:costco.0.amount": "30", "program:alpha:costco.0.note": "",
            "program:alpha:costco.new.date": "2026-04-02", "program:alpha:costco.new.amount": "12.5", "program:alpha:costco.new.note": "Q1",
            "site.0.name": "Rakuten", "site.0.amount.new.date": "", "site.0.amount.new.amount": "$1,000", "site.0.amount.new.note": "",
            "site.1.name": "", "site.1.amount.new.amount": ""}, follow_redirects=False)
        assert response.status_code == 303
        saved = json.loads((tmp_path / "data" / "tax_inputs.json").read_text(encoding="utf-8"))["2026"]
        assert saved["programs"] == {"program:alpha:costco": 42.5} and saved["sites"] == {"Rakuten": 1000.0}
        assert saved["program_entries"] == {"program:alpha:costco": [{"date": "2026-01-01", "amount": 30.0, "note": ""},
                                                                      {"date": "2026-04-02", "amount": 12.5, "note": "Q1"}]}
        assert saved["site_entries"] == {"Rakuten": [{"date": "2026-09-18", "amount": 1000.0, "note": ""}]}  # a blank date: today
        body = client.get("/taxes", params={"year": "2026"}).text
        assert 'data-log="program:alpha:costco"' in body and "$42.50" in body and "$1,000.00" in body
        assert 'name="program:alpha:costco.0.date" value="2026-04-02"' in body  # newest first
        assert '.amount.0.amount" value="1000"' in body and '.amount.new.date" value="2026-09-18"' in body  # Rakuten's row, the empty row
        # a row ticked for removal goes; the total follows
        response = client.post("/taxes/save", data={
            "year": "2026",
            "program:alpha:costco.0.date": "2026-04-02", "program:alpha:costco.0.amount": "12.5", "program:alpha:costco.0.remove": "on",
            "program:alpha:costco.1.date": "2026-01-01", "program:alpha:costco.1.amount": "30"}, follow_redirects=False)
        assert response.status_code == 303
        saved = json.loads((tmp_path / "data" / "tax_inputs.json").read_text(encoding="utf-8"))["2026"]
        assert saved["programs"] == {"program:alpha:costco": 30.0} and saved["program_entries"] == {"program:alpha:costco": [{"date": "2026-01-01", "amount": 30.0, "note": ""}]}
        assert saved["sites"] == {} and saved["site_entries"] == {}  # not posted: dropped, as before
        # a plain amount keeps the stored log while the total is unchanged, drops it when it moves
        client.post("/taxes/save", data={"year": "2026", "program:alpha:costco": "30"}, follow_redirects=False)
        assert json.loads((tmp_path / "data" / "tax_inputs.json").read_text(encoding="utf-8"))["2026"]["program_entries"] == {
            "program:alpha:costco": [{"date": "2026-01-01", "amount": 30.0, "note": ""}]}
        client.post("/taxes/save", data={"year": "2026", "program:alpha:costco": "31"}, follow_redirects=False)
        assert json.loads((tmp_path / "data" / "tax_inputs.json").read_text(encoding="utf-8"))["2026"]["program_entries"] == {}
        bad = client.post("/taxes/save", data={"year": "2026", "program:alpha:costco.new.amount": "lots"})
        assert bad.status_code == 200 and "Costco Executive Cashback — alpha: not a number" in bad.text
        # a sign-up bonus is the same log, usually one row: the day it was earned
        response = client.post("/taxes/save", data={
            "year": "2026", "bonus:0315.new.date": "2026-03-09", "bonus:0315.new.amount": "750", "bonus:0315.new.note": "after $4k"}, follow_redirects=False)
        assert response.status_code == 303
        saved = json.loads((tmp_path / "data" / "tax_inputs.json").read_text(encoding="utf-8"))["2026"]
        assert saved["bonuses"] == {"bonus:0315": 750.0} and saved["bonus_entries"] == {"bonus:0315": [{"date": "2026-03-09", "amount": 750.0, "note": "after $4k"}]}
        body = client.get("/taxes", params={"year": "2026"}).text
        assert 'data-log="bonus:0315"' in body and "<th>Earned</th>" in body and 'name="bonus:0315.0.date" value="2026-03-09"' in body
        # other income too: each row's amount is a log dated the day it was received
        response = client.post("/taxes/save", data={
            "year": "2026", "other.0.label": "refund", "other.0.amount.new.date": "2026-05-05", "other.0.amount.new.amount": "8"}, follow_redirects=False)
        assert response.status_code == 303
        saved = json.loads((tmp_path / "data" / "tax_inputs.json").read_text(encoding="utf-8"))["2026"]
        assert saved["other"] == [{"label": "refund", "amount": 8.0, "kind": "income", "entries": [{"date": "2026-05-05", "amount": 8.0, "note": ""}]}]
        body = client.get("/taxes", params={"year": "2026"}).text
        assert 'data-log="other.0.amount"' in body and "<th>Received</th>" in body and 'name="other.0.amount.0.date" value="2026-05-05"' in body
        client.post("/taxes/save", data={"year": "2026", "other.0.label": "refund", "other.0.amount": "8"}, follow_redirects=False)  # a plain amount keeps the log
        assert json.loads((tmp_path / "data" / "tax_inputs.json").read_text(encoding="utf-8"))["2026"]["other"][0]["entries"] == [{"date": "2026-05-05", "amount": 8.0, "note": ""}]

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

    def test_an_expense_missing_its_receipt_or_payer_is_refused_with_the_draft_kept(self, client):
        response = client.post("/taxes/expense", params={"year": "2026"},
                               data={"date": "2026-03-04", "description": "boxes", "amount": "12.50",
                                     "profile": "alpha", "email": "not-an-email"})
        assert response.status_code == 200
        assert "A receipt is required" in response.text and "Email is not an address" in response.text
        assert 'value="boxes"' in response.text  # the draft survives
        assert '<details class="add-row" id="expense-form" open>' in response.text  # open again, with the draft
        # who paid: the profile OR the email, one is enough
        neither = client.post("/taxes/expense", params={"year": "2026"},
                              data={"date": "2026-03-04", "description": "boxes", "amount": "1", "receipt_url": "https://x/r"})
        assert "Who paid is required: the profile, or the email of the account" in neither.text
        email_only = client.post("/taxes/expense", params={"year": "2026"}, follow_redirects=False,
                                 data={"date": "2026-03-04", "description": "tape", "amount": "1", "email": "a@b.co",
                                       "receipt_url": "https://x/r"})
        assert email_only.status_code == 303
        assert ">Profile<" in client.get("/taxes", params={"year": "2026"}).text  # its own column, editable in place

    def test_update_expense_keeps_the_receipt_unless_replaced(self, tmp_path):
        """The grid's cell writes and the receipt upload share update_expense: fields re-validated
        as on add, the receipt kept unless a new file or a different link replaces it, a replaced
        upload deleted. (The form-based edit mode went on 2026-09-19: the grid edits in place.)"""
        from web.tax_inputs import YearInputs, add_expense, update_expense

        inputs = YearInputs()
        entry = add_expense(inputs, {"date": "2026-03-04", "description": "boxes", "amount": "12.50",
                                     "profile": "alpha", "receipt_url": "https://x/receipt"},
                            year=2026, data_dir=tmp_path)
        same = update_expense(inputs, entry["id"], {"date": "2026-03-05", "description": "bigger boxes", "amount": "13",
                                                     "profile": "alpha", "receipt_url": "https://x/receipt"},
                              year=2026, data_dir=tmp_path)
        assert same["description"] == "bigger boxes" and same["amount"] == 13.0 and same["receipt"] == {"url": "https://x/receipt"}
        with pytest.raises(ValueError, match="Description is required"):
            update_expense(inputs, entry["id"], {"date": "2026-03-05", "description": "", "amount": "13", "profile": "alpha"},
                           year=2026, data_dir=tmp_path)
        one = update_expense(inputs, entry["id"], {"date": "2026-03-05", "description": "bigger boxes", "amount": "13", "profile": "alpha"},
                             year=2026, data_dir=tmp_path, receipt_file=("one.pdf", b"%PDF-1"))
        assert one["receipt"]["name"] == "one.pdf" and "url" not in one["receipt"]
        update_expense(inputs, entry["id"], {"date": "2026-03-05", "description": "bigger boxes", "amount": "13", "profile": "alpha"},
                       year=2026, data_dir=tmp_path, receipt_file=("two.pdf", b"%PDF-2"))
        assert sorted(p.name for p in (tmp_path / "expenses" / "2026").glob("*")) == [f"{entry['id']}_two.pdf"]
        with pytest.raises(KeyError):
            update_expense(inputs, "nope", {"description": "x"}, year=2026, data_dir=tmp_path)

    def test_the_expenses_table_edits_like_the_orders_grid(self, client, tmp_path):
        """the Orders grid's cells, row numbers, one-cell writes, a bulk
        delete."""
        import re

        for when, what in (("2026-03-04", "boxes"), ("2026-03-05", "tape")):
            client.post("/taxes/expense", params={"year": "2026"}, follow_redirects=False,
                        data={"date": when, "description": what, "amount": "12.50", "profile": "alpha",
                              "category": "supplies", "receipt_url": "https://x/r"})
        store = tmp_path / "data" / "tax_inputs.json"
        ids = [e["id"] for e in json.loads(store.read_text(encoding="utf-8"))["2026"]["expenses"]]
        body = client.get("/taxes", params={"year": "2026"}).text
        assert 'class="grid compact expenses sheetlike stacked" data-cell-url="/taxes/expense/cell?year=2026"' in body
        assert 'class="num actions"' not in body and "del-expense-" not in body  # no per-row buttons
        assert "Edit Expense" not in body and 'action="/taxes/expense?year=2026"' in body  # the form only adds
        assert '<details class="add-row" id="expense-form" >' in body  # folded above the list, closed by default
        assert body.index('<details class="add-row"') < body.index('class="grid compact expenses sheetlike stacked"')
        assert body.count('name="sel"') == 2 and 'id="sel-all"' in body and 'id="delete-selected"' in body
        assert 'action="/taxes/expenses/delete?year=2026" data-confirm=' in body and 'data-confirm-many="Delete the {n} selected expenses?' in body
        assert f'data-field="amount" data-entry-id="{ids[0]}" data-raw="12.50"' in body
        assert re.search(r'data-field="date"[^>]*data-kind="date"', body) and re.search(r'data-field="category"[^>]*data-kind="choice"', body)
        assert '"category": ["supplies"]' in body and '"profile": ["alpha"]' in body  # the columns' previous answers
        assert 'data-tip-from="expense-hints"' in body and '<td colspan="2">Rows</td>' in body and "keep my edits" not in body
        # the header names sort, as on the Orders table: ascending, descending,
        # clear; by default newest date first with no arrow
        # the arrow at the header's right is the sort link, the name selects
        assert ('<span class="name">Amount</span><a class="sort" href="/taxes?year=2026&esort=amount&edir=asc#s-expenses" '
                'title="sort ascending">⇅</a>') in body
        grid_at = body.index('class="grid compact expenses sheetlike stacked"')
        default_rows = body[grid_at:body.index("</tbody>", grid_at)]
        assert default_rows.index(ids[1]) < default_rows.index(ids[0]) and "▼" not in body[grid_at:body.index("</thead>", grid_at)]
        by_amount = client.post("/taxes/expense/cell", params={"year": "2026"},
                                data={"entry_id": ids[1], "field": "amount", "value": "99", "expected": "12.50"})
        assert "data-error" not in by_amount.text
        sorted_page = client.get("/taxes", params={"year": "2026", "esort": "amount", "edir": "desc"}).text
        grid_at = sorted_page.index('class="grid compact expenses sheetlike stacked"')
        rows_part = sorted_page[grid_at:sorted_page.index("</tbody>", grid_at)]
        assert rows_part.index(ids[1]) < rows_part.index(ids[0]) and "▼</a>" in sorted_page
        assert 'href="/taxes?year=2026#s-expenses" title="clear the sort">▼</a>' in sorted_page
        asc_page = client.get("/taxes", params={"year": "2026", "esort": "amount", "edir": "asc"}).text
        assert 'href="/taxes?year=2026&esort=amount&edir=desc#s-expenses" title="sort descending">▲</a>' in asc_page
        client.post("/taxes/expense/cell", params={"year": "2026"},
                    data={"entry_id": ids[1], "field": "amount", "value": "12.50", "expected": "99.00"})
        # one cell: the td comes back re-rendered (or with the error in data-error)
        td = client.post("/taxes/expense/cell", params={"year": "2026"},
                         data={"entry_id": ids[0], "field": "amount", "value": "20", "expected": "12.50"})
        assert td.status_code == 200 and td.text.lstrip().startswith("<td") and 'data-raw="20.00"' in td.text
        assert "data-error" not in td.text and "$20.00" in td.text
        assert json.loads(store.read_text(encoding="utf-8"))["2026"]["expenses"][0]["amount"] == 20.0
        bad = client.post("/taxes/expense/cell", params={"year": "2026"},
                          data={"entry_id": ids[0], "field": "date", "value": "2025-01-01", "expected": "2026-03-04"})
        assert 'data-error="Date must fall in 2026"' in bad.text and 'data-raw="2026-03-04"' in bad.text  # unchanged
        stale = client.post("/taxes/expense/cell", params={"year": "2026"},
                            data={"entry_id": ids[0], "field": "amount", "value": "1", "expected": "12.50"})
        assert "changed meanwhile" in stale.text and 'data-raw="20.00"' in stale.text
        gone = client.post("/taxes/expense/cell", params={"year": "2026"}, data={"entry_id": "nope", "field": "amount", "value": "1"})
        assert 'data-error="no such expense' in gone.text
        link = client.post("/taxes/expense/cell", params={"year": "2026"},
                           data={"entry_id": ids[1], "field": "receipt_url", "value": "https://x/new", "expected": "https://x/r"})
        assert 'href="https://x/new"' in link.text
        blank = client.post("/taxes/expense/cell", params={"year": "2026"},
                            data={"entry_id": ids[1], "field": "receipt_url", "value": "", "expected": "https://x/new"})
        assert 'href="https://x/new"' in blank.text and "data-error" not in blank.text  # a receipt stays: it is required
        # the receipt cell's upload button, as on the Orders table: the file replaces the receipt
        assert f'class="cell-upload" data-upload-url="/taxes/expense/{ids[1]}/receipt?year=2026"' in body
        up = client.post(f"/taxes/expense/{ids[1]}/receipt", params={"year": "2026"}, follow_redirects=False,
                         files={"receipt_file": ("scan.pdf", b"%PDF-9", "application/pdf")})
        assert up.status_code == 303 and "Receipt+uploaded" in up.headers["location"] or "Receipt%20uploaded" in up.headers["location"]
        entry = json.loads(store.read_text(encoding="utf-8"))["2026"]["expenses"]
        entry = next(e for e in entry if e["id"] == ids[1])
        assert entry["receipt"]["name"] == "scan.pdf" and "url" not in entry["receipt"]
        assert client.post("/taxes/expense/nope/receipt", params={"year": "2026"},
                           files={"receipt_file": ("s.pdf", b"x", "application/pdf")}).status_code == 404
        none = client.post(f"/taxes/expense/{ids[1]}/receipt", params={"year": "2026"}, follow_redirects=False)
        assert none.status_code == 303 and "error=" in none.headers["location"]
        # the selected rows go together, one confirmation
        deleted = client.post("/taxes/expenses/delete", params={"year": "2026"}, data={"sel": ids}, follow_redirects=False)
        assert deleted.status_code == 303 and "Deleted" in deleted.headers["location"]
        assert json.loads(store.read_text(encoding="utf-8"))["2026"]["expenses"] == []

    def test_it_refuses_the_wrong_methods(self, client):
        assert client.put("/taxes").status_code == 405


class TestVirtualCardOnTheSettingsPage:
    def test_the_flag_round_trips_through_the_entry_card(self, config_file):
        from web import settings_form

        config_file(cards=[{"last4": "0315", "name": "USB Prime Business", "cashback_rate": 0.05},
                           {"last4": "1111", "name": "The real card", "cashback_rate": 0.05}])
        # a virtual card names the card it belongs to
        settings_form.apply_entry("cards", 0, {"last4": "0315", "name": "USB Prime Business",
                                               "cashback_rate": "5%", "virtual": "on", "virtual_of": "1111"})
        assert settings_form.display_entries("cards")[0]["virtual"] is True
        assert settings_form.display_entries("cards")[0]["virtual_of"] == "1111"
        settings_form.apply_entry("cards", 0, {"last4": "0315", "name": "USB Prime Business",
                                               "cashback_rate": "5%"})
        assert settings_form.display_entries("cards")[0]["virtual"] is False
        from config.loader import load_config

        assert "virtual" not in load_config()["cards"][0]
