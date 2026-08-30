"""scripts/import_history.py -- the pure parts, and a dry run on a synthetic export (--no-sheet)."""

import csv

import pytest

from scripts import import_history as ih


class TestHeaderMapping:
    def test_aliases_and_display_names_map(self):
        m, unmapped = ih.map_headers(["Order Date", "Item", "Qty", "Order Number", "Card Used", "Cashback",
                                      "Total Profit", "SUB", "Insurance"], {})
        assert m["Item"] == "item_name" and m["Qty"] == "quantity" and m["Order Number"] == "order_id"
        assert m["Card Used"] == "card_name" and m["Cashback"] == "cashback_rate"
        assert m["Total Profit"] == "source_profit"
        assert unmapped == ["SUB"]

    def test_an_override_wins_and_a_bad_target_is_refused(self):
        m, _ = ih.map_headers(["Net"], {"Net": "source_profit"})
        assert m["Net"] == "source_profit"
        with pytest.raises(SystemExit):
            ih.map_headers(["X"], {"X": "Nonsense Column"})


class TestDates:
    def test_the_order_is_detected_from_a_day_above_twelve(self):
        assert ih.detect_date_order(["3/11/2026", "2/21/2026"]) == "mdy"
        assert ih.detect_date_order(["21/2/2026"]) == "dmy"
        assert ih.detect_date_order(["3/11/2026", "4/5/2026"]) is None   # never disambiguated

    def test_mixed_orders_are_refused(self):
        with pytest.raises(SystemExit):
            ih.detect_date_order(["21/2/2026", "2/21/2026"])

    def test_to_iso(self):
        assert ih.to_iso("3/11/2026", "mdy") == "2026-03-11"
        assert ih.to_iso("3/11/2026", "dmy") == "2026-11-03"
        assert ih.to_iso("2026-08-30", "mdy") == "2026-08-30"
        assert ih.to_iso("Please fill", "mdy") == ""


class TestCellParsing:
    def test_placeholders_and_money(self):
        assert ih.clean("Please fill") == "" and ih.clean("#VALUE!") == ""
        assert ih.money("$1,314.98") == 1314.98 and ih.money("-$3.86") == -3.86 and ih.money("12.5%") == 0.125
        assert ih.money("Please fill") is None

    def test_card_split(self):
        assert ih.split_card("Triple Cash 4351") == ("Triple Cash", "4351")
        assert ih.split_card("Prime Visa") == ("Prime Visa", "")
        assert ih.split_card("Venmo Credit + Exec") == ("Venmo Credit + Exec", "")

    def test_tracking_cells_split_on_newlines(self):
        assert ih.tracking_numbers("TBA1\nTBA2\nTBA3") == ["TBA1", "TBA2", "TBA3"]
        assert ih.tracking_numbers("Please fill") == []

    def test_status_words(self):
        assert ih.normalise_status("Paid") == "paid" and ih.normalise_status("Return") == "return"
        with pytest.raises(ValueError):
            ih.normalise_status("Lost")


HEADER = ["Order Date", "Status", "Item", "Quantity", "Retailer", "Order Number", "Tracking Number",
          "Delivery Date", "Total Cost", "Card Used", "Cashback", "SUB", "SUB Rate", "Buying Group",
          "Insurance", "Payout Date", "Payout Amount", "Total Profit"]

GOOD = ["2/21/2026", "Paid", "ASUS Vivobook", "1", "Costco", "1399000009", "1Z1", "2/25/2026", "$714.98",
        "Triple Cash 4351", "1%", "TRUE", "12.5%", "BFMR", "-$3.86", "2/26/2026", "$715.00", "$92.68"]


def _write(tmp_path, rows):
    p = tmp_path / "old.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(rows)
    return p


def _rows(tmp_path, rows, **kw):
    headers, raw = ih.read_rows(_write(tmp_path, rows))
    mapping, _ = ih.map_headers(headers, {})
    return ih.normalise(raw, mapping, date_order="mdy", rate_adds=kw.get("rate_adds", [("SUB Rate", "SUB")]),
                        profile="p", allow_open=kw.get("allow_open", False))


class TestNormaliseAndReconcile:
    def test_a_real_row_reconciles_to_the_cent_with_the_sub_rate_folded_in(self, tmp_path):
        rows, refusals, counts = _rows(tmp_path, [GOOD])
        assert refusals == [] and len(rows) == 1
        r = rows[0]
        assert r["cashback_rate"] == pytest.approx(0.135) and r["insurance"] == 3.86 and r["card_last4"] == "4351"
        assert ih.reconcile(rows) == []

    def test_a_wrong_rate_mapping_fails_the_reconciliation(self, tmp_path):
        rows, _, _ = _rows(tmp_path, [GOOD], rate_adds=[])   # SUB rate not folded in
        bad = ih.reconcile(rows)
        assert len(bad) == 1 and "recomputed" in bad[0]

    def test_open_rows_are_refused_unless_allowed(self, tmp_path):
        open_row = list(GOOD)
        open_row[1] = "Ordered"
        rows, refusals, counts = _rows(tmp_path, [open_row])
        assert rows == [] and counts["open rows refused"] == 1
        rows, _, _ = _rows(tmp_path, [open_row], allow_open=True)
        assert len(rows) == 1

    def test_no_order_number_is_synthesised_and_warned(self, tmp_path):
        bonus = ["3/19/2026", "Paid", "Referral Bonus", "1", "", "", "", "3/19/2026", "$0.00", "", "", "FALSE", "0%",
                 "BFMR", "$0.00", "3/19/2026", "$250.00", "$250.00"]
        rows, _, counts = _rows(tmp_path, [bonus, bonus])
        assert [r["order_id"] for r in rows] == ["BFMR-IMPORT-2026-03-19", "BFMR-IMPORT-2026-03-19-2"]
        assert rows[0]["retailer"] == "BFMR" and any("no tracking number" in w for w in rows[0].warnings)
        assert ih.reconcile(rows) == []

    def test_a_multi_tracking_cell_becomes_one_row_per_box_with_money_prorated(self, tmp_path):
        multi = list(GOOD)
        multi[3] = "3"
        multi[6] = "TBA1\nTBA2\nTBA3"
        multi[8] = "$897.00"
        multi[14] = "-$6.60"
        multi[16] = "$897.00"
        multi[17] = "$0"
        rows, _, _ = _rows(tmp_path, [multi])
        items = ih.explode(rows)
        assert [(i.shipment, i.tracking_number, i.quantity) for i in items] == [("1", "TBA1", 1), ("2", "TBA2", 1), ("3", "TBA3", 1)]
        assert sum(i.payout_amount for i in items) == pytest.approx(897.0)
        assert sum(i.insurance for i in items) == pytest.approx(6.6)

    def test_shipments_number_by_tracking_within_an_order(self, tmp_path):
        a = list(GOOD)
        b = list(GOOD)
        b[2] = "Other item"
        b[6] = "1Z2"
        c = list(GOOD)
        c[2] = "Third"
        items = ih.explode(_rows(tmp_path, [a, b, c])[0])
        assert [i.shipment for i in items] == ["1", "2", "1"]


class TestPreview:
    def test_update_append_existing_and_collision_are_told_apart(self, tmp_path):
        rows, _, _ = _rows(tmp_path, [GOOD])
        items = ih.explode(rows)
        it = items[0]
        grid = [["Order ID", "Order Date", "Item Name", "Shipment", "Tracking Number"],
                [it.order_id, it.order_date, it.item_name, it.shipment, it.tracking_number]]
        assert len(ih.preview_against_sheet(items, grid)["update"]) == 1
        grid[1][2] = "Renamed by the scraper"
        assert len(ih.preview_against_sheet(items, grid)["existing_order"]) == 1
        grid[1][0] = "OTHER"
        assert len(ih.preview_against_sheet(items, grid)["tracking_collision"]) == 1
        assert len(ih.preview_against_sheet(items, [grid[0]])["append"]) == 1


def test_a_dry_run_end_to_end_writes_the_normalised_csv_and_nothing_else(tmp_path, capsys):
    placeholder = list(GOOD)
    placeholder[5] = "113-0000000-0000000"
    placeholder[8] = "Please fill"          # a real order with no cost: skipped so a scrape can fill it
    placeholder[17] = "#VALUE!"
    src = _write(tmp_path, [GOOD, placeholder])
    out = tmp_path / "import.csv"
    code = ih.main([str(src), "--rate-add", "SUB Rate:SUB", "--no-sheet", "--out", str(out)])
    text = capsys.readouterr().out
    assert code == 0 and "0 disagree" in text and "DRY RUN" in text and out.exists()
    assert "1 rows without a cost skipped" in text
    with out.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["order_id"] == "1399000009" and rows[0]["cashback_rate"] == "0.135" and rows[0]["insurance"] == "3.86"


def test_a_disagreeing_profit_column_refuses_the_import(tmp_path, capsys):
    src = _write(tmp_path, [GOOD])
    code = ih.main([str(src), "--no-sheet", "--out", str(tmp_path / "x.csv")])   # SUB rate NOT folded in
    assert code == 1 and "REFUSED" in capsys.readouterr().err
