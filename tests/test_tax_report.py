"""scripts/tax_report.py -- the cash-basis year split, against the audit test fake."""

from scripts import tax_report
from scripts.tax_report import _year_of, build_report, render_text
from sheets.ledger_sync import _cogs_formula, _profit_formula
from tests.test_audit_sheet import Cell, build, row_cells


def _row(n, *, order_date, payout_date="", payout="", status="paid", cogs=798.0, insurance="",
         retailer="Best Buy", group="BFMR", order_id=None):
    return row_cells(n, **{
        "Order ID": Cell(order_id or f"O{n}"), "Order Date": Cell(order_date), "Status": Cell(status),
        "Retailer": Cell(retailer), "Buying Group": Cell(group),
        "Payout Date": Cell(payout_date), "Payout Amount": Cell(payout, fmt="currency"),
        "COGS": Cell(cogs, fmt="currency", formula=_cogs_formula(n)),
        "Insurance": Cell(insurance, fmt="currency"),
        "Total Profit": Cell("", formula=_profit_formula(n)),
    })


class TestYearOf:
    def test_iso_text_and_serials_and_blanks(self):
        assert _year_of("2026-08-12") == 2026
        assert _year_of("2026-08-12T06:00:00Z") == 2026
        assert _year_of(46246) == 2026          # a Sheets serial for 2026-08-12
        assert _year_of("") is None and _year_of(None) is None and _year_of("soon") is None


class TestTheTwoDates:
    """Receipts follow Payout Date; COGS and insurance follow Order Date."""

    def test_a_december_order_paid_in_january_lands_in_two_years(self):
        sheet = build(_row(2, order_date="2025-12-20", payout_date="2026-01-10", payout=900.0, insurance=7.4))
        r25 = build_report(sheet, 2025)
        r26 = build_report(sheet, 2026)
        assert r25["totals"]["cogs"] == 798.0 and r25["totals"]["insurance"] == 7.4 and r25["totals"]["payouts"] == 0
        assert r26["totals"]["payouts"] == 900.0 and r26["totals"]["cogs"] == 0
        assert r25["straddling"]["ordered_this_year_paid_in_a_later_year"] == {"rows": 1, "payouts": 900.0}
        assert r26["straddling"]["paid_this_year_for_orders_from_other_years"] == {"rows": 1, "payouts": 900.0}

    def test_an_unpaid_order_is_a_cost_now_and_flagged(self):
        r = build_report(build(_row(2, order_date="2026-03-01", status="delivered")), 2026)
        assert r["totals"]["cogs"] == 798.0 and r["totals"]["payouts"] == 0
        assert r["straddling"]["ordered_this_year_not_yet_paid"] == {"rows": 1, "cogs": 798.0}

    def test_a_cancelled_order_carries_no_cost(self):
        r = build_report(build(_row(2, order_date="2026-03-01", status="cancelled", cogs="")), 2026)
        assert r["totals"]["rows"] == 0 and r["totals"]["cogs"] == 0

    def test_net_is_payouts_minus_cogs_minus_insurance_and_cashback_is_shown(self):
        sheet = build(_row(2, order_date="2026-03-01", payout_date="2026-04-01", payout=900.0,
                           insurance=7.4, cogs=766.08))   # (798 + 0) * (1 - 0.04)
        t = build_report(sheet, 2026)["totals"]
        assert t["net"] == round(900.0 - 766.08 - 7.4, 2)
        assert t["gross_cost"] == 798.0 and t["cashback"] == round(798.0 - 766.08, 2)

    def test_breakdowns_group_by_retailer_and_buying_group(self):
        sheet = build(
            _row(2, order_date="2026-01-01", retailer="Amazon", group="MOD"),
            _row(3, order_date="2026-01-02", retailer="Best Buy", group="BFMR"),
            _row(4, order_date="2026-01-03", retailer="Best Buy", group="BFMR", order_id="O3"),
        )
        r = build_report(sheet, 2026)
        assert r["by_retailer"]["Amazon"]["orders"] == 1
        assert r["by_retailer"]["Best Buy"]["orders"] == 1 and r["by_retailer"]["Best Buy"]["rows"] == 2
        assert set(r["by_buying_group"]) == {"MOD", "BFMR"}

    def test_a_missing_cogs_cell_is_recomputed_from_its_parts(self):
        sheet = build(row_cells(2, **{"Order Date": Cell("2026-05-05"), "COGS": Cell(""),
                                     "Total Cost": Cell(100.0), "Shipping": Cell(10.0),
                                     "Cashback Rate": Cell(0.1, fmt="percent")}))
        assert build_report(sheet, 2026)["totals"]["cogs"] == 99.0


class TestRendering:
    def test_text_names_the_basis_and_the_numbers(self):
        sheet = build(_row(2, order_date="2026-03-01", payout_date="2026-04-01", payout=900.0, insurance=7.4))
        text = render_text(build_report(sheet, 2026))
        assert "Tax report -- 2026 (cash basis)" in text
        assert "900.00" in text and "(798.00)" in text and "(7.40)" in text
        assert "By retailer" in text and "Best Buy" in text
        assert "Orders dated 2026 (1 row(s))" in text

    def test_no_rows_flag_omits_the_listing(self):
        sheet = build(_row(2, order_date="2026-03-01"))
        assert "Orders dated" not in render_text(build_report(sheet, 2026), list_rows=False)

    def test_main_reads_a_snapshot_offline(self, tmp_path, capsys, monkeypatch):
        import json
        from tests.test_audit_sheet import grids_for
        snap = tmp_path / "s.json"
        snap.write_text(json.dumps(grids_for(_row(2, order_date="2026-03-01")).to_snapshot(), default=str))
        monkeypatch.setattr(tax_report, "open_worksheet_readonly", lambda: (_ for _ in ()).throw(AssertionError("must not open the live sheet")))
        tax_report.main(["2026", "--from-snapshot", str(snap), "--json"])
        out = json.loads(capsys.readouterr().out)
        assert out["year"] == 2026 and out["totals"]["cogs"] == 798.0

    def test_main_asks_for_the_year_when_omitted(self, tmp_path, capsys, monkeypatch):
        import json
        from tests.test_audit_sheet import grids_for
        snap = tmp_path / "s.json"
        snap.write_text(json.dumps(grids_for(_row(2, order_date="2026-03-01")).to_snapshot(), default=str))
        answers = iter(["nope", "2026"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
        tax_report.main(["--from-snapshot", str(snap), "--no-rows"])
        assert "Tax report -- 2026" in capsys.readouterr().out
