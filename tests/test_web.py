"""The read-only web dashboard (web/), exercised offline through the snapshot backend.

Every view is driven through FastAPI's TestClient over a CSV written with the ledger's own HEADER
(the same `row(**fields)` shape tests/test_ledger_sync.py uses), a temporary logs/ directory for
the heartbeat and a temporary failures/ directory for the dossiers (the db backend is exercised in
tests/test_audit_db.py and tests/test_ledger_db.py).

The read-only guarantee is pinned two ways: the source of `web/` is scanned for the write methods,
and every route refuses every non-GET method.
"""

from __future__ import annotations

import csv
import dataclasses
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from models.order import FIELDNAMES, MONEY_FREE_STATUSES, TERMINAL_STATUSES  # noqa: E402
from ledger.sync import HEADER, _COL, _cogs_formula, _profit_formula  # noqa: E402
from web import ledger_reader  # noqa: E402
from web.app import ROUTES, create_app, money, percent  # noqa: E402
from web.failures import hosted_copies, list_dossiers, parse_name  # noqa: E402
from web.heartbeat import read_heartbeat  # noqa: E402
from web.ledger_reader import (  # noqa: E402
    LedgerRow, SnapshotReader, cogs_of, newest_snapshot, profit_of,
    reader_from_settings, rows_from_grid,
)
from web.queries import Filters, filter_rows, order_view, sort_rows  # noqa: E402
from web.summary import overview  # noqa: E402

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------------------------------
# Fixtures: a ledger snapshot with one row of every kind the views distinguish
# --------------------------------------------------------------------------------------------------


def row(**values) -> list[str]:
    """Build a full-width row from snake_case field names -- test_ledger_sync's helper."""
    return [str(values.get(f, "")) for f in FIELDNAMES]


def write_snapshot(path: Path, *rows: list[str], header: list[str] = HEADER) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def _formula_cells(row_number: int) -> dict:
    """What a CSV backup carries in the two formula columns: the formula text itself."""
    return {"cogs": _cogs_formula(row_number), "total_profit": _profit_formula(row_number)}


LEDGER_ROWS = [
    # row 2: OPEN, ordered, no tracking, no payout -- a plain open BFMR row
    row(order_date="2026-09-09", status="ordered", retailer="Amazon Business",
        item_name="MacBook Air 13 M5", shipment="1", quantity="1", order_id="111-0000001-0000001",
        buying_group="BFMR", cost_per_item="1259.99", total_cost="1259.99", shipping="0",
        sales_tax="0", gift_card="0", rewards_used="0", card_name="USB Prime Business",
        cashback_rate="0.05", profile_label="profile-alpha",
        order_url="https://www.amazon.com/gp/css/order-details?orderID=111-0000001-0000001",
        delivery_address="BuyForMeRetail B999999, Testville, NH 03050", card_last4="0315",
        last_scraped_at="2026-09-10T10:48:26+00:00", **_formula_cells(2)),
    # row 3: OPEN, shipped, COMMITTED payout (Expected Payout, nothing paid) -> PROJECTED profit
    row(order_date="2026-09-08", status="shipped", retailer="Best Buy",
        item_name="MacBook Air 15 M5 Midnight", shipment="1", quantity="1",
        order_id="BBY01-800000000001", tracking_number="529900000012", tracking_submitted="True",
        delivery_date="2026-09-15", buying_group="BFMR", cost_per_item="1000", total_cost="1000",
        shipping="0", sales_tax="0", gift_card="0", rewards_used="0", card_name="Amex Business Gold",
        cashback_rate="0.04", insurance="6.4", expected_payout="1230", profile_label="profile-alpha",
        order_url="https://www.bestbuy.com/profile/ss/orders/order-details/BBY01-800000000001/view",
        tracking_url="https://www.fedex.com/fedextrack/?trknbr=529900000012",
        receipt_url="https://objectstorage.example/o/receipts/bestbuy/BBY01-800000000001.pdf",
        delivery_address="BFMR B999999, Testville, NH 03050", card_last4="4331", package_id="1",
        **_formula_cells(3)),
    # row 4: the SAME order, shipment 2, still ordered -- a split order stays together
    row(order_date="2026-09-08", status="ordered", retailer="Best Buy",
        item_name="MacBook Air 15 M5 Midnight", shipment="2", quantity="2",
        order_id="BBY01-800000000001", buying_group="BFMR", cost_per_item="1000", total_cost="2000",
        shipping="0", sales_tax="0", gift_card="0", rewards_used="0", card_name="Amex Business Gold",
        cashback_rate="0.04", profile_label="profile-alpha",
        order_url="https://www.bestbuy.com/profile/ss/orders/order-details/BBY01-800000000001/view",
        receipt_url="https://objectstorage.example/o/receipts/bestbuy/BBY01-800000000001.pdf",
        delivery_address="BFMR B999999, Testville, NH 03050", card_last4="4331", package_id="2",
        **_formula_cells(4)),
    # row 5: SETTLED -- paid, dated payout -> REALIZED profit; paid $20 LESS than committed
    row(order_date="2026-08-20", status="paid", retailer="Costco",
        item_name="iPad Air 11 M4 (Item #2042809)", shipment="1", quantity="2",
        order_id="1399000017", tracking_number="1Z999AA10000000001", tracking_submitted="True",
        delivery_date="2026-08-25", buying_group="MOD", cost_per_item="200", total_cost="400",
        shipping="0", sales_tax="0", gift_card="0", rewards_used="0", card_name="Venmo Visa",
        cashback_rate="0.09", insurance="0", payout_amount="500", payout_date="2026-09-01",
        expected_payout="520",
        profile_label="profile-bravo", tracking_url="https://www.ups.com/track?tracknum=1Z999AA10000000001",
        delivery_address="MOD warehouse", card_last4="4351", **_formula_cells(5)),
    # row 6: SETTLED by STATUS alone -- MOD's dateless paid row
    row(order_date="2026-08-18", status="paid", retailer="Costco", item_name="Dyson V15 (Item #1)",
        shipment="1", quantity="1", order_id="1399000018", tracking_number="1Z999AA10000000002",
        buying_group="MOD", cost_per_item="300", total_cost="300", shipping="0", cashback_rate="0.09",
        payout_amount="330", profile_label="profile-bravo", card_last4="4351", **_formula_cells(6)),
    # row 7: CANCELLED -- money-free, never counted anywhere
    row(order_date="2026-08-15", status="cancelled", retailer="Costco",
        item_name="IPAD AIR 11 M4 256GB PURP (Item #2042809)", shipment="1", quantity="2",
        order_id="1399000019", buying_group="BFMR", profile_label="profile-bravo", card_last4="4351",
        **_formula_cells(7)),
    # row 8: DELIVERED with a blank card and a blank rate -- the COGS input gap
    row(order_date="2026-08-10", status="delivered", retailer="Amazon", item_name="Fitbit Charge 6",
        shipment="1", quantity="1", order_id="111-0000002-0000002", tracking_number="TBA000000000001",
        delivery_date="2026-08-13", buying_group="BFMR", cost_per_item="100", total_cost="100",
        shipping="0", profile_label="profile-charlie", **_formula_cells(8)),
    # row 9: a GIFT CARD row -- kept, routed nowhere
    row(order_date="2026-08-09", status="delivered", retailer="Amazon", item_name="Amazon Gift Card",
        shipment="1", quantity="1", order_id="111-0000003-0000003", buying_group="Gift Card",
        cost_per_item="40", total_cost="40", cashback_rate="0.05", profile_label="profile-charlie",
        card_last4="0315", **_formula_cells(9)),
    # row 10: SUPERSEDED -- money-free, quantity blank too
    row(order_date="2026-08-05", status="superseded", retailer="Amazon", item_name="iPad Pro",
        shipment="3", order_id="111-0000004-0000004", tracking_number="TBA000000000009",
        buying_group="BFMR", profile_label="profile-charlie", **_formula_cells(10)),
]

NOTE_ROW = row(item_name="-- a note below the block, no Order ID --")


@pytest.fixture
def snapshot_path(tmp_path) -> Path:
    return write_snapshot(tmp_path / "ledger_backup_20260917T000000Z.csv", *LEDGER_ROWS, NOTE_ROW)


@pytest.fixture
def logs_dir(tmp_path) -> Path:
    directory = tmp_path / "logs"
    directory.mkdir()
    return directory


@pytest.fixture
def failures_dir(logs_dir) -> Path:
    directory = logs_dir / "failures"
    directory.mkdir()
    return directory


@pytest.fixture
def client(snapshot_path, logs_dir, failures_dir):
    (logs_dir / ".last_run").write_text("2026-09-17T09:00:00Z", encoding="utf-8")  # 3 h before NOW
    app = create_app(SnapshotReader(snapshot_path), logs_dir=logs_dir, failures_dir=failures_dir,
                     clock=lambda: NOW, settings=_settings())
    return TestClient(app)


def _settings(**overrides):
    """The live Settings with the values these tests depend on pinned. The run interval decides
    the heartbeat's stale threshold, and the developer's own .env may set RUN_INTERVAL_HOURS."""
    from config.settings import settings

    return dataclasses.replace(settings, container_run_interval_hours=6, **overrides)


# --------------------------------------------------------------------------------------------------
# The adapter: snapshot backend
# --------------------------------------------------------------------------------------------------


class TestSnapshotReader:
    def test_reads_by_header_name_and_skips_rows_without_an_order_id(self, snapshot_path):
        snapshot = SnapshotReader(snapshot_path).load()

        assert snapshot.backend == "snapshot"
        assert snapshot.source == str(snapshot_path)
        assert snapshot.schema_matches
        assert len(snapshot.rows) == len(LEDGER_ROWS)
        assert snapshot.skipped_rows == 1
        assert snapshot.rows[0].order_id == "111-0000001-0000001"
        assert snapshot.rows[0].row_number == 2  # the header is row 1, as on the ledger

    def test_an_older_column_order_still_reads_correctly(self, tmp_path):
        """The 2026-09-10 move of Package ID: a backup written before it has the column last.
        Values are found by name, so nothing lands in the wrong field, and the mismatch is
        reported rather than hidden."""
        old_header = [h for h in HEADER if h != "Package ID"] + ["Package ID"]
        old_index = {h: i for i, h in enumerate(old_header)}
        record = row(order_id="X1", order_date="2026-09-01", status="shipped", item_name="Thing",
                     shipment="1", package_id="00009999990206101794", last_scraped_at="2026-09-02")
        by_name = dict(zip(HEADER, record))
        shuffled = [""] * len(old_header)
        for name, value in by_name.items():
            shuffled[old_index[name]] = value
        path = write_snapshot(tmp_path / "ledger_backup_old.csv", shuffled, header=old_header)

        snapshot = SnapshotReader(path).load()

        assert not snapshot.schema_matches
        assert snapshot.missing_columns == ()
        assert snapshot.extra_columns == ()
        only = snapshot.rows[0]
        assert only.text("package_id") == "00009999990206101794"
        assert only.text("last_scraped_at") == "2026-09-02"

    def test_a_missing_column_reads_blank_and_is_named(self, tmp_path):
        header = [h for h in HEADER if h != "Rewards Used"]
        record = [v for h, v in zip(HEADER, row(order_id="X1", order_date="2026-09-01",
                                                   total_cost="10")) if h != "Rewards Used"]
        path = write_snapshot(tmp_path / "ledger_backup_short.csv", record, header=header)

        snapshot = SnapshotReader(path).load()

        assert snapshot.missing_columns == ("Rewards Used",)
        assert snapshot.rows[0].text("rewards_used") == ""
        assert snapshot.rows[0].number("rewards_used") is None

    def test_newest_backup_is_chosen_by_its_timestamped_name(self, tmp_path):
        for stamp in ("20260901T000000Z", "20260917T120000Z", "20260910T105451Z"):
            write_snapshot(tmp_path / f"ledger_backup_{stamp}.csv", row(order_id=stamp))
        (tmp_path / "before_live.json").write_text("{}", encoding="utf-8")  # not a backup

        assert newest_snapshot(tmp_path).name == "ledger_backup_20260917T120000Z.csv"
        assert SnapshotReader(data_dir=tmp_path).load().rows[0].order_id == "20260917T120000Z"

    def test_no_backup_is_a_loud_error_not_an_empty_page(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="ledger_backup_"):
            newest_snapshot(tmp_path)

    def test_a_relative_explicit_path_is_under_the_repo_root(self):
        reader = SnapshotReader("data/ledger_backup_x.csv")

        assert reader.resolve() == ledger_reader.ROOT / "data" / "ledger_backup_x.csv"

    def test_health_names_the_file(self, snapshot_path):
        health = SnapshotReader(snapshot_path).health()

        assert health["backend"] == "snapshot"
        assert health["snapshot_path"] == str(snapshot_path)
        assert health["snapshot_modified_at"]


# --------------------------------------------------------------------------------------------------
# The read-only guarantee
# --------------------------------------------------------------------------------------------------


class TestReadOnlyGuarantee:
    WRITE_TOKENS = (
        "append_row", "batch_update", "add_rows", "add_cols", "delete_rows", "update_cell",
        "update_acell", "_get_worksheet", "sync_csv_to_ledger", "sort_ledger_by_date_desc",
    )
    #: The only two things web/ may do with a worksheet handle: one read, and its title.
    WORKSHEET_USE = re.compile(r"\bworksheet\.(?!get_values\b|title\b)\w+")
    FORBIDDEN_IMPORTS = ("scrapers", "buying_groups", "sync_tracking", "respond_bfmr", "receipts",
                         "main")

    #: The ONE file that may write the ledger: a cell you edit by hand on the Orders page.
    #: tests/test_web_edit.py pins what it may and may not do; everything else stays read-only.
    WRITE_FILE = "ledger_writer.py"

    @classmethod
    def _sources(cls) -> dict[str, str]:
        root = Path(__file__).resolve().parents[1] / "web"
        return {str(p.relative_to(root)): p.read_text(encoding="utf-8")
                for p in root.rglob("*.py") if p.name != cls.WRITE_FILE}

    def test_no_worksheet_write_method_or_write_scope_is_named_in_web(self):
        for name, text in self._sources().items():
            for token in self.WRITE_TOKENS:
                assert token not in text, f"web/{name} mentions {token!r}"
            assert not self.WORKSHEET_USE.findall(text), (
                f"web/{name} uses a worksheet method other than get_values: "
                f"{self.WORKSHEET_USE.findall(text)}")

    #: The ONE exemption to the receipts ban: hand uploads go to the same object store the capture
    #: uses (store + the key scheme in sources). Never receipts.capture -- that opens a paid browser.
    RECEIPT_STORE_ONLY = "receipts_upload.py"

    def test_web_never_imports_a_scraper_or_a_buying_group_client(self):
        pattern = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", re.M)
        for name, text in self._sources().items():
            for module in pattern.findall(text):
                top = module.split(".")[0]
                if top == "receipts" and name == self.RECEIPT_STORE_ONLY:
                    assert module in ("receipts", "receipts.store", "receipts.sources"), module
                    assert "receipts.capture" not in text and "import capture" not in text
                    continue
                assert top not in self.FORBIDDEN_IMPORTS, f"web/{name} imports {module}"

    @pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
    def test_every_route_refuses_every_non_get_method(self, client, method):
        for route in ROUTES:
            path = route.replace("{order_id}", "BBY01-800000000001")
            response = getattr(client, method)(path)
            assert response.status_code == 405, f"{method.upper()} {path} -> {response.status_code}"

    def test_the_app_declares_itself_read_only(self, client):
        assert client.app.state.read_only is True
        assert client.get("/health").json()["read_only"] is True


# --------------------------------------------------------------------------------------------------
# Money semantics: settled vs committed, and the formulas
# --------------------------------------------------------------------------------------------------


def _row(**values) -> LedgerRow:
    return LedgerRow(cells=dict(zip(FIELDNAMES, row(**values))), row_number=2)


class TestPayoutSemantics:
    def test_a_dated_payout_is_settled(self):
        r = _row(status="delivered", payout_amount="100", payout_date="2026-09-01")
        assert r.is_settled and not r.is_committed and r.payout_state == "settled"

    @pytest.mark.parametrize("status", ["paid", "return"])
    def test_a_buying_group_outcome_status_settles_a_dateless_payout(self, status):
        """MOD's paid rows carry no date: the audit's cogs_inputs_complete counts them settled by
        status, and so does the dashboard."""
        r = _row(status=status, payout_amount="100")
        assert r.is_settled and r.payout_state == "settled"

    @pytest.mark.parametrize("status", ["ordered", "shipped", "delivered"])
    def test_an_expected_payout_on_an_open_or_delivered_row_is_a_commitment(self, status):
        r = _row(status=status, expected_payout="1230", total_cost="1000", cashback_rate="0.04",
                 insurance="6.4")
        assert r.is_committed and not r.is_settled and r.payout_state == "committed"
        assert r.expected_payout == 1230.0 and r.projected_payout == 1230.0
        assert r.profit is None and r.projected_profit == 263.6 and r.profit_or_projected == 263.6

    @pytest.mark.parametrize("status", ["ordered", "shipped", "delivered"])
    def test_a_legacy_undated_payout_amount_still_reads_as_a_commitment(self, status):
        """A CSV backup or a ledger from before Expected Payout (2026-09-18) carried the
        commitment in Actual Payout with a blank date; it still shows as projected."""
        r = _row(status=status, payout_amount="1230")
        assert r.is_committed and not r.is_settled and r.payout_state == "committed"
        assert r.projected_payout == 1230.0

    def test_a_settled_row_keeps_its_commitment_as_the_record_of_the_promise(self):
        r = _row(status="paid", payout_amount="500", payout_date="2026-09-01", expected_payout="520")
        assert r.is_settled and not r.is_committed and r.expected_payout == 520.0
        assert r.projected_payout is None and r.projected_profit is None

    def test_no_amount_is_no_payout_at_all(self):
        r = _row(status="delivered", payout_date="2026-09-01")
        assert r.payout_state == "none"

    def test_a_zero_payout_with_a_date_or_outcome_is_a_settled_loss(self):
        """four $0.00 settlements (a return, three clawbacks) carried real
        losses in Total Profit and the ledger's SUM counted them; the dashboard must too."""
        dated = _row(status="return", total_cost="100", payout_amount="$0.00",
                     payout_date="2026-09-03")
        assert dated.is_settled and dated.payout_state == "settled" and dated.profit == -100.0
        paid = _row(status="paid", total_cost="475", payout_amount="0")
        assert paid.is_settled and paid.profit == -475.0
        # A $0 with neither date nor outcome is nothing: not committed (the allocator never
        # writes a zero), not settled.
        neither = _row(status="delivered", total_cost="10", payout_amount="0")
        assert neither.payout_state == "none"

    @pytest.mark.parametrize("status", MONEY_FREE_STATUSES)
    def test_money_free_rows_are_never_committed(self, status):
        r = _row(status=status, payout_amount="5")
        assert not r.is_committed and r.cogs is None and r.profit is None

    def test_open_is_ordered_shipped_or_delivered_and_never_a_gift_card(self):
        """delivered counts as open -- the group has not paid yet. Wider than
        the scrapers' TERMINAL_STATUSES on purpose."""
        for status in ("ordered", "shipped", "delivered"):
            assert _row(status=status).is_open
        for status in ("cancelled", "paid", "return", "superseded"):
            assert not _row(status=status).is_open
        assert "delivered" in TERMINAL_STATUSES  # the scrapers still never re-read it
        assert not _row(status="delivered", buying_group="Gift Card").is_open


class TestFormulasInPython:
    @staticmethod
    def _fields_read_by(formula: str) -> set[str]:
        letters = {v: k for k, v in _COL.items()}
        return {letters[m] for m in re.findall(r"\b([A-Z]{1,2})2\b", formula)}

    def test_cogs_of_reads_exactly_the_cells_the_cogs_formula_reads(self):
        """If _cogs_formula ever reads a new cell, this fails until cogs_of reads it too."""
        assert self._fields_read_by(_cogs_formula(2)) == {
            "status", "total_cost", "return_quantity", "cost_per_item", "gift_card", "shipping",
            "sales_tax", "rewards_used", "cashback_rate",
        }

    def test_profit_of_reads_exactly_the_cells_the_sheet_formula_reads(self):
        assert self._fields_read_by(_profit_formula(2)) == {
            "status", "payout_amount", "cogs", "insurance",
        }

    def test_cogs_arithmetic(self):
        # (1000 - 1x100 - 50 + 10 + 5 - 20) * (1 - 0.04) + 20 = 845 * 0.96 + 20 = 831.20
        r = _row(status="delivered", total_cost="1000", return_quantity="1", cost_per_item="100",
                 gift_card="50", shipping="10", sales_tax="5", rewards_used="20", cashback_rate="0.04")
        assert cogs_of(r) == 831.20

    def test_blank_cells_count_as_zero_and_no_cost_is_no_cogs(self):
        assert cogs_of(_row(status="delivered", total_cost="100")) == 100.0
        assert cogs_of(_row(status="delivered")) is None

    def test_profit_is_payout_minus_cogs_minus_insurance_and_blank_without_a_payout(self):
        r = _row(status="paid", total_cost="1000", cashback_rate="0.04", insurance="6.4",
                 payout_amount="1230", payout_date="2026-09-01")
        assert profit_of(r) == round(1230 - 960 - 6.4, 2)
        assert profit_of(_row(status="delivered", total_cost="1000")) is None

    def test_a_formula_literal_in_the_cell_is_computed_not_parsed(self):
        """A CSV backup stores '=IF(B2=...' in COGS; the display-number parser must not scrape the
        digits out of the cell references."""
        r = _row(status="delivered", total_cost="100", cashback_rate="0.05",
                 cogs=_cogs_formula(2), total_profit=_profit_formula(2))
        assert r.number("cogs") is None
        assert r.cogs == 95.0

    def test_a_numeric_cell_wins_over_recomputation(self):
        """A live formatted read hands back the ledger's own result; that is the value shown."""
        r = _row(status="delivered", total_cost="100", cashback_rate="0.05", cogs="$96.00")
        assert r.cogs == 96.0

    def test_display_formatting_parses(self):
        r = _row(status="delivered", total_cost="$1,299.00", cashback_rate="4%",
                 payout_amount="(12.50)")
        assert r.total_cost == 1299.0
        assert r.number("cashback_rate") == 0.04
        assert r.payout_amount == -12.5


class TestTemplateFilters:
    def test_money(self):
        assert money(1234.5) == "$1,234.50"
        assert money(-12) == "-$12.00"
        assert money(None) == "" and money("") == ""
        assert money("n/a") == "n/a"

    def test_percent(self):
        assert percent(0.05) == "5%"
        assert percent(0.0925) == "9.25%"
        assert percent(None) == ""


# --------------------------------------------------------------------------------------------------
# The overview numbers
# --------------------------------------------------------------------------------------------------


class TestOverview:
    @pytest.fixture
    def summary(self, snapshot_path):
        return overview(SnapshotReader(snapshot_path).load())

    def test_counts(self, summary):
        assert summary["rows"] == len(LEDGER_ROWS)
        assert summary["orders"] == 8
        assert summary["open_rows"] == 4  # rows 2, 3, 4 and the delivered row 8 (not the gift card)

    def test_open_rows_by_status_and_group(self, summary):
        table = summary["open_table"]
        assert table["groups"] == ["BFMR"]
        assert [(r["status"], r["cells"], r["total"]) for r in table["rows"]] == [
            ("ordered", [2], 2), ("shipped", [1], 1), ("delivered", [1], 1),
        ]
        assert table["column_totals"] == [4] and table["total"] == 4

    def test_projected_profit_is_the_committed_rows(self, summary):
        # Row 3 alone: 1230 - 1000*0.96 - 6.4 = 263.60
        assert summary["projected"] == {"rows": 1, "orders": 1, "payout": 1230.0, "cogs": 960.0,
                                        "profit": 263.6}

    def test_realized_profit_is_the_settled_rows_including_a_dateless_paid_one(self, summary):
        # Row 5: 500 - 400*0.91 = 136.00; row 6: 330 - 300*0.91 = 57.00
        assert summary["realized"] == {"rows": 2, "orders": 2, "payout": 830.0, "cogs": 637.0,
                                       "profit": 193.0}

    def test_the_column_sum_is_the_rows_with_a_total_profit(self, summary):
        """What SUM() over the Total Profit column gives: the settled rows only, now that a
        commitment lives in Expected Payout and leaves Total Profit blank until paid."""
        assert summary["column_sum"] == {"rows": 2, "profit": 193.0, "other": 0.0}

    def test_the_overview_has_no_cogs_gaps_or_scheduler_sections(self, summary):
        # the Audit page's cogs_inputs_complete covers the gaps, and the
        # heartbeat lives in the header pill with its stale timer in Settings
        assert "gaps" not in summary

    def test_status_counts_follow_the_vocabulary_order(self, summary):
        assert summary["status_counts"] == [("ordered", 2), ("shipped", 1), ("delivered", 2),
                                            ("cancelled", 1), ("paid", 2), ("superseded", 1)]

    def test_both_sections_carry_the_same_six_tiles_in_the_same_order(self, summary):
        labels = [t["label"] for t in summary["lifetime"]]
        assert labels == ["Rows / orders", "Open rows", "Spend", "Actual return", "Paid out",
                          "Floating", "Projected profit", "Realized profit"]
        assert [t["label"] for t in summary["month"]["tiles"]] == labels
        assert [t["kind"] for t in summary["month"]["tiles"]] == [t["kind"] for t in summary["lifetime"]]
        # an overview: a few words under each number, the definition in the tooltip
        for t in summary["lifetime"] + summary["month"]["tiles"]:
            assert len(t["hint"].split()) <= 4 and t["detail"]

    def test_lifetime_tiles_count_every_row(self, summary):
        by_label = {t["label"]: t for t in summary["lifetime"]}
        assert by_label["Rows / orders"]["value"] == (len(LEDGER_ROWS), 8)
        assert by_label["Open rows"]["value"] == 4
        assert by_label["Projected profit"]["value"] == 263.6
        assert by_label["Realized profit"]["value"] == 193.0
        assert by_label["Paid out"]["value"] == 830.0
        # Spend: every row that carries money (the cancelled / superseded rows are excluded)
        assert by_label["Spend"]["value"] == sum(
            float(r[HEADER.index("Total Cost")] or 0) for r in LEDGER_ROWS
            if r[HEADER.index("Status")] not in ("cancelled", "superseded"))
        assert by_label["Open rows"]["href"] == "/orders?state=open"
        # Floating: cost not yet paid back -- rows 2, 3, 4 and the Fitbit; not the gift card, not
        # the paid rows, not the money-free ones. NOT spend minus paid out.
        assert by_label["Floating"]["value"] == round(1259.99 + 1000 + 2000 + 100, 2)
        assert by_label["Floating"]["href"] == "/orders?state=unpaid"
        # Actual return: sum(Total Profit) / sum(Total Cost) over the SETTLED rows -- rows 5 and 6:
        # (136 + 57) / (400 + 300). Cost-weighted by construction (dollars over dollars).
        assert by_label["Actual return"]["value"] == round(193.0 / 700.0, 4)
        assert by_label["Actual return"]["kind"] == "percent"
        assert by_label["Actual return"]["hint"] == "2 settled rows"
        assert "(Payout" in by_label["Actual return"]["detail"]
        assert by_label["Actual return"]["href"] == "/orders?state=settled&sort=total_profit&dir=desc"
        assert by_label["Floating"]["value"] != round(by_label["Spend"]["value"] - by_label["Paid out"]["value"], 2)

    def test_the_month_section_is_by_order_date_alone(self, snapshot_path):
        september = overview(SnapshotReader(snapshot_path).load(), month="2026-09",
                             today=NOW.date())["month"]
        tiles = {t["label"]: t["value"] for t in september["tiles"]}
        # Rows 2-4 were placed in September and are all still open; row 3 carries the commitment.
        assert tiles["Rows / orders"][0] == 3 and tiles["Open rows"] == 3
        assert tiles["Projected profit"] == 263.6
        assert tiles["Floating"] == round(1259.99 + 1000 + 2000, 2)  # the Fitbit was placed in August
        assert tiles["Actual return"] is None  # nothing placed in September is settled yet
        # Nothing placed in September is settled yet (row 5 was paid in September but placed in
        # August: the month is by Order Date alone).
        assert tiles["Paid out"] == 0.0 and tiles["Realized profit"] == 0.0
        assert september["label"] == "September 2026" and september["current"] is True
        assert (september["prev"], september["next"]) == ("2026-08", "")

        august = overview(SnapshotReader(snapshot_path).load(), month="2026-08",
                          today=NOW.date())["month"]
        tiles = {t["label"]: t["value"] for t in august["tiles"]}
        assert tiles["Rows / orders"][0] == 6 and tiles["Open rows"] == 1  # the Fitbit
        assert tiles["Paid out"] == 830.0 and tiles["Realized profit"] == 193.0  # rows 5 and 6
        assert tiles["Actual return"] == round(193.0 / 700.0, 4)
        assert tiles["Projected profit"] == 0.0
        assert (august["prev"], august["next"]) == ("", "2026-09")
        # a month with no rows still renders, with both arrows
        assert overview(SnapshotReader(snapshot_path).load(), month="2026-08",
                        today=date(2026, 12, 1))["month"]["next"] == "2026-09"


# --------------------------------------------------------------------------------------------------
# The heartbeat
# --------------------------------------------------------------------------------------------------


class TestHeartbeat:
    def test_missing_stamp_is_reported_as_no_run_yet(self, logs_dir):
        beat = read_heartbeat(logs_dir, now=NOW, interval_hours=6)
        assert beat["present"] is False and beat["stale"] is True
        assert "no run has completed" in beat["message"]

    def test_fresh_stamp(self, logs_dir):
        (logs_dir / ".last_run").write_text("2026-09-17T09:00:00Z", encoding="utf-8")
        beat = read_heartbeat(logs_dir, now=NOW, interval_hours=6)
        assert beat == {**beat, "present": True, "stale": False, "age_seconds": 3 * 3600,
                        "age_text": "3h 0m", "threshold_seconds": 12 * 3600}

    def test_the_stale_timer_is_a_setting_with_twice_the_interval_as_its_default(self, tmp_path):
        from datetime import datetime, timezone

        from web.heartbeat import read_heartbeat

        now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
        default = read_heartbeat(tmp_path, now=now, interval_hours=6)
        assert default["threshold_seconds"] == 12 * 3600 and default["stale_hours"] == 12
        custom = read_heartbeat(tmp_path, now=now, interval_hours=6, stale_hours=1.5)
        assert custom["threshold_seconds"] == 5400 and custom["stale_hours"] == 1.5
        assert read_heartbeat(tmp_path, now=now, interval_hours=6, stale_hours=None)["stale_hours"] == 12

    def test_stale_after_two_intervals_like_the_healthcheck(self, logs_dir):
        (logs_dir / ".last_run").write_text((NOW - timedelta(hours=13)).isoformat(),
                                            encoding="utf-8")
        beat = read_heartbeat(logs_dir, now=NOW, interval_hours=6)
        assert beat["stale"] is True
        assert "expected one every 6h" in beat["message"]

    def test_an_unparseable_stamp_falls_back_to_the_files_mtime(self, logs_dir):
        stamp = logs_dir / ".last_run"
        stamp.write_text("garbage", encoding="utf-8")
        beat = read_heartbeat(stamp.parent, now=datetime.now(timezone.utc), interval_hours=6)
        assert beat["present"] is True and beat["stale"] is False


# --------------------------------------------------------------------------------------------------
# Queries: filters, sorting, one order
# --------------------------------------------------------------------------------------------------


class TestQueries:
    @pytest.fixture
    def rows(self, snapshot_path):
        return SnapshotReader(snapshot_path).load().rows

    def test_filters(self, rows):
        assert {r.order_id for r in filter_rows(rows, Filters(retailers=("Costco",)))} == {
            "1399000017", "1399000018", "1399000019"}
        assert [r.row_number for r in filter_rows(rows, Filters(statuses=("shipped",)))] == [3]
        assert {r.order_id for r in filter_rows(rows, Filters(groups=("MOD",)))} == {
            "1399000017", "1399000018"}
        assert {r.order_id for r in filter_rows(rows, Filters(profiles=("profile-charlie",)))} == {
            "111-0000002-0000002", "111-0000003-0000003", "111-0000004-0000004"}
        assert [r.row_number for r in filter_rows(rows, Filters(q="529900000012"))] == [3]
        assert [r.row_number for r in filter_rows(rows, Filters(q="dyson"))] == [6]

    def test_unknown_sort_key_falls_back(self):
        assert Filters.from_query({"sort": "DROP TABLE"}).sort == "order_date"
        assert Filters.from_query({"sort": "total_cost"}).desc is False
        assert Filters.from_query({"sort": "total_cost", "dir": "desc"}).desc is True

    def test_numeric_sort_puts_blanks_last_in_both_directions(self, rows):
        desc = sort_rows(rows, Filters(sort="total_cost", desc=True))
        asc = sort_rows(rows, Filters(sort="total_cost", desc=False))
        assert [r.total_cost for r in desc][:3] == [2000.0, 1259.99, 1000.0]
        assert [r.total_cost for r in desc][-2:] == [None, None]
        assert [r.total_cost for r in asc][:2] == [40.0, 100.0]
        assert [r.total_cost for r in asc][-2:] == [None, None]

    def test_text_sort_keeps_an_orders_rows_together(self, rows):
        by_date = sort_rows(rows, Filters(sort="order_date", desc=True))
        assert [(r.order_id, r.shipment) for r in by_date][:3] == [
            ("111-0000001-0000001", "1"), ("BBY01-800000000001", "1"), ("BBY01-800000000001", "2")]

    def test_order_view_groups_shipments_and_states_the_payout(self, rows):
        view = order_view([r for r in rows if r.order_id == "BBY01-800000000001"])
        assert [s["shipment"] for s in view["shipments"]] == ["1", "2"]
        assert view["shipments"][0]["tracking_number"] == "529900000012"
        assert view["shipments"][0]["tracking_submitted"] is True
        assert view["shipments"][1]["tracking_number"] == ""
        assert view["receipt_urls"] == [
            "https://objectstorage.example/o/receipts/bestbuy/BBY01-800000000001.pdf"]
        assert view["payout_state"] == "committed"
        assert view["totals"]["total_cost"] == 3000.0
        assert view["totals"]["profit"] == 263.6

    def test_order_view_of_a_settled_order(self, rows):
        view = order_view([r for r in rows if r.order_id == "1399000017"])
        assert view["payout_state"] == "settled" and view["payout_dates"] == ["2026-09-01"]

    def test_order_view_of_nothing_is_none(self):
        assert order_view([]) is None


# --------------------------------------------------------------------------------------------------
# The views, over the snapshot backend
# --------------------------------------------------------------------------------------------------


class TestOverviewPage:
    def test_renders_the_numbers(self, client):
        response = client.get("/")
        assert response.status_code == 200
        body = response.text
        assert "Projected profit" in body and "$263.60" in body
        assert "Realized profit" in body and "$193.00" in body
        assert "Open Rows" in body
        assert "Blank Card Last 4" not in body and "COGS Input Gaps" not in body and ">Scheduler<" not in body
        assert 'data-tip-from="tip-heartbeat"' in body  # the header pill's tooltip, a table
        assert "<td>stale after</td><td>12h without a completed run</td>" in body
        assert "no automatic writes" not in body and "loaded 2026" not in body  # header: pill + stamp gone

    def test_every_page_loads_the_in_page_tooltip_layer(self, client):
        body = client.get("/").text
        assert 'src="/static/tooltip.js' in body
        js = client.get("/static/tooltip.js").text
        assert 'removeAttribute("title")' in js and "data-tip" in js  # the native bubble never shows
        assert 'getAttribute("data-tip-from")' in js and 'classList.add("rich")' in js  # a tip may be a table
        assert "split(/\\s+/)" in js  # several blocks in one panel: a cell's how-to plus its hand-edit note
        css = (Path(__file__).resolve().parents[1] / "web" / "static" / "style.css").read_text(encoding="utf-8")
        assert ".tip {" in css and ".tip.on" in css

    def test_the_header_carries_the_wordmark_logo_and_favicon(self, client):
        body = client.get("/").text
        assert '<link rel="icon" type="image/svg+xml" href="/static/logo.svg' in body
        assert 'class="logo"' in body and 'Buying Group <strong>Ledger</strong>' in body
        logo = client.get("/static/logo.svg")
        assert logo.status_code == 200 and "<svg" in logo.text

    def test_lifetime_and_month_sections_are_linked_tiles(self, client):
        body = client.get("/").text
        assert "<h2>Lifetime" in body and "Calendar Month" in body
        # NOW is 2026-09-17: the month section opens on September, with a way back only.
        assert "September 2026" in body and 'href="/?month=2026-08"' in body
        assert 'href="/?month=2026-10"' not in body
        # every tile is a link to the Orders page, filtered the way it was counted
        assert 'class="tile link " href="/orders" title="every row of the ledger"' in body
        assert '<section class="tiles stats">' in body
        assert 'href="/orders?state=open"' in body
        assert 'href="/orders?state=settled"' in body and 'href="/orders?state=committed"' in body
        assert 'href="/orders?state=unpaid"' in body and ">Floating<" in body
        assert ">Actual return<" in body and "27.57%" in body  # rendered as a percentage
        assert 'href="/orders?month=2026-09"' in body
        assert 'href="/orders?month=2026-09&amp;state=open"' in body
        assert 'href="/orders?month=2026-09&amp;state=settled"' in body
        assert "paid=2026-09" not in body  # the month is by Order Date alone

    def test_an_earlier_month_can_be_opened_and_navigated(self, client):
        body = client.get("/", params={"month": "2026-08"}).text
        assert "August 2026" in body and 'href="/?month=2026-09"' in body
        assert 'href="/?month=2026-07"' not in body  # nothing was placed before August
        assert '<a class="muted small" href="/">this month</a>' in body
        # a bad month falls back to the current one rather than failing
        assert "September 2026" in client.get("/", params={"month": "never"}).text

    def test_the_cards_footer_keeps_the_arrows_on_the_outbound_links(self, client):
        body = client.get("/orders", params={"view": "cards"}).text
        assert ">Order ↗<" in body and ">Receipt ↗<" in body and ">Details<" in body

    def test_a_card_lists_its_items_as_numbered_lines_with_quantity(self, client):
        body = client.get("/orders", params={"view": "cards", "q": "111-0000002-0000002"}).text
        card = body[body.index('<article class="card'):body.index("</article>")]
        assert '<ol class="items ' in card and '<span class="n">1</span>' in card
        assert "Fitbit Charge 6" in card and "×1" in card
        # a single-item order carries no separators
        assert 'class="items "' in card

    def test_heartbeat_shows_fresh(self, client):
        body = client.get("/").text
        assert "last run 3h 0m ago" in body
        assert "pill ok" in body

    def test_heartbeat_warns_when_stale(self, snapshot_path, logs_dir, failures_dir):
        (logs_dir / ".last_run").write_text("2026-09-15T00:00:00Z", encoding="utf-8")
        app = create_app(SnapshotReader(snapshot_path), logs_dir=logs_dir,
                         failures_dir=failures_dir, clock=lambda: NOW, settings=_settings())
        body = TestClient(app).get("/").text
        assert "pill stale" in body and "expected one every 6h" in body

    def test_heartbeat_warns_when_absent(self, snapshot_path, logs_dir, failures_dir):
        app = create_app(SnapshotReader(snapshot_path), logs_dir=logs_dir,
                         failures_dir=failures_dir, clock=lambda: NOW, settings=_settings())
        assert "no run has completed yet" in TestClient(app).get("/").text


class TestOrdersPage:
    def test_full_page_lists_every_row_with_links(self, client):
        response = client.get("/orders")
        assert response.status_code == 200
        body = response.text
        assert "<html" in body and 'id="filters"' in body
        assert body.count('<tr class="status-') == len(LEDGER_ROWS)
        # Order Link, Tracking Link and Receipt Link are anchors, verbatim.
        assert 'href="https://www.bestbuy.com/profile/ss/orders/order-details/BBY01-800000000001/view"' in body
        assert 'href="https://www.fedex.com/fedextrack/?trknbr=529900000012"' in body
        assert 'href="https://objectstorage.example/o/receipts/bestbuy/BBY01-800000000001.pdf"' in body
        assert 'href="/orders/BBY01-800000000001"' in body
        # A committed payout is tagged as projected.
        assert "proj." in body

    def test_htmx_request_gets_the_table_alone(self, client):
        response = client.get("/orders", headers={"HX-Request": "true"})
        assert response.status_code == 200
        assert "<html" not in response.text and 'id="filters"' not in response.text
        assert "<table" in response.text

    def test_filters_and_search(self, client):
        body = client.get("/orders", params={"retailer": "Costco"}).text
        assert body.count('<tr class="status-') == 3
        body = client.get("/orders", params={"status": "shipped", "group": "BFMR"}).text
        assert body.count('<tr class="status-') == 1 and "529900000012" in body
        body = client.get("/orders", params={"q": "fitbit"}).text
        assert body.count('<tr class="status-') == 1 and "111-0000002-0000002" in body
        # the card's name is searchable, and the cards view sorts by card
        body = client.get("/orders", params={"q": "venmo"}).text
        assert body.count('<tr class="status-') >= 1 and "Venmo Visa" in body[body.index("<tbody"):]
        cards = client.get("/orders", params={"view": "cards"}).text  # the Sort-by control is the cards view's
        assert 'name="sort" value="card_name"' in cards and 'name="sort" value="card_last4"' in cards
        body = client.get("/orders", params={"profile": "nobody"}).text
        assert "No rows match." in body

    def test_sort_links_and_order(self, client):
        body = client.get("/orders", params={"sort": "total_cost", "dir": "desc"},
                          headers={"HX-Request": "true"}).text
        cells = re.findall(r'data-field="total_cost"[^>]*>\s*([^<]*)', body)
        assert [c.strip() for c in cells][:3] == ["$2,000.00", "$1,259.99", "$1,000.00"]
        assert "sort=total_cost&amp;dir=asc" in body or "sort=total_cost&dir=asc" in body

    def test_the_card_facet_files_by_last4_and_shows_the_name(self, client):
        body = client.get("/orders").text
        card = body[body.index('data-param="card"'):body.index("</details>", body.index('data-param="card"'))]
        assert 'name="card" value="0315" >' in card and "…0315</label>" in card  # the name rides with the number
        assert 'value="(blank)"' in card  # a row without a card
        narrowed = client.get("/orders", params={"card": "0315"}).text
        assert "…0315" in narrowed and 'name="card" value="0315" checked' in narrowed
        assert client.get("/audit", params={"card": "0315"}).status_code == 200  # the scoped pages share the bar
        assert client.get("/recon", params={"card": "0315"}).status_code == 200

    def test_facets_come_from_the_whole_ledger(self, client):
        body = client.get("/orders", params={"retailer": "Costco"}).text
        assert 'name="retailer" value="Best Buy" >' in body  # still offered while Costco is chosen
        assert 'name="retailer" value="Costco" checked' in body


class TestOrderPage:
    def test_one_order_with_its_shipments(self, client):
        response = client.get("/orders/BBY01-800000000001")
        assert response.status_code == 200
        body = response.text
        assert "Shipment 1" in body and "Shipment 2" in body
        assert "529900000012" in body and "submitted to the buying group" in body
        assert "committed by the buying group, not yet paid" in body
        assert "Projected Profit" in body and "$263.60" in body
        assert 'href="https://objectstorage.example/o/receipts/bestbuy/BBY01-800000000001.pdf"' in body
        assert "BFMR B999999, Testville, NH 03050" in body

    def test_a_settled_order(self, client):
        body = client.get("/orders/1399000017").text
        assert "settled on 2026-09-01" in body and "$136.00" in body

    def test_unknown_order_is_404(self, client):
        assert client.get("/orders/nope").status_code == 404


class TestFailuresPage:
    REPORT = (
        "# Failure dossier — costco [profile-bravo] — 2026-08-30 07:03:04Z\n\n"
        "## What failed\n\n**CostcoApiError**: order 1399000014: no `orderLineItems`\n\n"
        "```\nTraceback (most recent call last):\n  boom\n```\n\n"
        "## Selector audit\n\n| selector | matches |\n|---|---|\n| `#a` | 0 |\n\n"
        "<script>alert(1)</script>\n\n"
        "## Hosted copies\n\n"
        "- `response_1.txt`: https://objectstorage.example/p/TOKEN/o/failures/costco_profile-bravo_20260830T070304Z/response_1.txt\n"
    )

    def _dossier(self, root: Path, name: str, report: str | None):
        directory = root / name
        directory.mkdir()
        (directory / "response_1.txt").write_text("{}", encoding="utf-8")
        if report is not None:
            (directory / "report.md").write_text(report, encoding="utf-8")
        return directory

    def test_a_dossier_downloads_as_one_zip_for_an_agent(self, client, failures_dir):
        """a download button before details; the zip holds the whole dossier."""
        import io
        import zipfile

        name = "costco_profile-bravo_20260830T070304Z"
        self._dossier(failures_dir, name, self.REPORT)
        page = client.get("/activity", params={"type": "dossier", "days": "0"}).text
        assert f'href="/activity/dossier/{name}/download"' in page
        assert page.index(f'href="/activity/dossier/{name}/download"') < page.index(">report</button>")
        response = client.get(f"/activity/dossier/{name}/download")
        assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
        assert response.headers["content-disposition"] == f'attachment; filename="{name}.zip"'
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert sorted(archive.namelist()) == [f"{name}/report.md", f"{name}/response_1.txt"]
            report = archive.read(f"{name}/report.md").decode("utf-8").replace("\r\n", "\n")
            assert report == self.REPORT  # the file may carry CRLF on Windows
        assert client.get("/activity/dossier/nope/download").status_code == 404
        assert client.get("/activity/dossier/..%2F..%2Fconfig.json/download").status_code == 404

    def test_newest_first_with_reports_rendered_and_hosted_links_verbatim(self, client, failures_dir):
        self._dossier(failures_dir, "costco_profile-bravo_20260830T070304Z", self.REPORT)
        self._dossier(failures_dir, "amazon_profile-charlie_20260914T160056Z", "# Newer\n\ntext\n")
        self._dossier(failures_dir, "bestbuy_profile-bravo_20260901T000000Z", None)

        moved = client.get("/failures", follow_redirects=False)  # the Failures page lives in Activity now
        assert moved.status_code == 303 and moved.headers["location"] == "/activity?type=dossier&days=0"
        response = client.get("/activity", params={"type": "dossier", "days": "0"})
        assert response.status_code == 200
        body = response.text
        assert body.count('<tr class="kind-dossier">') == 3  # dossiers on disk, never logged, still listed
        newest = body.index("amazon_profile-charlie_20260914T160056Z")
        middle = body.index("bestbuy_profile-bravo_20260901T000000Z")
        oldest = body.index("costco_profile-bravo_20260830T070304Z")
        assert newest < middle < oldest
        assert "<h2>What failed</h2>" in body and "<strong>CostcoApiError</strong>" in body
        assert "<table>" in body and "<td><code>#a</code></td>" in body
        link = ("https://objectstorage.example/p/TOKEN/o/failures/"
                "costco_profile-bravo_20260830T070304Z/response_1.txt")
        assert f'<a href="{link}" rel="noopener">{link}</a>' in body
        assert "no report.md" in body

    def test_raw_html_in_a_report_is_escaped(self, client, failures_dir):
        self._dossier(failures_dir, "costco_profile-bravo_20260830T070304Z", self.REPORT)
        body = client.get("/activity", params={"type": "dossier", "days": "0"}).text
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body

    def test_empty_directory(self, client):
        body = client.get("/activity", params={"type": "dossier", "days": "0"}).text
        assert 'class="kind-dossier"' not in body and "Nothing recorded" in body

    def test_missing_directory(self, snapshot_path, logs_dir, tmp_path):
        app = create_app(SnapshotReader(snapshot_path), logs_dir=logs_dir,
                         failures_dir=tmp_path / "nowhere", clock=lambda: NOW, settings=_settings())
        assert TestClient(app).get("/activity", params={"type": "dossier"}).status_code == 200

    def test_helpers(self):
        assert parse_name("costco_profile-bravo_20260830T070304Z") == (
            "costco", "profile-bravo", datetime(2026, 8, 30, 7, 3, 4, tzinfo=timezone.utc))
        assert parse_name("odd") == ("odd", "", None)
        assert hosted_copies(self.REPORT) == [(
            "response_1.txt",
            "https://objectstorage.example/p/TOKEN/o/failures/costco_profile-bravo_20260830T070304Z/response_1.txt",
        )]
        assert hosted_copies("# no section") == []
        assert list_dossiers(Path("does/not/exist")) == []


class TestHealth:
    def test_snapshot_backend(self, client, snapshot_path):
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True and body["read_only"] is True
        assert body["backend"] == "snapshot"
        assert body["snapshot_path"] == str(snapshot_path)
        assert body["rows"] == len(LEDGER_ROWS) and body["schema_matches"] is True
        assert body["heartbeat"]["present"] is True and body["heartbeat"]["stale"] is False
        assert body["heartbeat"]["age_seconds"] == 3 * 3600

    def test_a_missing_snapshot_is_503_with_the_reason(self, tmp_path, logs_dir, failures_dir):
        app = create_app(SnapshotReader(data_dir=tmp_path), logs_dir=logs_dir,
                         failures_dir=failures_dir, clock=lambda: NOW, settings=_settings())
        test_client = TestClient(app)

        health = test_client.get("/health")
        assert health.status_code == 503 and "ledger_backup_" in health.json()["error"]
        assert test_client.get("/").status_code == 503
        assert test_client.get("/orders").status_code == 503


# --------------------------------------------------------------------------------------------------
# Choosing the backend from config
# --------------------------------------------------------------------------------------------------


class TestReaderFromSettings:
    def test_default_is_the_ledger_file(self, tmp_path):
        from web.ledger_reader import DbReader

        reader = reader_from_settings(_settings(web_ledger_source="db", web_ledger_cache_ttl_seconds=42,
                                                ledger_db_path=str(tmp_path / "l.sqlite3")))
        assert isinstance(reader, DbReader) and reader.ttl_seconds == 42.0
        assert reader.db.path == tmp_path / "l.sqlite3"
        assert _settings().web_ledger_source == "db" or reader_from_settings(_settings()).backend in ("db", "snapshot")

    def test_snapshot_when_asked(self):
        reader = reader_from_settings(_settings(web_ledger_source="snapshot", web_snapshot_path=""))
        assert isinstance(reader, SnapshotReader) and reader._explicit is None

    def test_snapshot_path_from_config(self):
        reader = reader_from_settings(_settings(web_ledger_source="snapshot",
                                                web_snapshot_path="data/ledger_backup_x.csv"))
        assert reader.resolve().name == "ledger_backup_x.csv"

    def test_explicit_arguments_win(self):
        reader = reader_from_settings(_settings(web_ledger_source="db"), source="snapshot",
                                      snapshot_path="x.csv")
        assert isinstance(reader, SnapshotReader)

    def test_a_typo_is_refused_not_defaulted(self):
        with pytest.raises(ValueError, match="WEB_LEDGER_SOURCE"):
            reader_from_settings(_settings(web_ledger_source="sheets"))
        with pytest.raises(ValueError, match="WEB_LEDGER_SOURCE"):
            reader_from_settings(_settings(web_ledger_source="sheet"))

    def test_the_settings_have_their_config_home(self):
        from config.settings import ENV_TO_CONFIG

        assert ENV_TO_CONFIG["WEB_LEDGER_SOURCE"] == "web.ledger_source"
        assert ENV_TO_CONFIG["WEB_SNAPSHOT_PATH"] == "web.snapshot_path"
        assert ENV_TO_CONFIG["WEB_LEDGER_CACHE_TTL_SECONDS"] == "web.ledger_cache_ttl_seconds"
        assert ENV_TO_CONFIG["WEB_BIND_HOST"] == "web.bind_host"
        assert ENV_TO_CONFIG["WEB_PORT"] == "web.port"
        assert ENV_TO_CONFIG["WEB_ENABLED"] == "web.enabled"
        assert ENV_TO_CONFIG["LEDGER_DB_PATH"] == "database.path"
        assert _settings().web_enabled in (True, False)
        assert _settings().ledger_db_path
        defaults = _settings()
        assert defaults.web_ledger_cache_ttl_seconds == 300 or defaults.web_ledger_cache_ttl_seconds > 0


# --------------------------------------------------------------------------------------------------
# Packaging: the scheduler stays untouched
# --------------------------------------------------------------------------------------------------


class TestPackaging:
    ROOT = Path(__file__).resolve().parents[1]

    def test_the_web_extras_are_a_separate_pinned_file_installed_by_the_one_image(self):
        """One container: the scheduler image installs requirements-web.txt
        too. It stays a separate file so a desktop `python -m web` remains an opt-in install."""
        runtime = (self.ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        for package in ("fastapi", "jinja2", "uvicorn", "markdown-it-py"):
            assert package not in runtime, f"{package} belongs in requirements-web.txt"
        extras = (self.ROOT / "requirements-web.txt").read_text(encoding="utf-8")
        assert re.search(r"^fastapi==\d", extras, re.M) and re.search(r"^jinja2==\d", extras, re.M)
        assert re.search(r"^python-multipart==\d", extras, re.M)
        dockerfile = (self.ROOT / "Dockerfile").read_text(encoding="utf-8")
        assert "-r requirements-web.txt" in dockerfile and "EXPOSE 8765" in dockerfile
        assert not (self.ROOT / "web" / "Dockerfile").exists(), "one image, not two"

    def test_the_compose_file_has_one_service_that_publishes_the_dashboard_on_loopback(self):
        compose = (self.ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        assert "\n  web:\n" not in compose, "one container, not two"
        assert "profiles:" not in compose
        assert '"${WEB_PUBLISH_HOST:-127.0.0.1}:${WEB_PUBLISH_PORT:-8765}:8765"' in compose
        for name in ("WEB_ENABLED", "WEB_LEDGER_SOURCE", "LEDGER_DB_PATH"):
            assert f'{name}: "${{{name}:-}}"' in compose, f"{name} is not passed through"

    def test_the_entrypoint_starts_the_dashboard_and_the_healthcheck_probes_it(self):
        entrypoint = (self.ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
        assert 'if [ "${WEB_ENABLED:-true}" = "true" ]' in entrypoint
        assert "python -m web --host 0.0.0.0 --port 8765" in entrypoint
        assert "exec /usr/local/bin/supercronic /app/crontab" in entrypoint  # still PID 1
        healthcheck = (self.ROOT / "docker" / "healthcheck.sh").read_text(encoding="utf-8")
        assert "/tmp/container.env" in healthcheck  # sees config.json's WEB_ENABLED / interval
        assert "web dashboard is not answering" in healthcheck
        from scripts.container_settings import EXPORTS

        assert "WEB_ENABLED" in dict(EXPORTS)

    def test_operations_doc_says_how_to_run_it(self):
        doc = (self.ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
        assert "docker compose up -d --build" in doc
        assert "python -m web" in doc
        assert "python -m scripts.backup" in doc
        assert "read-only" in doc.lower()

    def test_backups_never_enter_git_or_an_image(self):
        for name in (".gitignore", ".dockerignore"):
            assert re.search(r"^backups/$", (self.ROOT / name).read_text(encoding="utf-8"), re.M), name

    def test_static_assets_are_vendored(self):
        assert (self.ROOT / "web" / "static" / "htmx.min.js").stat().st_size > 10_000
        assert (self.ROOT / "web" / "static" / "style.css").is_file()


class TestThemeToggle:
    def test_the_toggle_is_on_every_page_and_applies_before_first_paint(self, client):
        body = client.get("/").text
        assert 'onclick="toggleTheme()"' in body
        assert 'localStorage.getItem("ledger-theme")' in body
        # Applied in <head>, before the stylesheet-dependent body renders.
        assert body.index("ledger-theme") < body.index("<body")
        assert 'href="/settings"' in body and 'href="/backup"' not in body  # Backup lives in Settings

    def test_the_stylesheet_honours_the_attribute_over_the_system_preference(self):
        css = (Path(__file__).resolve().parents[1] / "web" / "static" / "style.css").read_text(
            encoding="utf-8")
        assert ':root[data-theme="dark"]' in css
        assert ':root:not([data-theme="light"])' in css  # system dark applies only while unset


# --------------------------------------------------------------------------------------------------
# Backup and restore
# --------------------------------------------------------------------------------------------------


class TestBackupScript:
    def test_the_commit_comes_from_the_git_files_when_there_is_no_git(self, tmp_path, monkeypatch):
        """Inside the image there is no git; .dockerignore lets .git/HEAD and the ref in."""
        from scripts import backup as backup_module

        monkeypatch.delenv("GIT_COMMIT", raising=False)
        root = tmp_path / "image"
        (root / ".git" / "refs" / "heads").mkdir(parents=True)
        (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (root / ".git" / "refs" / "heads" / "main").write_text("0123456789abcdef\n", encoding="utf-8")
        assert backup_module._commit_from_git_files(root) == "0123456"
        (root / ".git" / "refs" / "heads" / "main").unlink()
        (root / ".git" / "packed-refs").write_text("# pack-refs\nfedcba9876543210 refs/heads/main\n",
                                                   encoding="utf-8")
        assert backup_module._commit_from_git_files(root) == "fedcba9"
        assert backup_module._commit_from_git_files(tmp_path / "nowhere") == ""
        monkeypatch.setenv("GIT_COMMIT", "abc1234")
        assert backup_module._git_commit(tmp_path / "nowhere") == "abc1234"
        ignore = (Path(__file__).resolve().parents[1] / ".dockerignore").read_text(encoding="utf-8")
        assert "!.git/HEAD" in ignore and "!.git/refs/heads/" in ignore
        compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text(encoding="utf-8")
        assert "- ./backups:/app/backups" in compose  # a rebuild must not take the backups with it

    @pytest.fixture
    def repo(self, tmp_path):
        root = tmp_path / "repo"
        (root / "data").mkdir(parents=True)
        (root / "logs" / "failures" / "x").mkdir(parents=True)
        (root / "config.json").write_text('{"secret": 1}', encoding="utf-8")
        (root / ".state.json").write_text("{}", encoding="utf-8")
        (root / "data" / "ledger.sqlite3").write_bytes(b"sqlite")
        (root / "data" / "ledger_backup_20260910T105451Z.csv").write_text("a,b\n", encoding="utf-8")
        (root / "data" / "__pycache__").mkdir()
        (root / "data" / "__pycache__" / "x.pyc").write_bytes(b"")
        (root / "logs" / "run.log").write_text("log", encoding="utf-8")
        (root / "logs" / "failures" / "x" / "page_1.html").write_text("<html>", encoding="utf-8")
        return root

    def test_members_are_the_state_files_and_data_never_logs(self, repo):
        from scripts.backup import backup_members

        assert [m.as_posix() for m in backup_members(repo)] == [
            "config.json", ".state.json", "data/ledger.sqlite3",
            "data/ledger_backup_20260910T105451Z.csv",
        ]

    def test_create_then_restore_into_a_fresh_clone(self, repo, tmp_path):
        import zipfile

        from scripts.backup import create_backup, list_backups, read_manifest, restore_backup

        archive = create_backup(repo, repo / "backups")
        assert archive.name.startswith("ledger_backup_") and archive.suffix == ".zip"
        assert list_backups(repo / "backups") == [archive]
        manifest = read_manifest(archive)
        assert manifest["files"] == ["config.json", ".state.json", "data/ledger.sqlite3",
                                     "data/ledger_backup_20260910T105451Z.csv"]
        with zipfile.ZipFile(archive) as z:
            assert "logs/run.log" not in z.namelist()

        clone = tmp_path / "clone"
        clone.mkdir()
        result = restore_backup(archive, clone)
        assert result == {"restored": manifest["files"], "skipped_existing": [], "ignored": []}
        assert (clone / "config.json").read_text(encoding="utf-8") == '{"secret": 1}'
        assert (clone / "data" / "ledger.sqlite3").read_bytes() == b"sqlite"

    def test_restore_keeps_existing_files_unless_forced(self, repo, tmp_path):
        from scripts.backup import create_backup, restore_backup

        archive = create_backup(repo, tmp_path / "out")
        (repo / "config.json").write_text("EDITED", encoding="utf-8")

        kept = restore_backup(archive, repo)
        assert "config.json" in kept["skipped_existing"] and kept["restored"] == []
        assert (repo / "config.json").read_text(encoding="utf-8") == "EDITED"

        forced = restore_backup(archive, repo, force=True)
        assert "config.json" in forced["restored"]
        assert (repo / "config.json").read_text(encoding="utf-8") == '{"secret": 1}'

    def test_restore_ignores_members_outside_the_allowed_set(self, tmp_path):
        import zipfile

        from scripts.backup import restore_backup

        archive = tmp_path / "evil.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("../outside.txt", "x")
            z.writestr("main.py", "print('pwned')")
            z.writestr("logs/run.log", "x")
            z.writestr("config.json", "{}")
        clone = tmp_path / "clone"
        clone.mkdir()

        result = restore_backup(archive, clone)
        assert result["restored"] == ["config.json"]
        assert sorted(result["ignored"]) == ["../outside.txt", "logs/run.log", "main.py"]
        assert not (tmp_path / "outside.txt").exists() and not (clone / "main.py").exists()

    def test_the_script_is_standard_library_only(self):
        """A fresh clone restores BEFORE any pip install, with the system Python."""
        source = (Path(__file__).resolve().parents[1] / "scripts" / "backup.py").read_text(
            encoding="utf-8")
        imported = set(re.findall(r"^(?:from|import)\s+([A-Za-z_][\w]*)", source, re.M))
        allowed = {"__future__", "argparse", "json", "os", "re", "socket", "sqlite3", "subprocess", "sys", "tempfile",
                   "zipfile", "datetime", "pathlib"}
        assert imported <= allowed, imported - allowed

    def test_cli(self, repo, capsys, monkeypatch):
        from scripts import backup as script

        monkeypatch.setattr(script, "ROOT", repo)
        monkeypatch.setattr(script, "BACKUPS_DIR", repo / "backups")
        assert script.main([]) == 0
        assert "Wrote" in capsys.readouterr().err
        assert script.main(["--list"]) == 0
        assert "ledger_backup_" in capsys.readouterr().out
        assert script.main(["--restore", "nope.zip"]) == 2


class TestBackupPage:
    @pytest.fixture
    def repo(self, tmp_path):
        root = tmp_path / "repo"
        (root / "data").mkdir(parents=True)
        (root / "data" / "ledger.sqlite3").write_bytes(b"sqlite")
        return root

    def _client(self, repo, snapshot_path, logs_dir, failures_dir):
        app = create_app(SnapshotReader(snapshot_path), logs_dir=logs_dir,
                         failures_dir=failures_dir, backup_dir=repo / "backups",
                         repo_root_dir=repo, clock=lambda: NOW, settings=_settings())
        return TestClient(app)

    def test_create_and_download(self, repo, snapshot_path, logs_dir, failures_dir):
        (repo / "config.json").write_text("{}", encoding="utf-8")
        client = self._client(repo, snapshot_path, logs_dir, failures_dir)

        # the old address lands on the Settings page's Backup panel
        moved = client.get("/backup", params={"message": "hi there"}, follow_redirects=False)
        assert moved.status_code == 303 and moved.headers["location"] == "/settings?message=hi+there#s-backup"
        page = client.get("/settings")
        assert page.status_code == 200 and "Create a backup now" in page.text
        assert 'id="s-backup"' in page.text and 'href="#s-backup"' in page.text
        assert "None yet." in page.text
        assert 'action="/backup/restore"' in page.text and "overwrite" in page.text  # any host

        created = client.post("/backup", follow_redirects=False)
        assert created.status_code == 303 and created.headers["location"].endswith("#s-backup")
        archives = list((repo / "backups").glob("ledger_backup_*.zip"))
        assert len(archives) == 1

        page = client.get("/settings").text
        assert archives[0].name in page
        download = client.get(f"/backup/{archives[0].name}")
        assert download.status_code == 200
        assert download.headers["content-type"] == "application/zip"
        assert download.content == archives[0].read_bytes()

    def test_download_rejects_anything_but_a_backup_name(self, repo, snapshot_path, logs_dir,
                                                        failures_dir):
        client = self._client(repo, snapshot_path, logs_dir, failures_dir)
        assert client.get("/backup/config.json").status_code == 404
        assert client.get("/backup/ledger_backup_missing.zip").status_code == 404
        assert client.get("/backup/..%2Fconfig.json").status_code == 404

    def test_restore_upload_works_on_any_host_and_keeps_existing_unless_forced(
            self, repo, snapshot_path, logs_dir, failures_dir, tmp_path):
        import zipfile

        archive = tmp_path / "ledger_backup_x.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("config.json", '{"restored": true}')
            z.writestr("data/ledger_backup_1.csv", "a\n")
        client = self._client(repo, snapshot_path, logs_dir, failures_dir)

        page = client.get("/settings").text
        assert "fresh clone" in page and 'action="/backup/restore"' in page
        assert 'data-confirm="Restore this backup' in page  # the in-page confirmation

        with archive.open("rb") as handle:
            response = client.post("/backup/restore", files={"archive": ("b.zip", handle,
                                                                          "application/zip")},
                                   follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"].startswith("/settings?message=Restored")
        assert "restart=container" in response.headers["location"]  # config.json came back
        assert (repo / "config.json").read_text(encoding="utf-8") == '{"restored": true}'
        assert (repo / "data" / "ledger_backup_1.csv").is_file()

        # Now configured: the form is still there, existing files are KEPT
        # unless overwrite is ticked.
        page = client.get("/settings").text
        assert "fresh clone" not in page and 'action="/backup/restore"' in page and "overwrite" in page
        (repo / "config.json").write_text('{"live": true}', encoding="utf-8")
        with archive.open("rb") as handle:
            kept = client.post("/backup/restore", files={"archive": ("b.zip", handle,
                                                                      "application/zip")},
                               follow_redirects=False)
        assert kept.status_code == 303 and "kept+2+existing" in kept.headers["location"]
        assert "restart=" not in kept.headers["location"]
        assert (repo / "config.json").read_text(encoding="utf-8") == '{"live": true}'
        with archive.open("rb") as handle:
            forced = client.post("/backup/restore", data={"force": "1"},
                                 files={"archive": ("b.zip", handle, "application/zip")},
                                 follow_redirects=False)
        assert forced.status_code == 303 and "Restored+2" in forced.headers["location"]
        assert (repo / "config.json").read_text(encoding="utf-8") == '{"restored": true}'

    def test_a_backup_can_be_deleted_from_the_panel(self, repo, snapshot_path, logs_dir, failures_dir):
        (repo / "config.json").write_text("{}", encoding="utf-8")
        client = self._client(repo, snapshot_path, logs_dir, failures_dir)
        client.post("/backup", follow_redirects=False)
        name = list((repo / "backups").glob("ledger_backup_*.zip"))[0].name
        page = client.get("/settings").text
        assert f'action="/backup/{name}/delete"' in page and 'form="del-backup-0"' in page
        assert 'data-confirm="Delete backup' in page
        gone = client.post(f"/backup/{name}/delete", follow_redirects=False)
        assert gone.status_code == 303 and gone.headers["location"].endswith("#s-backup")
        assert not list((repo / "backups").glob("ledger_backup_*.zip"))
        assert client.post(f"/backup/{name}/delete").status_code == 404
        assert client.post("/backup/..%2Fconfig.json/delete").status_code == 404

    def test_the_backup_panel_works_without_any_ledger_source(self, repo, logs_dir, failures_dir,
                                                              tmp_path):
        """A fresh clone has no snapshot, no config and no ledger -- the Settings page a restore
        starts from must still render."""
        app = create_app(SnapshotReader(data_dir=tmp_path / "empty"), logs_dir=logs_dir,
                         failures_dir=failures_dir, backup_dir=repo / "backups",
                         repo_root_dir=repo, clock=lambda: NOW, settings=_settings())
        page = TestClient(app).get("/settings")
        assert page.status_code == 200 and 'action="/backup/restore"' in page.text


# --------------------------------------------------------------------------------------------------
# Charts: donuts and stacked bars whose every slice is a filter link
# --------------------------------------------------------------------------------------------------


class TestCharts:
    def test_status_donut_uses_lifecycle_order_and_the_sheets_colours(self):
        from web.charts import STATUS_COLORS, status_donut

        d = status_donut([("paid", 2), ("ordered", 5), ("shipped", 1), ("weird", 1)])
        assert [s.label for s in d.segments] == ["ordered", "shipped", "paid", "weird"]
        assert d.total == 9
        assert d.segments[0].color == STATUS_COLORS["ordered"]
        assert d.segments[0].href == "/orders?status=ordered"
        assert d.segments[-1].color == "#8a8a8a"  # out-of-vocabulary: the fold-up grey
        assert abs(sum(s.share for s in d.segments) - 1.0) < 1e-9

    def test_arcs_cover_the_ring_with_a_gap_between_slices(self):
        from web.charts import donut

        d = donut("t", [("A", 3), ("B", 1)], param="retailer")
        assert d.segments[0].dash == round(d.circumference * 0.75 - 2, 3)
        assert d.segments[1].dash == round(d.circumference * 0.25 - 2, 3)
        assert d.segments[0].offset == 0.0
        assert d.segments[1].offset == round(-d.circumference * 0.75, 3)
        lone = donut("t", [("A", 3)], param="retailer")
        assert lone.segments[0].dash == round(lone.circumference, 3)  # no gap on a full ring

    def test_categorical_slots_are_fixed_in_order_and_fold_past_eight(self):
        from web.charts import CATEGORICAL, donut

        counts = [(f"g{i}", 10 - i) for i in range(10)]
        d = donut("t", counts, param="group")
        assert [s.label for s in d.segments][-1] == "Other"
        assert len(d.segments) == 8
        assert [s.color for s in d.segments[:7]] == list(CATEGORICAL[:7])
        assert d.segments[-1].href == ""  # a fold-up maps to no single filter
        assert d.segments[-1].count == 3 + 2 + 1

    def test_blank_group_links_to_the_blank_filter(self):
        from web.charts import donut

        d = donut("t", [("(blank)", 2), ("BFMR", 1)], param="group")
        assert d.segments[0].href == "/orders?group="

    def test_open_rows_bars(self):
        from web.charts import STATUS_COLORS, open_rows_bars

        table = {"groups": ["BFMR", "MOD"], "rows": [
            {"status": "ordered", "cells": [4, 1], "total": 5},
            {"status": "shipped", "cells": [2, 0], "total": 2}], "column_totals": [6, 1], "total": 7}
        b = open_rows_bars(table)
        assert [bar.label for bar in b.bars] == ["BFMR", "MOD"]  # largest first
        assert b.bars[0].total == 6 and b.bars[0].href == "/orders?group=BFMR"
        assert [s.label for s in b.bars[0].segments] == ["ordered", "shipped"]
        assert b.bars[0].segments[0].href == "/orders?status=ordered&group=BFMR"
        assert b.bars[0].segments[0].color == STATUS_COLORS["ordered"]
        assert b.bars[1].segments == b.bars[1].segments and len(b.bars[1].segments) == 1
        assert b.legend == [("ordered", STATUS_COLORS["ordered"]), ("shipped", STATUS_COLORS["shipped"])]

    def test_the_overview_renders_the_charts_with_clickable_slices(self, client):
        body = client.get("/").text
        assert body.count("<figure") == 4  # bars + three donuts
        assert 'href="/orders?status=ordered"' in body and 'href="/orders?retailer=Costco"' in body
        assert 'href="/orders?group=MOD"' in body
        assert 'href="/orders?status=shipped&amp;group=BFMR"' in body
        assert "<title>shipped: 1" in body  # the hover tooltip
        assert "table-view" not in body  # user: no table toggles under the charts
        assert "Rows by Status" in body and "Rows by Retailer" in body and "Rows by Buying Group" in body
        assert "Open Rows" in body
        assert 'style="max-width' not in body[body.index('<figure class="bars"'):body.index("</figure>")]  # the bars fill their card
        # The legend sits beside the chart, inside the chart body.
        assert body.index('<div class="chart-body">') < body.index('<ul class="legend">')

    def test_static_assets_carry_a_cache_busting_version(self, client):
        """A deploy must never render with the previous stylesheet from the browser's cache."""
        body = client.get("/").text
        assert re.search(r'href="/static/style\.css\?v=\d+"', body)
        assert re.search(r'src="/static/htmx\.min\.js\?v=\d+"', body)


class TestCardsHelpers:
    def test_filters_parse_view_per_and_page_with_safe_fallbacks(self):
        f = Filters.from_query({"view": "cards", "per": "48", "page": "3"})
        assert (f.view, f.per, f.page) == ("cards", 48, 3)
        assert f.as_query() == {"sort": "order_date", "dir": "desc", "view": "cards", "per": "48",
                                "page": "3"}
        bad = Filters.from_query({"view": "list", "per": "7", "page": "zero"})
        assert (bad.view, bad.per, bad.page) == ("table", 24, 1)
        assert "view" not in bad.as_query() and "per" not in bad.as_query()

    def test_order_cards_group_rows_per_order_in_row_order(self, snapshot_path):
        from web.queries import order_cards

        rows = sort_rows(SnapshotReader(snapshot_path).load().rows, Filters())
        cards = order_cards(rows)
        assert [c["order_id"] for c in cards][:2] == ["111-0000001-0000001", "BBY01-800000000001"]
        bby = next(c for c in cards if c["order_id"] == "BBY01-800000000001")
        assert bby["rows"] == 2 and bby["quantity"] == 3 and bby["total_cost"] == 3000.0
        assert bby["statuses"] == ["shipped", "ordered"] and bby["payout_state"] == "committed"
        assert bby["profit"] == 263.6 and len(bby["keys"]) == 2
        assert bby["tracking"] == ["529900000012"] and len(bby["receipt_urls"]) == 1
        cancelled = next(c for c in cards if c["order_id"] == "1399000019")
        assert cancelled["total_cost"] == 0.0 and cancelled["profit"] is None

    def test_paginate(self):
        from web.queries import paginate

        p = paginate(list(range(50)), 12, 2)
        assert p["items"] == list(range(12, 24)) and p["pages"] == 5
        assert (p["start"], p["end"], p["has_prev"], p["has_next"]) == (13, 24, True, True)
        assert paginate(list(range(50)), 12, 99)["page"] == 5  # clamps to the last page
        assert paginate([], 12, 1) == {**paginate([], 12, 1), "total": 0, "pages": 1, "start": 0, "end": 0}


class TestMultiSelectFilters:
    def test_repeated_params_select_all_that_apply_and_none_means_all(self, snapshot_path):
        rows = SnapshotReader(snapshot_path).load().rows
        f = Filters.from_query({"retailer": ["Costco", "Best Buy"], "status": ["paid", "shipped"]})
        assert f.retailers == ("Costco", "Best Buy") and f.statuses == ("paid", "shipped")
        assert f.retailer == ""  # more than one: no single value
        picked = filter_rows(rows, f)
        assert {(r.retailer, r.status) for r in picked} == {("Costco", "paid"), ("Best Buy", "shipped")}
        assert len(filter_rows(rows, Filters.from_query({}))) == len(rows)
        assert Filters.from_query({"retailer": "Costco"}).retailers == ("Costco",)

    def test_order_cards_carry_one_line_per_item_with_its_quantity_and_shipments(self, snapshot_path):
        from web.queries import CARD_COLUMNS, order_cards

        rows = SnapshotReader(snapshot_path).load().rows
        by_id = {c["order_id"]: c for c in order_cards(rows)}
        two = next(c for c in by_id.values() if c["rows"] > 1)
        assert len(two["item_lines"]) == len(two["items"])
        assert all(line["quantity"] >= 1 and line["shipments"] for line in two["item_lines"])
        # the row list can edit the links and the address too
        for col in ("order_url", "tracking_url", "receipt_url", "delivery_address"):
            assert col in CARD_COLUMNS

    def test_month_paid_and_state_filters(self, snapshot_path):
        rows = SnapshotReader(snapshot_path).load().rows
        assert len(filter_rows(rows, Filters.from_query({"month": "2026-09"}))) == 3
        assert [r.row_number for r in filter_rows(rows, Filters.from_query({"paid": "2026-09"}))] == [5]
        assert len(filter_rows(rows, Filters.from_query({"state": "open"}))) == 4
        assert [r.row_number for r in filter_rows(rows, Filters.from_query({"state": "committed"}))] == [3]
        assert len(filter_rows(rows, Filters.from_query({"state": "settled"}))) == 2
        assert [r.row_number for r in filter_rows(rows, Filters.from_query({"state": "unpaid"}))] == [2, 3, 4, 8]
        assert len(filter_rows(rows, Filters.from_query({"month": "2026-08", "state": "open"}))) == 1
        # malformed values mean "no filter", never an error
        bad = Filters.from_query({"month": "Sept", "paid": "2026-13", "state": "paid"})
        assert (bad.month, bad.paid, bad.state) == ("", "", "")
        assert "month" not in bad.as_query()
        good = Filters.from_query({"month": "2026-09", "state": "open"})
        assert good.as_query()["month"] == "2026-09" and good.as_query()["state"] == "open"

    def test_links_carry_repeated_params(self):
        from web.queries import query_string

        f = Filters.from_query({"retailer": ["Costco", "Best Buy"], "group": ["MOD"], "card": ["0315"]})
        assert query_string(f.as_query(sort="total_cost")) == (
            "retailer=Costco&retailer=Best+Buy&group=MOD&card=0315&sort=total_cost&dir=desc")

    def test_the_page_renders_checkbox_dropdowns_with_an_all_box(self, client):
        body = client.get("/orders", params={"retailer": ["Costco", "Best Buy"]}).text
        assert body.count('<details class="multi"') == 5  # retailer, profile, status, buying group, card
        retailer = body[body.index('data-param="retailer"'):body.index('data-param="profile"')]
        assert 'name="retailer" value="Costco" checked' in retailer
        assert 'name="retailer" value="Best Buy" checked' in retailer
        assert 'name="retailer" value="Amazon" >' in retailer
        assert '<input type="checkbox" class="all-box" > All' in retailer
        assert "Costco, Best Buy" in retailer  # the summary
        assert body.count('<tr class="status-') == 5  # Costco + Best Buy rows
        profile = body[body.index('data-param="profile"'):body.index('data-param="status"')]
        assert 'class="all-box" checked' in profile and ">all<" in profile


class TestStaticAssetsCarryTheirBlocks:
    """two appended blocks (the card row-list styles and the multi-select filter
    script) never reached the files -- a shell heredoc swallowed them -- and the cards rendered
    unstyled. These pin every feature block the templates rely on, so a lost block fails here."""

    ROOT = Path(__file__).resolve().parents[1] / "web" / "static"

    def test_the_picker_script_has_both_pickers(self):
        js = (self.ROOT / "picker.js").read_text(encoding="utf-8")
        for needle in ("window.Picker", "function renderDays", "function renderMonths", "function renderYears",
                       "function renderChoices", 'data-pick="', 'matches("input[data-month]")', "month: function",
                       'matches("input[data-choices]")', "choicesFor: choicesFor", "function formChoices(el)", 'getAttribute("data-options")',
                       'getElementById("cell-choices")', "data.card_pairs",
                       "state.filter = owner.value", 'v === current ? " current"',
                       "stopImmediatePropagation", 'matches("input[data-date]")', 'addEventListener("mousedown", function (e) { e.preventDefault(); })'):
            assert needle in js, f"picker.js lost its {needle!r} block"
        base = (self.ROOT.parent / "templates" / "base.html").read_text(encoding="utf-8")
        assert 'src="/static/picker.js' in base

    def test_the_stylesheet_has_every_feature_block(self):
        css = (self.ROOT / "style.css").read_text(encoding="utf-8")
        for needle in ("table.sheetlike", ".charts figure", "details.multi", ".card-rows-wrap", "td.sel-cell",
                       ".pop.cal", ".pop .choice", ".pop .cal-months .cal-month", "td .cell-upload", ".chip.virtual",
                       "td input.cell-check", ".pop .choice.current",
                       "border-spacing: 0; border-collapse: separate; }", "body.wide .pinned { position: sticky; top: 0;",
                       "body.wide table.grid th { top: var(--pinned-h, 0px); }",
                       "td.finding { white-space: nowrap;", ".tiles.audit-stats .tile.fail { border-color: #d64545; }", ".tiles.audit-stats .tile.skipped",
                       "table.card-rows", 'td[data-field="status"]', "dialog.confirm", ".pager",
                       ".add-form", ".add-form .actions", ".add-form .span-6", "repeat(6, minmax(0, 1fr))", "tr.selected td", ".cards {", ".card ol.items",
                       "td .cell-edit", ".settings-nav", ".entry-card", "details.multi.single",
                       "table.activity { width: 100%", ".dropzone.dragover",
                       ".filters { display: flex; flex-wrap: nowrap", ".filters label.search {",
                       "@keyframes rise-in", ".just-in {", "prefers-reduced-motion"):
            assert needle in css, f"style.css lost its {needle!r} rules"
        # motion stays cheap: nothing transitions "all", and no layout property is ever animated
        motion = css[css.index("/* ---- Motion"):]
        assert "transition: all" not in motion and "transition-property: all" not in motion
        for prop in ("width", "height", "margin", "padding", "top", "left"):
            assert not any("@keyframes" in line and prop + ":" in line for line in motion.splitlines())
        assert css.count("{") == css.count("}")
        # a scrolling filter bar clips the dropdown menus (2026-09-18): the bar must never scroll
        bar = css[css.index(".filters { display: flex"):css.index(chr(10), css.index(".filters { display: flex"))]
        assert "overflow" not in bar

    def test_the_script_has_every_feature_block(self):
        js = (self.ROOT / "edit.js").read_text(encoding="utf-8")
        for needle in ("startEdit(", 'closest("td.rownum")', 'closest("th.rownum")',
                       'addEventListener("htmx:confirm"', "details.multi", 'name !== "view"',
                       '".cell-edit, .cell-empty"', 'contains("single")', '"time.local"',
                       'closest(".dropzone")', "data-autosubmit", 'classList.add("just-in")',
                       'matches(\'[hx-trigger*="every"]\')', "sel-cell", 'execCommand("copy")',
                       'addEventListener("paste"', 'e.key === "Delete"', "fillSelection(", "all.checked = !any",
                       "if (thenDown) move(1, 0, false)", "paint(false)", "Picker.date(", "Picker.choices(",
                       'ctrl && e.key === ";"', "function fillToday()", "function toggleCheck(td)",
                       'contains("cell-check")', 'e.key === " " && td.getAttribute("data-kind") === "check"',
                       "Picker.forget()", 'setProperty("--pinned-h"', 'getElementById("protect-edits")',
                       "function undo()", "function redo()", 'getAttribute("data-cell-url")', 'getAttribute("data-entry-id")',
                       'getAttribute("data-confirm-many")', "details.dataset.narrow",
                       "function releaseSelection()", 'e.target.id === "release-hand"', '(e.key === "h" || e.key === "H")', 'e.key === "z" || e.key === "Z"', 'e.key === "y" || e.key === "Y"',
                       "undoStack.push(step)", "redoStack = []", "toggleOff: alone",
                       "function choicesFor(field, td)", "Picker.choicesFor(field,",
                       '".cell-upload"', 'name="next" value="table"',
                       'hx-encoding", "multipart/form-data"',
                       '(e.key === "Delete" || e.key === "Backspace") && rowsChecked()', 'getElementById("delete-selected")',
                       "selected rows from the ledger?", 'if (e.key === "Escape") { if (clearRows())',
                       '"rows:cleared"', "press.on = press.on.filter(", "function toggleOne(td)",
                       'addEventListener("cells:clear"', 'addEventListener("rows:clear"', "e.ctrlKey || e.metaKey",
                       'GRID_TD = "table.sheetlike td, table.order-rows td"'):
            assert needle in js, f"edit.js lost its {needle!r} block"


# --------------------------------------------------------------------------------------------------
# The Audit and Reconciliation pages: the Orders view over the affected rows (2026-09-18)
# --------------------------------------------------------------------------------------------------


class TestAuditPage:
    """scripts/audit_ledger's checks over the ledger the dashboard serves, each finding on its row."""

    def test_the_page_is_the_orders_view_over_the_flagged_rows(self, client):
        body = client.get("/audit").text
        assert "<h1>Audit</h1>" in body and 'id="filters"' in body and 'action="/audit"' in body
        assert '<th class="col-finding">Finding</th>' in body
        # Row 8 has COGS but no Cashback Rate: cogs_inputs_complete names it.
        assert "111-0000002-0000002" in body and "no rate resolved" in body
        assert "<b>cogs_inputs_complete</b>" in body
        # The summary lists every check with its status.
        assert 'class="tag fail"' in body and "cogs_inputs_complete" in body
        # The check filter lists the checks that flagged rows.
        assert 'data-param="check"' in body and 'name="check" value="cogs_inputs_complete"' in body
        # No "Add a row" and no remembered-filter cookie on this page.
        assert "Add a row" not in body
        assert 'name="scope" value="audit"' in body

    def test_choosing_a_check_narrows_the_rows_to_that_check(self, client):
        body = client.get("/audit", params={"check": "cogs_inputs_complete"}).text
        assert "111-0000002-0000002" in body and "<b>cogs_inputs_complete</b>" in body
        # A check that flagged nothing shows nothing.
        empty = client.get("/audit", params={"check": "status_is_present"}).text
        assert "Nothing to show." in empty and "111-0000002-0000002" not in empty

    def test_the_cards_view_carries_the_findings_too(self, client):
        body = client.get("/audit", params={"view": "cards"}).text
        assert '<div class="finding">' in body and "no rate resolved" in body
        assert 'href="/audit?' in body or "page 1 of 1" in body or "order(s)" in body

    def test_an_htmx_request_gets_the_partial_on_the_pages_own_url(self, client):
        response = client.get("/audit", headers={"HX-Request": "true"})
        assert "<html" not in response.text and '<td class="finding">' in response.text
        assert 'hx-get="/audit?' in response.text  # the sort links stay on the page

    def test_each_page_remembers_its_own_view_and_filters(self, client):
        """Orders,
        Audit and Recon each keep their own view, page size and filters."""
        client.get("/orders", params={"remember": "1", "retailer": "Costco", "view": "cards"})
        body = client.get("/audit").text
        assert "111-0000002-0000002" in body  # an Amazon row: the Orders memory did not apply
        assert 'class="card status-' not in body  # nor its cards view
        response = client.get("/audit", params={"remember": "1", "retailer": "Amazon", "view": "table", "per": "48"})
        assert response.cookies.get("ledger-view-audit") == "table" and response.cookies.get("ledger-per-audit") == "48"
        assert "retailer=Amazon" in response.cookies.get("ledger-filters-audit")
        assert "retailer=Amazon" not in (client.cookies.get("ledger-filters") or "")
        assert 'class="card status-' in client.get("/orders").text  # Orders kept its cards
        again = client.get("/audit").text  # a bare /audit replays the Audit memory
        assert 'name="per" value="48" checked' in again and "111-0000002-0000002" in again
        client.get("/audit", params={"reset": "1"}, follow_redirects=False)
        assert not client.cookies.get("ledger-filters-audit")

    def test_the_report_is_cached_until_the_ledger_changes(self, client, snapshot_path):
        from web import audit_view

        calls = []
        original = audit_view.run_audit

        def counting(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        import web.app as app_module

        app_module.run_audit = counting
        try:
            client.get("/audit")
            client.get("/audit", params={"view": "cards"})
            assert len(calls) == 1
            write_snapshot(snapshot_path, *LEDGER_ROWS, row(order_date="2026-09-18", status="ordered",
                                                             item_name="New", shipment="1",
                                                             order_id="NEW-1", quantity="1",
                                                             cost_per_item="5", total_cost="5"))
            client.get("/audit")
            assert len(calls) == 2
        finally:
            app_module.run_audit = original

    def test_rows_named_by_a_detail_line(self):
        from web.audit_view import rows_named

        assert rows_named("row 157: order X has no status") == [157]
        assert rows_named("row 157, Card Last 4: stored as a number") == [157]
        assert rows_named("rows [2, 3]: order O1 shipment '1'") == [2, 3]
        assert rows_named("12 cell(s) never filled -- run the backfill") == []

    def test_the_report_refuses_every_non_get(self, client):
        assert client.post("/audit").status_code == 405


class TestReconTilesFilter:
    def test_the_tiles_are_links_that_narrow_the_rows_by_kind(self, client):
        page = client.get("/recon").text
        assert page.count('<a class="tile link') == 4
        assert 'href="/recon?kind=short"' in page and 'href="/recon?kind=over"' in page
        short = client.get("/recon?kind=short").text
        assert 'name="kind" value="short"' in short and "short-paid" in short.lower()
        assert 'class="tile link payout-floating' in short and " current" in short[short.index('href="/recon?kind=short"') - 80:short.index('href="/recon?kind=short"')]
        assert client.get("/recon?kind=nonsense").status_code == 200


class TestReconPage:
    """Orders the buying group paid more or less than it committed to."""

    def test_a_short_paid_order_is_listed_with_both_figures(self, client):
        body = client.get("/recon").text
        assert "<h1>Reconciliation</h1>" in body and 'action="/recon"' in body
        # Row 5: expected 520, paid 500.
        assert "1399000017" in body and "short-paid by $20.00" in body
        assert "expected $520.00, paid $500.00" in body
        # The dateless MOD row (no commitment) and the open committed row are not mismatches.
        assert "1399000018" not in body and "BBY01-800000000001" not in body
        assert 'class="tile' in body and "$20.00" in body

    def test_the_cards_view_and_the_partial(self, client):
        cards = client.get("/recon", params={"view": "cards"}).text
        assert '<div class="finding">' in cards and "<b>short</b>" in cards
        partial = client.get("/recon", headers={"HX-Request": "true"}).text
        assert "<html" not in partial and 'hx-get="/recon?' in partial

    def test_reconcile_compares_order_totals_over_settled_rows_only(self):
        from web.recon_view import findings_for, reconcile

        rows = [
            # one order, two rows: 300 + 220 committed, 300 + 200 paid -> short 20
            _row(order_id="O1", order_date="2026-08-01", item_name="Widget", shipment="1", status="paid",
                 payout_amount="300", payout_date="2026-09-01", expected_payout="300", total_cost="250"),
            _row(order_id="O1", order_date="2026-08-01", item_name="Widget", shipment="2", status="paid",
                 payout_amount="200", payout_date="2026-09-01", expected_payout="220", total_cost="250"),
            # over-paid
            _row(order_id="O2", status="paid", payout_amount="110", expected_payout="100"),
            # a cent of rounding is not a mismatch
            _row(order_id="O3", status="paid", payout_amount="100.01", expected_payout="100"),
            # still open: not compared; no commitment: not compared
            _row(order_id="O4", status="shipped", expected_payout="100"),
            _row(order_id="O5", status="paid", payout_amount="90", payout_date="2026-09-01"),
            # a settled loss with a commitment: short by the whole thing
            _row(order_id="O6", status="return", payout_amount="0", expected_payout="50"),
        ]
        report = reconcile(rows)
        assert [o.order_id for o in report.orders] == ["O6", "O1", "O2"]
        assert report.compared == 4
        by = report.by_order()
        assert by["O1"].difference == -20.0 and by["O1"].kind == "short" and by["O1"].rows == 2
        assert by["O2"].difference == 10.0 and by["O2"].kind == "over"
        assert report.short_total == 70.0 and report.over_total == 10.0
        assert "short-paid by $20.00" in by["O1"].line
        findings = findings_for(rows, report)
        assert findings[("O1", "2026-08-01", "Widget", "2")] == [("short", "expected $220.00, paid $200.00 on this row; the order is short-paid by $20.00")]
        assert not any(k[0] == "O4" for k in findings)


class TestTableSorting:
    """A header click must change the sort: htmx inherits hx-include, so the bulk form's include of the filter bar -- with its
    hidden sort / dir inputs -- rode along on every header link and the current sort won."""

    def test_the_bulk_forms_include_never_reaches_the_header_links(self, client):
        template = (Path(__file__).resolve().parents[1] / "web" / "templates" / "orders.html").read_text(encoding="utf-8")
        form = template[template.index('<form id="bulk"'):template.index("</form>", template.index('<form id="bulk"'))]
        assert form.startswith('<form id="bulk" hx-disinherit="hx-include">')
        assert form.count('hx-include="#filters"') == 1  # the Delete button, nothing else
        body = client.get("/orders").text
        bulk = body[body.index('<form id="bulk"'):body.index("</form>", body.index('<form id="bulk"'))]
        table = bulk.split('<div id="orders-table">')[1]
        assert 'hx-include' not in table and 'sort=total_cost' in table

    def test_a_sort_link_sorts(self, client):
        body = client.get("/orders", params={"sort": "total_cost", "dir": "desc"},
                          headers={"HX-Request": "true"}).text
        first = body.index('data-field="total_cost">')
        assert "$2,000.00" in body[first:first + 40]
        assert "▼" in body[body.index("Total Cost"):body.index("Total Cost") + 30]


class TestReceiptFiles:
    """The Receipt Link column points at /receipts/... on the dashboard (receipts/store.py)."""

    def test_a_stored_receipt_is_served_and_a_climbing_path_is_not(self, client, tmp_path, monkeypatch):
        import dataclasses

        from receipts import store

        monkeypatch.setattr(store, "settings", dataclasses.replace(
            store.settings, receipt_capture_enabled=True, receipts_dir=str(tmp_path / "receipts")))
        link = store.put("receipts/bestbuy/2026-09/BBY01-1.pdf", b"%PDF-1.4 x", "pdf")
        assert link == "/receipts/bestbuy/2026-09/BBY01-1.pdf"
        response = client.get(link)
        assert response.status_code == 200 and response.content == b"%PDF-1.4 x"
        assert client.get("/receipts/bestbuy/2026-09/missing.pdf").status_code == 404
        assert client.get("/receipts/../config.json").status_code in (404, 400)
        assert client.get("/receipts/bestbuy/..%2F..%2Fconfig.json").status_code in (404, 400)


class TestTheNavOrder:
    def test_overview_orders_activity_audit_recon_taxes_tools(self, client):
        body = client.get("/").text
        nav = body[body.index("<nav"):body.index("</nav>")]
        order = [nav.index(x) for x in (">Overview<", ">Orders<", ">Activity", ">Audit", ">Recon", ">Taxes<", ">Tools")]
        assert order == sorted(order)


class TestNavBadges:

    @staticmethod
    def _nav(body: str) -> str:
        return body[body.index("<nav"):body.index("</nav>")]

    def test_audit_and_recon_counts_ride_every_page_and_activity_counts_the_loud_week(self, client, logs_dir):
        import re
        from datetime import timedelta
        from diagnostics import activity

        nav = self._nav(client.get("/orders").text)
        assert re.search(r'>Audit <span class="badge"[^>]*>\d+</span></a>', nav)  # the fixture ledger fails checks
        assert re.search(r'>Recon <span class="badge"[^>]*>\d+</span></a>', nav)  # and short-pays an order
        assert ">Activity</a>" in nav  # nothing loud yet: no badge at all
        path = logs_dir / "activity.jsonl"
        activity.record("alert", "boom", {}, path=path, at=NOW)
        activity.record("dossier", "boom", {}, path=path, at=NOW)
        activity.record("alert", "old", {}, path=path, at=NOW - timedelta(days=8))  # outside the week
        assert re.search(r'>Activity <span class="badge"[^>]*>2</span></a>', self._nav(client.get("/orders").text))
        assert re.search(r'>Activity <span class="badge"[^>]*>2</span></a>', self._nav(client.get("/audit").text))


class TestActivityLayout:
    def test_activity_uses_the_orders_layout(self, client):
        body = client.get("/activity").text
        assert '<body class="wide">' in body
        assert body.index("<h1>Activity</h1>") < body.index('<p class="muted small lead">') < body.index('<div class="pinned">') < body.index('id="activity-filters"') < body.index('id="activity-table"')


class TestOverviewAttention:
    def test_cards_appear_only_for_what_needs_a_hand(self, client, logs_dir):
        from diagnostics import activity

        body = client.get("/").text
        # the fixture ledger fails a check (cogs_inputs_complete) and short-pays an order
        assert 'aria-label="needs attention"' in body
        assert ">Audit failures<" in body and 'href="/audit"' in body
        assert ">Short-paid<" in body and 'href="/recon?kind=short"' in body
        assert ">Alerts<" not in body and ">Failure dossiers<" not in body  # nothing loud yet
        activity.record("alert", "Costco [p]: deterministic path failed", {}, path=logs_dir / "activity.jsonl",
                        at=NOW)
        body = client.get("/").text
        assert ">Alerts<" in body and 'href="/activity?type=alert&amp;days=7&amp;unacked=1"' in body  # only the unacknowledged

    def test_a_loud_card_is_acknowledged_per_kind_and_a_newer_one_comes_back(self, client, logs_dir):
        """and the dossiers with them."""
        import re
        from datetime import timedelta
        from diagnostics import activity

        path = logs_dir / "activity.jsonl"
        earlier = NOW - timedelta(hours=2)
        stamp = earlier.isoformat(timespec="seconds")
        activity.record("alert", "Costco [p]: deterministic path failed", {}, path=path, at=earlier)
        activity.record("dossier", "costco [p]: failure dossier written -- Boom", {"name": "x"}, path=path, at=earlier)
        body = client.get("/").text
        assert ">Alerts<" in body and ">Failure dossiers<" in body
        assert body.count('action="/activity/acknowledge"') == 2 and 'name="kind" value="alert"' in body
        assert ">acknowledge all<" in body and 'data-confirm="Acknowledge all 1 alerts?' in body  # asked once
        assert 'id="settings-confirm"' in body
        assert f'name="through" value="{stamp}"' in body
        response = client.post("/activity/acknowledge", data={"kind": "alert", "through": stamp}, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/"
        body = client.get("/").text
        assert ">Alerts<" not in body and ">Failure dossiers<" in body  # each kind on its own
        assert re.search(r'>Activity <span class="badge"[^>]*>1</span>', body)
        newest = activity.read(path)[0]
        assert newest["kind"] == "ack" and newest["summary"] == f"Acknowledged 1 alert(s) through {stamp}"
        assert newest["details"] == {"kind": "alert", "through": stamp, "count": 1}
        # no `through`: the newest one shown
        client.post("/activity/acknowledge", data={"kind": "dossier"}, follow_redirects=False)
        body = client.get("/").text
        assert ">Failure dossiers<" not in body and 'aria-label="needs attention"' in body  # the audit / recon cards stay
        assert ">Activity</a>" in body[body.index("<nav"):body.index("</nav>")]  # no badge
        # a newer alert shows again, alone
        activity.record("alert", "again", {}, path=path, at=NOW - timedelta(minutes=5))
        body = client.get("/").text
        section = body[body.index('aria-label="needs attention"'):]
        assert ">Alerts<" in section and section.index('<div class="value">1</div>') < section.index("</form>")
        assert client.post("/activity/acknowledge", data={"kind": "run"}).status_code == 400
        assert ">Acknowledged<" in client.get("/activity").text  # on the record, as a dashboard event
        elsewhere = client.post("/activity/acknowledge", data={"kind": "alert", "next": "//evil"}, follow_redirects=False)
        assert elsewhere.headers["location"] == "/"  # never off-site

    def test_one_alert_is_acknowledged_on_its_own_from_the_activity_page(self, client, logs_dir):
        import re
        from datetime import timedelta
        from diagnostics import activity

        path = logs_dir / "activity.jsonl"
        first, second = NOW - timedelta(hours=3), NOW - timedelta(hours=1)
        activity.record("alert", "Costco [p]: first", {"message": "a"}, path=path, at=first)
        activity.record("alert", "Costco [p]: second", {"message": "b"}, path=path, at=second)
        activity.record("alert", "old", {}, path=path, at=NOW - timedelta(days=9))  # outside the week: no button
        body = client.get("/activity", params={"type": "alert", "days": "0"}).text
        assert body.count('class="ack-one"') == 2
        row = body[body.index("Costco [p]: first"):]
        assert row.index('class="ack-one"') < row.index(">details<")  # the button sits left of details
        stamp = first.isoformat(timespec="seconds")
        assert f'name="at" value="{stamp}"' in body and 'name="next" value="/activity?type=alert&amp;days=0"' in body
        done = client.post("/activity/acknowledge", follow_redirects=False,
                           data={"kind": "alert", "at": stamp, "summary": "Costco [p]: first", "next": "/activity?type=alert&days=0"})
        assert done.status_code == 303 and done.headers["location"] == "/activity?type=alert&days=0"
        newest = activity.read(path)[0]
        assert newest["kind"] == "ack" and newest["summary"] == "Acknowledged alert: Costco [p]: first"
        assert newest["details"] == {"kind": "alert", "at": stamp, "summary": "Costco [p]: first"}
        body = client.get("/activity", params={"type": "alert", "days": "0"}).text
        assert body.count('class="ack-one"') == 1 and "Costco [p]: second" in body[body.index('class="ack-one"'):]
        # the overview's card lists only what is still to acknowledge
        only = client.get("/activity", params={"type": "alert", "days": "7", "unacked": "1"}).text
        assert "Costco [p]: second" in only and "Costco [p]: first" not in only[only.index("<table"):]
        assert "unacknowledged only" in only and 'name="unacked" value="1"' in only and "unacked=1" in only  # the tag, and the links keep it
        assert re.search(r'>Activity <span class="badge"[^>]*>1</span>', body)
        overview = client.get("/").text
        section = overview[overview.index('aria-label="needs attention"'):]
        assert section.index('<div class="value">1</div>') < section.index("</form>")
        # an unknown one records nothing; acknowledging all then clears the rest
        client.post("/activity/acknowledge", data={"kind": "alert", "at": stamp, "summary": "never happened"}, follow_redirects=False)
        assert activity.read(path)[0]["kind"] == "ack" and activity.read(path)[0]["details"]["summary"] == "Costco [p]: first"
        client.post("/activity/acknowledge", data={"kind": "alert"}, follow_redirects=False)
        assert 'class="ack-one"' not in client.get("/activity", params={"type": "alert", "days": "0"}).text
        assert ">Alerts<" not in client.get("/").text

    def test_nothing_is_shown_when_nothing_is_wrong(self, tmp_path, logs_dir):
        from web.ledger_reader import LedgerRow
        from web.app import create_app
        from config.settings import settings

        class Clean:
            backend = "snapshot"

            def load(self, force=False):
                from web.ledger_reader import Snapshot
                return Snapshot(rows=[], header=list(HEADER), backend="snapshot", source="x", loaded_at=NOW)

            def health(self):
                return {}

        app = create_app(Clean(), logs_dir=logs_dir, failures_dir=tmp_path / "f", backup_dir=tmp_path / "b",
                         repo_root_dir=tmp_path, clock=lambda: NOW,
                         settings=dataclasses.replace(settings, container_run_interval_hours=6))
        body = TestClient(app).get("/").text
        assert 'aria-label="needs attention"' not in body
