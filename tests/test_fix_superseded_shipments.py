"""The repair for rows left by a SUPERSEDED tracking number: mark (default), delete, restore.

Only the pure planners are exercised: they decide what gets MARKED, DELETED or APPENDED on the live
sheet, so they are the half that must be right. The fetch half is a thin CDP loop over production
parsers, and the apply half is single-cell writes over gspread.
"""

from models.order import FIELDNAMES
from scripts.fix_superseded_shipments import plan_restore, plan_supersede_fix
from ledger.sync import HEADER, _SUPERSEDED_BLANK_FIELDS

OID = "111-9990021-9990021"
DEAD, LIVE = "TBA999000000007", "TBA999000000009"


def sheet_row(**values):
    return [values.get(name, "") for name in HEADER]


def row(tracking, shipment, item="iPad Pro", order_id=OID, retailer="Amazon Business",
        status="shipped", quantity="3", total_cost="2847", payout="", insurance="", **extra):
    values = {"Order ID": order_id, "Retailer": retailer, "Item Name": item,
              "Shipment": shipment, "Tracking Number": tracking, "Order Date": "2026-08-12",
              "Status": status, "Quantity": quantity, "Cost Per Item": "949", "Total Cost": total_cost,
              "Actual Payout": payout, "Insurance": insurance, "Cashback Rate": "0.05",
              "Tracking Submitted": "True"}
    values.update(extra)
    return sheet_row(**values)


def plan(rows, live, mode="mark"):
    return plan_supersede_fix(list(HEADER), rows, live, mode=mode)


class TestMarkMode:
    """The default since 2026-09-09: the dead row stays, retired and money-free."""

    def test_the_real_case_marks_the_dead_label_and_renumbers_the_survivor(self):
        """Rows 17/18 live on 2026-08-22: Shipment 1 held a dead label, Shipment 2 the live one."""
        p = plan([row(DEAD, 1), row(LIVE, 2)], {OID: [LIVE]})

        assert p["marks"] == [(2, OID, "1", DEAD, "iPad Pro", "2")]
        assert [(r[0], r[2], r[3]) for r in p["renumbers"]] == [(3, "2", "1")]
        assert p["deletions"] == [] and p["refused"] == []
        assert p["orders"] == [OID]

    def test_a_fully_matching_order_plans_nothing(self):
        p = plan([row("T1", 1), row("T2", 2)], {OID: ["T1", "T2"]})

        assert p["marks"] == [] and p["renumbers"] == [] and p["refused"] == []

    def test_two_dead_rows_sharing_one_carton_get_one_new_number(self):
        p = plan([row(DEAD, 1, item="A"), row(DEAD, 1, item="B"), row(LIVE, 2)], {OID: [LIVE]})

        assert [(m[0], m[5]) for m in p["marks"]] == [(2, "2"), (3, "2")]

    def test_two_distinct_dead_numbers_are_numbered_past_the_live_boxes_in_row_order(self):
        p = plan([row("DEAD-A", 1), row("DEAD-B", 2), row(LIVE, 3)], {OID: [LIVE]})

        assert [(m[3], m[5]) for m in p["marks"]] == [("DEAD-A", "2"), ("DEAD-B", "3")]

    def test_an_already_superseded_row_is_left_alone_and_a_second_re_label_numbers_past_it(self):
        old = row("DEAD-OLD", 2, status="superseded", quantity="", total_cost="")
        p = plan([row(LIVE, 1), old, row("DEAD-NEW", 3)], {OID: [LIVE]})

        assert [(a[0], a[3]) for a in p["already_superseded"]] == [(3, "DEAD-OLD")]
        assert p["marks"] == [(4, OID, "3", "DEAD-NEW", "iPad Pro", "3")]
        assert p["renumbers"] == []

    def test_a_dead_row_carrying_group_money_is_refused(self):
        for overrides in ({"payout": "2850"}, {"insurance": "7.40"}, {"status": "paid"},
                          {"status": "return"}, {"status": "cancelled"}):
            p = plan([row(DEAD, 1, **overrides), row(LIVE, 2)], {OID: [LIVE]})

            assert p["marks"] == [], overrides
            assert [(r[0], r[2]) for r in p["refused"]] == [(2, DEAD)], overrides

    def test_a_blank_tracking_row_is_never_touched(self):
        p = plan([row("", 2)], {OID: ["T1"]})

        assert p["marks"] == [] and [b[0] for b in p["blank_tracking"]] == [2]

    def test_orders_that_were_not_read_are_untouched(self):
        """A failed page load must never look like 'every row is superseded'."""
        p = plan([row("T1", 1), row("T2", 2, order_id="OTHER")], {})

        assert p["marks"] == [] and p["renumbers"] == []

    def test_other_orders_on_the_sheet_are_ignored(self):
        p = plan([row("ZZZ", 1, order_id="OTHER"), row("TBA-LIVE", 2)], {OID: ["TBA-LIVE"]})

        assert p["marks"] == []
        assert [(r[0], r[3]) for r in p["renumbers"]] == [(3, "1")]

    def test_the_planner_is_retailer_agnostic_and_best_buy_is_readable(self):
        """Best Buy rows plan exactly like Amazon's; the script reads them via the ss-api client."""
        from scripts.fix_superseded_shipments import RETAILERS

        assert "Best Buy" in RETAILERS
        bby = "BBY01-809900000010"
        p = plan([row("529900000011", 1, order_id=bby, retailer="Best Buy", quantity="1"),
                  row("529900000012", 2, order_id=bby, retailer="Best Buy", quantity="1")],
                 {bby: ["529900000012"]})
        assert [(m[3], m[5]) for m in p["marks"]] == [("529900000011", "2")]
        assert [(r[0], r[3]) for r in p["renumbers"]] == [(3, "1")]


class TestDeleteMode:
    """`--delete`: the pre-2026-09-09 repair, unchanged."""

    def test_the_real_case_deletes_the_dead_label_and_renumbers_the_survivor(self):
        p = plan([row(DEAD, 1), row(LIVE, 2)], {OID: [LIVE]}, mode="delete")

        assert [(d[0], d[3]) for d in p["deletions"]] == [(2, DEAD)]
        assert [(r[0], r[2], r[3]) for r in p["renumbers"]] == [(3, "2", "1")]
        assert p["marks"] == []

    def test_rows_sharing_one_tracking_number_keep_one_shipment_number(self):
        p = plan([row("T1", 2, item="A"), row("T1", 2, item="B")], {OID: ["T1"]}, mode="delete")

        assert p["deletions"] == []
        assert [(r[0], r[3]) for r in p["renumbers"]] == [(2, "1"), (3, "1")]

    def test_a_blank_tracking_row_is_never_deleted(self):
        p = plan([row("", 2)], {OID: ["T1"]}, mode="delete")

        assert p["deletions"] == [] and [b[0] for b in p["blank_tracking"]] == [2]


class TestRestore:
    """`--restore-from`: a row an earlier --delete removed comes back as a superseded row."""

    OLD_HEADER = [h for h in HEADER if h not in ("Rewards Used", "Gift Card", "Sales Tax")]  # 08-22 shape

    def _backup_row(self, tracking, shipment="1", **extra):
        full = row(tracking, shipment, **extra)
        return [full[HEADER.index(h)] for h in self.OLD_HEADER]

    def test_the_deleted_row_comes_back_retired_money_free_and_numbered_after_the_live_box(self):
        current = [row(LIVE, 1, status="paid", payout="2850")]
        backup = [self._backup_row(DEAD, "1"), self._backup_row(LIVE, "2")]

        p = plan_restore(list(HEADER), current, self.OLD_HEADER, backup, OID)

        assert [t for t, _ in p["appends"]] == [DEAD]
        assert p["skipped"] == [(LIVE, "already on the sheet")]
        new = p["appends"][0][1]
        assert new[HEADER.index("Status")] == "superseded"
        assert new[HEADER.index("Shipment")] == 2
        assert new[HEADER.index("Tracking Number")] == DEAD
        assert new[HEADER.index("Tracking Submitted")] is True
        assert new[HEADER.index("Cashback Rate")] == 0.05
        for field in _SUPERSEDED_BLANK_FIELDS:
            assert new[FIELDNAMES.index(field)] == "", field
        assert new[HEADER.index("Rewards Used")] == ""  # a column the old backup never had

    def test_other_orders_in_the_backup_are_ignored(self):
        backup = [self._backup_row("X", "1", order_id="OTHER")]
        p = plan_restore(list(HEADER), [], self.OLD_HEADER, backup, OID)
        assert p["appends"] == [] and p["skipped"] == []


class TestPackageIdMatching:
    """Column 34 (2026-09-09): a row is live by tracking number OR by package id. Live only by id =
    a re-labelled package (same id, new number) -- reported, renumbered, never marked or deleted."""

    def test_a_dead_number_whose_package_id_is_still_on_the_page_is_relabelled_not_marked(self):
        p = plan([row(DEAD, 1, **{"Package ID": "NxWmqLBj2"})],
                 {OID: [{"tracking": LIVE, "package_id": "NxWmqLBj2"}]})

        assert p["marks"] == [] and p["deletions"] == [] and p["refused"] == []
        assert p["relabelled"] == [(2, OID, "1", DEAD, LIVE)]
        assert p["orders"] == []

    def test_relabelled_is_never_deleted_either(self):
        p = plan([row(DEAD, 1, **{"Package ID": "P1"})],
                 {OID: [{"tracking": LIVE, "package_id": "P1"}]}, mode="delete")
        assert p["deletions"] == [] and p["relabelled"] == [(2, OID, "1", DEAD, LIVE)]

    def test_dead_by_number_and_by_id_is_marked(self):
        p = plan([row(DEAD, 1, **{"Package ID": "NWfgPHR2F"}), row(LIVE, 2, **{"Package ID": "NxWmqLBj2"})],
                 {OID: [{"tracking": LIVE, "package_id": "NxWmqLBj2"}]})

        assert p["marks"] == [(2, OID, "1", DEAD, "iPad Pro", "2")]
        assert p["relabelled"] == []
        assert [(r[0], r[2], r[3]) for r in p["renumbers"]] == [(3, "2", "1")]

    def test_a_row_without_a_package_id_falls_back_to_the_tracking_number(self):
        p = plan([row(DEAD, 1), row(LIVE, 2)], {OID: [{"tracking": LIVE, "package_id": "P9"}]})
        assert p["marks"] == [(2, OID, "1", DEAD, "iPad Pro", "2")]

    def test_the_old_list_of_strings_live_shape_still_works_with_the_column_present(self):
        p = plan([row(DEAD, 1, **{"Package ID": "P1"}), row(LIVE, 2, **{"Package ID": "P2"})], {OID: [LIVE]})
        assert p["marks"] == [(2, OID, "1", DEAD, "iPad Pro", "2")]
        assert [(r[0], r[2], r[3]) for r in p["renumbers"]] == [(3, "2", "1")]

    def test_a_relabelled_row_is_renumbered_to_its_page_position(self):
        p = plan([row(DEAD, 2, **{"Package ID": "P1"}), row("T2", 1, **{"Package ID": "P2"})],
                 {OID: [{"tracking": "T1-NEW", "package_id": "P1"}, {"tracking": "T2", "package_id": "P2"}]})

        assert p["relabelled"] == [(2, OID, "2", DEAD, "T1-NEW")]
        assert [(r[0], r[2], r[3]) for r in p["renumbers"]] == [(2, "2", "1"), (3, "1", "2")]

    def test_a_live_entry_with_an_unread_number_still_counts_by_id(self):
        """The pt page could not be read, but the card and its shipmentId are on the page."""
        p = plan([row(DEAD, 1, **{"Package ID": "P1"})], {OID: [{"tracking": "", "package_id": "P1"}]})
        assert p["marks"] == [] and p["relabelled"] == [(2, OID, "1", DEAD, "")]
