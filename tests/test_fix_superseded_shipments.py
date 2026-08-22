"""The one-off repair that removes rows left by a SUPERSEDED tracking number.

Only the pure planner is exercised: it decides what gets DELETED from the live sheet, so it is the half
that must be right. The fetch half is a thin CDP loop over production parsers.
"""

from scripts.fix_superseded_shipments import plan_supersede_fix
from sheets.ledger_sync import HEADER

OID = "111-9990021-9990021"


def sheet_row(**values):
    return [values.get(name, "") for name in HEADER]


def row(tracking, shipment, item="iPad Pro", order_id=OID, retailer="Amazon Business"):
    return sheet_row(**{"Order ID": order_id, "Retailer": retailer, "Item Name": item,
                        "Shipment": shipment, "Tracking Number": tracking,
                        "Order Date": "2026-08-12"})


def plan(rows, live):
    return plan_supersede_fix(list(HEADER), rows, live)


class TestSupersededRows:
    def test_the_real_case_deletes_the_dead_label_and_renumbers_the_survivor(self):
        """Rows 17/18 live: Shipment 1 held a dead label, Shipment 2 held the live one."""
        p = plan([row("TBA999000000007", 1), row("TBA999000000009", 2)],
                 {OID: ["TBA999000000009"]})

        assert [(d[0], d[3]) for d in p["deletions"]] == [(2, "TBA999000000007")]
        assert [(r[0], r[2], r[3]) for r in p["renumbers"]] == [(3, "2", "1")]

    def test_a_fully_matching_order_plans_nothing(self):
        p = plan([row("T1", 1), row("T2", 2)], {OID: ["T1", "T2"]})

        assert p["deletions"] == [] and p["renumbers"] == []

    def test_rows_sharing_one_tracking_number_keep_one_shipment_number(self):
        # A multi-SKU carton is legal: several rows, one package, one shipment number.
        p = plan([row("T1", 2, item="A"), row("T1", 2, item="B")], {OID: ["T1"]})

        assert p["deletions"] == []
        assert [(r[0], r[3]) for r in p["renumbers"]] == [(2, "1"), (3, "1")]

    def test_a_blank_tracking_row_is_never_deleted(self):
        # It cannot be matched — it may simply be a box that hasn't shipped yet.
        p = plan([row("", 2)], {OID: ["T1"]})

        assert p["deletions"] == []
        assert [b[0] for b in p["blank_tracking"]] == [2]

    def test_orders_that_were_not_read_are_untouched(self):
        """A failed page load must never look like 'every row is superseded'."""
        p = plan([row("T1", 1), row("T2", 2, order_id="OTHER")], {})

        assert p["deletions"] == [] and p["renumbers"] == []

    def test_other_orders_on_the_sheet_are_ignored(self):
        p = plan([row("ZZZ", 1, order_id="OTHER"), row("TBA-LIVE", 2)],
                 {OID: ["TBA-LIVE"]})

        assert p["deletions"] == []
        assert [(r[0], r[3]) for r in p["renumbers"]] == [(3, "1")]
