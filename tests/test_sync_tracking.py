"""Offline tests for the eligibility policy and the payout allocation. No ledger, no network.

`plan_tracking_submissions` is a pure f(header, rows), so the whole "what gets posted where, and
what deliberately doesn't" policy is testable without touching the ledger — the same shape as
scripts/backfill_profit_columns.py:plan_profit_backfill.
"""

import pytest

import sync_tracking

from buying_groups.base import PayoutRecord, SubmissionResult
from models.order import STATUSES
from ledger.sync import HEADER
from sync_tracking import (
    EXPECTED_PAYOUT_COL,
    INSURANCE_COL,
    PAYOUT_AMOUNT_COL,
    PAYOUT_DATE_COL,
    STATUS_COL,
    SUBMITTED_COL,
    _expected_payment_mismatches,
    _first_n_packages,
    _tick_submitted,
    _status_rank,
    allocate_expected_payouts,
    allocate_payouts,
    plan_tracking_submissions,
)

HEADER_LIST = list(HEADER)


def row(**values) -> list:
    """Build a full-width row, positionally, from column names."""
    cells = [""] * len(HEADER_LIST)
    for name, value in values.items():
        cells[HEADER_LIST.index(name)] = value
    return cells


def shipped(order_id, tracking, group="BFMR", **overrides):
    base = dict(
        **{"Order ID": order_id, "Order Date": "2026-08-01", "Item Name": "Widget",
           "Quantity": 1, "Tracking Number": tracking, "Shipment": 1,
           "Status": "shipped", "Total Cost": 100, "Buying Group": group}
    )
    base.update(overrides)
    return row(**base)


class TestAGroupSwitchedOff:
    """each buying group has its own switch under the sync's."""

    def test_a_group_switched_off_is_skipped_unless_named(self, monkeypatch):
        import dataclasses
        import sys

        cs = sys.modules["config.settings"]  # the module is_enabled imports from, whatever another test swapped
        monkeypatch.setattr(cs, "settings", dataclasses.replace(cs.settings, bfmr_enabled=True, mod_enabled=False))
        by_group = {"MOD": ["m"], "BFMR": ["b"], "Other": ["o"]}
        assert sync_tracking._groups_to_run(by_group, None) == [("BFMR", ["b"]), ("Other", ["o"])]
        assert sync_tracking._groups_to_run(by_group, "MOD") == [("MOD", ["m"])]  # an explicit --group overrides


class TestEligibility:
    def test_a_shipped_row_with_a_tracking_number_is_submitted(self):
        plan = plan_tracking_submissions(HEADER_LIST, [shipped("O1", "T1")])
        assert [s.order_id for s in plan["by_group"]["BFMR"]] == ["O1"]

    def test_a_delivered_row_is_still_submitted(self):
        """A row that shipped AND delivered between two runs was never submitted. Filtering to
        `shipped` only would drop it permanently, and with it the reimbursement."""
        plan = plan_tracking_submissions(
            HEADER_LIST, [shipped("O1", "T1", **{"Status": "delivered"})]
        )
        assert plan["by_group"]["BFMR"]

    def test_a_cancelled_row_is_never_submitted(self):
        plan = plan_tracking_submissions(
            HEADER_LIST, [shipped("O1", "T1", **{"Status": "cancelled"})]
        )
        assert not plan["by_group"] and plan["skipped_cancelled"] == 1

    def test_a_row_without_a_tracking_number_waits(self):
        plan = plan_tracking_submissions(
            HEADER_LIST, [shipped("O1", "", **{"Status": "ordered"})]
        )
        assert not plan["by_group"] and plan["skipped_no_tracking"] == 1

    def test_a_blank_order_id_row_is_ignored_entirely(self):
        """Same rule sync_csv_to_ledger applies: a row with no Order ID is not a real order."""
        plan = plan_tracking_submissions(HEADER_LIST, [shipped("", "T1")])
        assert not plan["by_group"]
        assert plan["skipped_no_tracking"] == 0 and plan["skipped_cancelled"] == 0

    @pytest.mark.parametrize("group", ["Unclassified", "", "SomeGroupWeDontSupport"])
    def test_an_unroutable_group_is_counted_not_guessed_at(self, group):
        """An Unclassified row is a real warehouse someone forgot to configure. Making it visible is
        the whole point — silently picking a provider could ship someone else's package's payout."""
        plan = plan_tracking_submissions(HEADER_LIST, [shipped("O1", "T1", group=group)])
        assert not plan["by_group"]
        assert sum(plan["skipped_unroutable"].values()) == 1

    def test_the_example_configs_spelling_of_maxoutdeals_still_routes(self):
        plan = plan_tracking_submissions(
            HEADER_LIST, [shipped("O1", "T1", group="MaxOutDeals")]
        )
        assert list(plan["by_group"]) == ["MOD"]

    def test_row_numbers_account_for_the_header_row(self):
        plan = plan_tracking_submissions(HEADER_LIST, [shipped("O1", "T1"), shipped("O2", "T2")])
        assert [s.row_number for s in plan["by_group"]["BFMR"]] == [2, 3]


class TestRetailerReachesTheClient:
    """The planner must carry Retailer through, and this seam is where it broke.

    `_is_bestbuy` gates BFMR's duplicate-carton suffix retry, so a blank retailer switches the whole
    feature off silently. It WAS blank for every row: `optional_cell` resolves a column through
    `idx`, and "Retailer" had never been added to that map, so it read "" on every ledger rather than
    only on a ledger that lacks the column.

    Nothing caught it because the client-side tests build a TrackingSubmission directly with
    retailer="Best Buy" — they prove the retry works, given a retailer, and never ask whether one
    arrives. These tests cross that seam.
    """

    @staticmethod
    def _bestbuy_row():
        return shipped("BBY01-809900000006", "529900000009", **{"Retailer": "Best Buy"})

    def test_the_retailer_column_reaches_the_submission(self):
        plan = plan_tracking_submissions(HEADER_LIST, [self._bestbuy_row()])
        assert plan["by_group"]["BFMR"][0].retailer == "Best Buy"

    def test_a_best_buy_row_actually_satisfies_the_gate_that_uses_it(self):
        """Asserting the value alone would still pass if the gate expected another spelling, so
        assert against the real predicate rather than a string."""
        from buying_groups.bfmr import _is_bestbuy

        plan = plan_tracking_submissions(HEADER_LIST, [self._bestbuy_row()])
        assert _is_bestbuy(plan["by_group"]["BFMR"][0].retailer)

    def test_a_non_best_buy_row_does_not_satisfy_it(self):
        from buying_groups.bfmr import _is_bestbuy

        plan = plan_tracking_submissions(
            HEADER_LIST, [shipped("111-2", "TBA1", **{"Retailer": "Amazon"})])
        assert not _is_bestbuy(plan["by_group"]["BFMR"][0].retailer)

    def test_a_ledger_predating_the_column_still_syncs_the_row(self):
        """The point of `optional_cell`: an older ledger loses the suffix retry, not the whole row."""
        header = [c for c in HEADER_LIST if c != "Retailer"]
        row_cells = self._bestbuy_row()
        del row_cells[HEADER_LIST.index("Retailer")]

        plan = plan_tracking_submissions(header, [row_cells])
        submission = plan["by_group"]["BFMR"][0]
        assert submission.retailer == ""
        assert submission.order_id == "BBY01-809900000006", "the rest of the row survives"


class TestUnresolvedSplitQuantity:
    def test_a_star_quantity_row_is_flagged_rather_than_submitted(self):
        """The undisclosed-split safety net writes Quantity '*' when a retailer rotates a tracking
        number on a same-SKU multi-box line. No API accepts '*' — but skipping it SILENTLY means
        that box is never submitted and never paid, so the caller alerts on this list."""
        plan = plan_tracking_submissions(
            HEADER_LIST, [shipped("O1", "T1", **{"Quantity": "*", "Total Cost": ""})]
        )
        assert not plan["by_group"]
        assert plan["unresolved_split"] == [(2, "O1", "T1")]

    def test_a_normal_row_never_lands_in_the_unresolved_list(self):
        plan = plan_tracking_submissions(HEADER_LIST, [shipped("O1", "T1")])
        assert plan["unresolved_split"] == []

    def test_a_float_corrupted_tracking_number_is_withheld_and_reported(self):
        """a 22-digit tracking number typed without a leading apostrophe was
        stored as a double and read back as '9.339589752066617e+21' — its trailing digits
        already gone. Submitting that posts garbage to a group (not undoable at MOD), so the row is
        withheld and surfaced for a human to re-type the number as text."""
        plan = plan_tracking_submissions(
            HEADER_LIST, [shipped("O1", "9.339589752066617e+21")]
        )
        assert not plan["by_group"], "the mangled number must never reach a submission"
        assert plan["corrupted_tracking"] == [(2, "O1", "9.339589752066617e+21")]

    def test_real_tracking_numbers_are_not_mistaken_for_corruption(self):
        for t in ("TBA999000000004", "1Z999TST0000000004", "9399990000000000000001"):
            plan = plan_tracking_submissions(HEADER_LIST, [shipped("O1", t)])
            assert plan["corrupted_tracking"] == [], t
            assert plan["by_group"], t


class TestPackageGrouping:
    def test_rows_sharing_a_tracking_number_are_recorded_as_one_package(self):
        plan = plan_tracking_submissions(HEADER_LIST, [
            shipped("O1", "T1", **{"Item Name": "A", "Total Cost": 800}),
            shipped("O1", "T1", **{"Item Name": "B", "Total Cost": 200}),
        ])
        assert plan["rows_by_tracking"] == {"T1": [2, 3]}

    def test_limit_takes_whole_packages_never_half_a_box(self):
        """Submitting half a box would under-report its value to MOD (which sums per tracking
        number) and strand the rest — the number would then read as already known."""
        plan = plan_tracking_submissions(HEADER_LIST, [
            shipped("O1", "T1", **{"Item Name": "A"}),
            shipped("O1", "T1", **{"Item Name": "B"}),
            shipped("O2", "T2"),
        ])
        kept = _first_n_packages(plan["by_group"]["BFMR"], 1)
        assert [r.tracking_number for r in kept] == ["T1", "T1"]


class TestPayoutAllocation:
    def _plan(self):
        return plan_tracking_submissions(HEADER_LIST, [
            shipped("O1", "T1", **{"Item Name": "A", "Total Cost": 800}),
            shipped("O1", "T1", **{"Item Name": "B", "Total Cost": 200}),
        ])

    def test_a_package_payout_is_split_pro_rata_and_sums_to_the_original(self):
        """A payout arrives per PACKAGE but the ledger is per (shipment x item). Writing the full
        amount onto both rows would book it twice in every column sum — the same trap order-level
        shipping already avoids in ledger_sync._profit_formula."""
        plan = self._plan()
        writes = allocate_payouts(
            [PayoutRecord("T1", payout_amount=1100.0, insurance=5.0, payout_date="2026-08-10")],
            plan["rows_by_tracking"], plan["costs_by_row"],
        )
        assert writes[2][PAYOUT_AMOUNT_COL] == 880.0   # 800/1000 of 1100
        assert writes[3][PAYOUT_AMOUNT_COL] == 220.0   # 200/1000 of 1100
        assert sum(w[PAYOUT_AMOUNT_COL] for w in writes.values()) == 1100.0
        assert sum(w[INSURANCE_COL] for w in writes.values()) == 5.0

    def test_the_payout_date_is_repeated_on_every_row_of_the_package(self):
        plan = self._plan()
        writes = allocate_payouts(
            [PayoutRecord("T1", payout_amount=10.0, payout_date="2026-08-10")],
            plan["rows_by_tracking"], plan["costs_by_row"],
        )
        assert {w[PAYOUT_DATE_COL] for w in writes.values()} == {"2026-08-10"}

    def test_several_records_for_one_package_are_summed_before_splitting(self):
        """BFMR reports per deal line, so a box holding two deals returns two records for one
        tracking number."""
        plan = self._plan()
        writes = allocate_payouts(
            [PayoutRecord("T1", payout_amount=600.0), PayoutRecord("T1", payout_amount=400.0)],
            plan["rows_by_tracking"], plan["costs_by_row"],
        )
        assert sum(w[PAYOUT_AMOUNT_COL] for w in writes.values()) == 1000.0

    def test_unpriced_rows_split_evenly_instead_of_dividing_by_zero(self):
        plan = plan_tracking_submissions(HEADER_LIST, [
            shipped("O1", "T1", **{"Item Name": "A", "Total Cost": ""}),
            shipped("O1", "T1", **{"Item Name": "B", "Total Cost": ""}),
        ])
        writes = allocate_payouts(
            [PayoutRecord("T1", payout_amount=100.0)],
            plan["rows_by_tracking"], plan["costs_by_row"],
        )
        assert [w[PAYOUT_AMOUNT_COL] for w in writes.values()] == [50.0, 50.0]

    def test_an_unpaid_package_gets_no_payout_cell_at_all(self):
        """Not a zero. `_profit_formula` reads a blank Actual Payout as "not paid out yet" and
        renders blank; a literal 0 makes it compute `0 - Total Cost - ...`, i.e. a large fictitious
        LOSS on a perfectly healthy order."""
        plan = self._plan()
        writes = allocate_payouts(
            [PayoutRecord("T1", payout_amount=None, status="")],
            plan["rows_by_tracking"], plan["costs_by_row"], plan["status_by_row"],
        )
        assert writes == {}

    def test_a_settled_package_with_no_premium_records_a_real_zero(self):
        """Once the group has PAID and still reports no premium line, there was never a charge —
        0 is a fact, and a uniformly-filled column beats a sparse one."""
        plan = self._plan()
        writes = allocate_payouts(
            [PayoutRecord("T1", payout_amount=100.0, insurance=None)],
            plan["rows_by_tracking"], plan["costs_by_row"], plan["status_by_row"],
            plan["insurance_by_row"],
        )
        assert writes[2][INSURANCE_COL] == 0.0

    def test_an_open_package_with_no_premium_is_left_untouched(self):
        """BFMR posts the premium line BEFORE it pays, so a missing one on an unpaid package may
        simply not have been posted yet. Writing 0 there would assert something not yet known."""
        plan = self._plan()
        writes = allocate_payouts(
            [PayoutRecord("T1", payout_amount=None, insurance=None, status="")],
            plan["rows_by_tracking"], plan["costs_by_row"], plan["status_by_row"],
            plan["insurance_by_row"],
        )
        assert writes == {}

    def test_an_inferred_zero_never_overwrites_a_hand_typed_premium(self):
        """Insurance was hand-entered for months. An inferred 0 written over a real figure would
        erase a cost and overstate that row's profit by exactly what was paid to insure it."""
        plan = plan_tracking_submissions(HEADER_LIST, [
            shipped("O1", "T1", **{"Item Name": "A", "Total Cost": 800, "Insurance": 4.5}),
            shipped("O1", "T1", **{"Item Name": "B", "Total Cost": 200}),
        ])
        writes = allocate_payouts(
            [PayoutRecord("T1", payout_amount=100.0, insurance=None)],
            plan["rows_by_tracking"], plan["costs_by_row"], plan["status_by_row"],
            plan["insurance_by_row"],
        )
        assert INSURANCE_COL not in writes[2], "the typed 4.5 survives"
        assert writes[3][INSURANCE_COL] == 0.0, "the blank row still gets its zero"

    def test_a_reported_premium_still_wins_over_a_typed_value(self):
        """The group's own figure is authoritative; only the INFERRED zero defers to a human."""
        plan = plan_tracking_submissions(
            HEADER_LIST, [shipped("O1", "T1", **{"Total Cost": 100, "Insurance": 4.5})]
        )
        writes = allocate_payouts(
            [PayoutRecord("T1", payout_amount=100.0, insurance=7.4)],
            plan["rows_by_tracking"], plan["costs_by_row"], plan["status_by_row"],
            plan["insurance_by_row"],
        )
        assert writes[2][INSURANCE_COL] == 7.4

    def test_a_reported_zero_insurance_is_written(self):
        """MOD genuinely never charges insurance, so 0.0 is a fact worth recording — and it has to
        be distinguishable from BFMR's "no idea"."""
        plan = self._plan()
        writes = allocate_payouts(
            [PayoutRecord("T1", payout_amount=100.0, insurance=0.0)],
            plan["rows_by_tracking"], plan["costs_by_row"],
        )
        assert writes[2][INSURANCE_COL] == 0.0

    def test_a_payout_for_a_tracking_number_not_on_the_ledger_is_dropped(self):
        """The group may hold packages we never recorded (bought outside this ledger). Writing them
        somewhere would be worse than ignoring them."""
        plan = self._plan()
        writes = allocate_payouts(
            [PayoutRecord("UNKNOWN", payout_amount=99.0)],
            plan["rows_by_tracking"], plan["costs_by_row"],
        )
        assert writes == {}


class TestStatusOnlyMovesForward:
    """The groups own `paid` / `return`, but their reports are snapshots, so a write must never walk
    a row backwards."""

    def _plan(self, current_status="delivered"):
        return plan_tracking_submissions(
            HEADER_LIST,
            [shipped("O1", "T1", group="MOD", **{"Status": current_status})],
        )

    def test_a_payout_advances_a_delivered_row_to_paid(self):
        plan = self._plan("delivered")
        writes = allocate_payouts(
            [PayoutRecord("T1", payout_amount=100.0, status="paid")],
            plan["rows_by_tracking"], plan["costs_by_row"], plan["status_by_row"],
        )
        assert writes[2][STATUS_COL] == "paid"

    def test_a_hand_typed_mod_return_survives_every_later_run(self):
        """MOD publishes no return signal, so a return is typed on the ledger by hand — while MOD
        goes on reporting that package as received (= paid) forever. Without this guard every run
        would silently undo the correction."""
        plan = self._plan("return")
        writes = allocate_payouts(
            [PayoutRecord("T1", payout_amount=100.0, status="paid")],
            plan["rows_by_tracking"], plan["costs_by_row"], plan["status_by_row"],
        )
        assert STATUS_COL not in writes[2]
        assert writes[2][PAYOUT_AMOUNT_COL] == 100.0  # the money still updates

    def test_a_group_with_no_opinion_never_touches_the_status(self):
        plan = self._plan("shipped")
        writes = allocate_payouts(
            [PayoutRecord("T1", payout_amount=100.0, status="")],
            plan["rows_by_tracking"], plan["costs_by_row"], plan["status_by_row"],
        )
        assert STATUS_COL not in writes[2]

    def test_a_return_still_overrides_an_earlier_paid(self):
        plan = self._plan("paid")
        writes = allocate_payouts(
            [PayoutRecord("T1", payout_amount=100.0, status="return")],
            plan["rows_by_tracking"], plan["costs_by_row"], plan["status_by_row"],
        )
        assert writes[2][STATUS_COL] == "return"

    def test_every_ledger_status_is_rankable(self):
        """A status missing from _STATUS_RANK ranks -1, losing even to "ordered" — which is how
        "paid" and "return" were silently demoted before this was noticed."""
        assert all(_status_rank(s) >= 0 for s in STATUSES)

    def test_superseded_outranks_delivered_but_not_paid(self):
        assert _status_rank("delivered") < _status_rank("superseded") < _status_rank("paid")


class TestSupersededRowsAreRetired:
    """A superseded row holds a dead tracking number the groups already have. It must
    never be re-posted, insured, or handed a payout — and it must not feed the cancelled alert."""

    def _plan(self):
        return plan_tracking_submissions(
            HEADER_LIST,
            [shipped("O1", "DEAD", group="MOD", **{"Status": "superseded", "Quantity": "",
                                                    "Total Cost": ""})],
        )

    def test_it_is_never_submitted_insured_or_paid(self):
        plan = self._plan()
        assert not plan["by_group"]
        assert "DEAD" not in plan["rows_by_tracking"]
        assert 2 not in plan["costs_by_row"] and 2 not in plan["status_by_row"]
        assert plan["skipped_superseded"] == 1
        assert plan["superseded_rows"] == [(2, "O1", "DEAD")]
        # Even a group reporting the dead number finds no row to put money on.
        writes = allocate_payouts(
            [PayoutRecord("DEAD", payout_amount=100.0, status="paid")],
            plan["rows_by_tracking"], plan["costs_by_row"], plan["status_by_row"],
        )
        assert writes == {}

    def test_it_is_not_a_cancelled_alert_candidate(self):
        plan = self._plan()
        assert plan["skipped_cancelled"] == 0 and plan["cancelled_by_group"] == {}


class TestPartiallyCancelledOrders:
    """order BBY01-809900000011 was split into two shipments and ONE was cancelled
    on purpose; every run alerted that the order was cancelled. The alert is due only when the
    buying group still expects more units than are coming."""

    class FakeClient:
        group_key = "BFMR"

        def __init__(self, open_orders=(), held=None):
            self.open_orders, self.held = set(open_orders), dict(held or {})
            self.asked_open, self.asked_qty = None, None

        def active_purchases_for(self, order_ids):
            self.asked_open = list(order_ids)
            return {o for o in self.asked_open if o in self.open_orders}

        def active_purchase_quantities_for(self, order_ids):
            self.asked_qty = list(order_ids)
            return {o: q for o, q in self.held.items() if o in self.asked_qty}

    @staticmethod
    def _plan():
        return plan_tracking_submissions(HEADER_LIST, [
            shipped("HALF", "T1", **{"Shipment": 1, "Quantity": 1}),
            shipped("HALF", "", **{"Shipment": 2, "Quantity": 1, "Status": "cancelled"}),
            shipped("GONE", "", **{"Quantity": 2, "Status": "cancelled"}),
        ])

    def test_the_plan_counts_the_units_still_coming_per_order(self):
        plan = self._plan()
        assert plan["live_quantity_by_order"] == {"HALF": 1}  # GONE has nothing coming
        assert sorted(plan["cancelled_by_group"]["BFMR"]) == [(3, "HALF"), (4, "GONE")]

    def test_a_partial_cancellation_the_group_already_reduced_is_silent(self, monkeypatch):
        sent = []
        monkeypatch.setattr("sync_tracking.alert", lambda subject, body: sent.append((subject, body)))
        client = self.FakeClient(open_orders={"HALF"}, held={"HALF": 1})
        sync_tracking._alert_on_cancelled_orders("BFMR", client, self._plan(), apply=True)
        sync_tracking._alert_on_over_reserved_orders("BFMR", client, self._plan(), apply=True)
        assert sent == []  # an open purchase for the half still coming is right, not a divergence
        assert client.asked_open == ["GONE"]  # only the fully cancelled order is checked for openness

    def test_a_partial_cancellation_the_group_still_shows_in_full_alerts(self, monkeypatch):
        sent = []
        monkeypatch.setattr("sync_tracking.alert", lambda subject, body: sent.append((subject, body)))
        client = self.FakeClient(open_orders={"HALF"}, held={"HALF": 2})
        sync_tracking._alert_on_over_reserved_orders("BFMR", client, self._plan(), apply=True)
        assert len(sent) == 1
        subject, body = sent[0]
        assert subject == "Action needed: BFMR — 1 order(s) expect more units than are coming"
        assert "order HALF: 1 unit(s) still coming, BFMR shows 2 (cancelled rows: 3)" in body
        assert "Reduce the purchase quantity in My Tracker" in body and "never reduces" in body

    def test_the_mismatch_alerts_with_no_cancelled_row_on_the_ledger(self, monkeypatch):
        # the cancelled line of a live order is no longer recorded -- the alert
        # must still fire when BFMR keeps the original quantity.
        sent = []
        monkeypatch.setattr("sync_tracking.alert", lambda subject, body: sent.append((subject, body)))
        plan = plan_tracking_submissions(HEADER_LIST, [shipped("HALF", "T1", **{"Shipment": 1, "Quantity": 1})])
        client = self.FakeClient(open_orders={"HALF"}, held={"HALF": 2})
        sync_tracking._alert_on_over_reserved_orders("BFMR", client, plan, apply=True)
        assert len(sent) == 1 and "order HALF: 1 unit(s) still coming, BFMR shows 2" in sent[0][1]
        assert "cancelled rows" not in sent[0][1]

    def test_settled_and_unresolved_orders_are_not_asked(self, monkeypatch):
        sent = []
        monkeypatch.setattr("sync_tracking.alert", lambda subject, body: sent.append((subject, body)))
        plan = plan_tracking_submissions(HEADER_LIST, [
            shipped("PAID", "T1", **{"Quantity": 1, "Status": "paid"}),
            shipped("SPLIT", "T2", **{"Quantity": 1}),
            shipped("SPLIT", "T3", **{"Shipment": 2, "Quantity": "*"}),
        ])
        client = self.FakeClient(held={"PAID": 2, "SPLIT": 5})
        sync_tracking._alert_on_over_reserved_orders("BFMR", client, plan, apply=True)
        assert sent == [] and (client.asked_qty is None or not ({"PAID", "SPLIT"} & set(client.asked_qty)))

    def test_a_fully_cancelled_order_still_open_at_the_group_alerts_as_before(self, monkeypatch):
        sent = []
        monkeypatch.setattr("sync_tracking.alert", lambda subject, body: sent.append((subject, body)))
        client = self.FakeClient(open_orders={"GONE"}, held={})
        sync_tracking._alert_on_cancelled_orders("BFMR", client, self._plan(), apply=True)
        assert len(sent) == 1 and sent[0][0] == "Action needed: BFMR — 1 cancelled order(s) still hold a purchase"
        assert "order GONE (row 4)" in sent[0][1] and "HALF" not in sent[0][1]

    def test_a_group_that_cannot_report_quantities_is_not_asked_about_partials(self, monkeypatch):
        sent = []
        monkeypatch.setattr("sync_tracking.alert", lambda subject, body: sent.append((subject, body)))

        class Mod:
            def active_purchases_for(self, order_ids):
                return set(order_ids)

        plan = {"cancelled_by_group": {"MOD": [(3, "HALF")]}, "live_quantity_by_order": {"HALF": 1},
                "rows_by_order": {"MOD": {"HALF": [2]}}, "status_by_row": {2: "shipped"}}
        sync_tracking._alert_on_cancelled_orders("MOD", Mod(), plan, apply=True)
        sync_tracking._alert_on_over_reserved_orders("MOD", Mod(), plan, apply=True)
        assert sent == []


class TestTrackingSubmittedCheckbox:
    def _plan(self, **overrides):
        return plan_tracking_submissions(HEADER_LIST, [shipped("O1", "T1", **overrides)])

    def test_nothing_is_ticked_on_a_dry_run(self):
        assert _tick_submitted({2}, self._plan(), apply=False) == {}

    def test_an_accepted_row_writes_a_real_boolean(self):
        """A real bool, not the string "TRUE" — the checkbox column only ticks for a boolean,
        and the RAW write that carries it stores a string as a string."""
        assert _tick_submitted({2}, self._plan(), apply=True)[2][SUBMITTED_COL] is True

    def test_an_already_ticked_row_is_left_alone(self):
        """Re-writing True over True is harmless but would queue a cell write and a formula re-stamp
        for every package on every run."""
        assert _tick_submitted({2}, self._plan(**{"Tracking Submitted": True}), apply=True) == {}

    def test_a_formatted_read_of_a_ticked_box_also_counts_as_ticked(self):
        """A FORMATTED read returns the string "TRUE" where an unformatted one returns a bool; both
        have to mean the same thing or the box gets re-written forever."""
        assert _tick_submitted({2}, self._plan(**{"Tracking Submitted": "TRUE"}), apply=True) == {}

    def test_an_unticked_box_does_not_block_a_later_tick(self):
        plan = self._plan(**{"Tracking Submitted": False})
        assert _tick_submitted({2}, plan, apply=True)[2][SUBMITTED_COL] is True

    def test_a_row_that_was_not_submitted_is_not_ticked(self):
        assert _tick_submitted(set(), self._plan(), apply=True) == {}


class TestNeedsManualAlerting:
    """A Best Buy combined carton can't be submitted by any retry of ours, so it gets its own alert.

    These assert the WIRING — that `needs_manual` reaches an alert with an actionable subject and is
    excluded from insurance — rather than re-testing the BFMR detection itself.
    """

    def test_a_needs_manual_result_alerts_separately_from_a_failure(self, monkeypatch):
        from buying_groups.base import SubmissionResult

        sent = []
        monkeypatch.setattr("sync_tracking.alert", lambda subject, body: sent.append(subject))

        result = SubmissionResult(needs_manual=[("T1", "duplicate tracking, resubmit as T1B")])
        sync_tracking._alert(
            True,
            f"ACTION NEEDED — BFMR: {len(result.needs_manual)} package(s) could not be submitted",
            "\n\n".join(r for _t, r in result.needs_manual),
        )
        assert sent and sent[0].startswith("ACTION NEEDED")

    def test_the_summary_names_them_so_a_run_log_cannot_read_as_clean(self):
        from buying_groups.base import SubmissionResult

        summary = SubmissionResult(submitted=["A"], needs_manual=[("T1", "…")]).summary()
        assert "1 need manual action" in summary


class TestUnsubmittableAlertsImmediately:
    """A tracking number that cannot be submitted is alerted RIGHT AWAY, never held.

    An earlier version waited for `delivered` to avoid crying wolf while BFMR's asynchronous Best Buy
    check was still running. That was backwards: **most buying groups only insure a package if its
    tracking number was submitted BEFORE delivery**, so delivery is precisely the
    moment the alert stops being actionable. Waiting for certainty costs the cover the alert exists to
    protect; a false alarm costs one glance at My Tracker.
    """

    @staticmethod
    def _plan(**over):
        base = {
            "by_group": {}, "rows_by_tracking": {}, "costs_by_row": {}, "status_by_row": {},
            "insurance_by_row": {}, "submitted_by_row": {}, "unresolved_split": [],
            "unroutable_tracked": [], "skipped_no_tracking": 0, "skipped_unroutable": {},
            "skipped_cancelled": 0, "cancelled_by_group": {},
        }
        base.update(over)
        return base

    def test_the_hold_is_gone(self):
        """Asserted by ABSENCE so the deferral cannot be reintroduced without this failing."""
        assert not hasattr(sync_tracking, "_is_held")
        assert "deferrable" not in SubmissionResult().__dataclass_fields__

    def test_an_unroutable_shipped_row_warns_once_per_group(self, monkeypatch):
        """A group the config does not know is a gap to fill, not an emergency:
        a calm alert ONCE per group name, remembered in .state.json; the log and the activity feed
        carry it on every run."""
        from config.loader import load_state
        from diagnostics import activity

        sent = []
        monkeypatch.setattr("sync_tracking.alert", lambda subject, body: sent.append((subject, body)))

        plan = self._plan(unroutable_tracked=[(7, "ORDER-1", "1Z999", "AI")])
        sync_tracking._alert_on_unroutable(plan, apply=True)
        subject, body = sent[0]
        assert subject == "Buying group not set up: AI — 1 shipped package(s) waiting"
        assert "ACTION NEEDED" not in subject and "BEFORE DELIVERY" not in body
        assert "row 7" in body and "1Z999" in body and "once per group" in body
        assert load_state()["unroutable_groups_alerted"]["AI"]
        assert activity.read()[0]["summary"].startswith("1 shipped row(s) have a Buying Group that is not configured (AI)")

        sync_tracking._alert_on_unroutable(plan, apply=True)  # the next run: no second alert
        assert len(sent) == 1 and len(activity.read()) == 2  # but the feed has it again
        sync_tracking._alert_on_unroutable(
            self._plan(unroutable_tracked=[(9, "ORDER-2", "1Z777", "NewGroup")]), apply=True)
        assert len(sent) == 2 and "NewGroup" in sent[1][0]  # a NEW name is alerted

    def test_an_untracked_unroutable_row_does_not_alert(self, monkeypatch):
        """An unclassified row with nothing to submit yet is a config gap to fix at leisure — no
        package is in the carrier's hands, so no clock is running. Only the planner decides this, by
        collecting rows that HAVE a tracking number."""
        sent = []
        monkeypatch.setattr("sync_tracking.alert", lambda subject, body: sent.append(subject))
        sync_tracking._alert_on_unroutable(self._plan(), apply=True)
        assert sent == []

    def test_the_planner_only_collects_unroutable_rows_that_shipped(self):
        rows = [
            shipped("A", "1Z999", group="Unclassified"),
            shipped("B", "", group="Unclassified", **{"Status": "ordered"}),
        ]
        plan = plan_tracking_submissions(list(HEADER), rows)
        assert [r[1] for r in plan["unroutable_tracked"]] == ["A"]
        assert plan["skipped_unroutable"] == {"Unclassified": 1}

    def test_a_dry_run_still_sends_nothing(self, monkeypatch):
        sent = []
        monkeypatch.setattr("sync_tracking.alert", lambda subject, body: sent.append(subject))
        sync_tracking._alert_on_unroutable(
            self._plan(unroutable_tracked=[(7, "ORDER-1", "1Z999", "Unclassified")]), apply=False)
        assert sent == []


class TestCancelledPurchaseAlert:
    """BFMR cancelled the purchase while the retailer order is still coming — alert, never act.

    Only reachable because the planner now KEEPS awaiting-shipment rows instead of merely counting
    them. That population is the whole point: BFMR's deadline is for submitting tracking, so a
    cancellation can only happen before a number exists, and rows without one used to reach no BFMR
    call at all.
    """

    class FakeClient:
        group_key = "BFMR"

        def __init__(self, dead=()):
            self.dead = set(dead)
            self.asked = None

        def cancelled_purchases_for(self, order_ids):
            self.asked = list(order_ids)
            return {o for o in self.asked if o in self.dead}

    @staticmethod
    def _plan(awaiting):
        return {"awaiting_by_group": {"BFMR": awaiting}}

    def test_a_cancelled_purchase_on_an_incoming_order_alerts(self, monkeypatch):
        sent = []
        monkeypatch.setattr("sync_tracking.alert", lambda subject, body: sent.append((subject, body)))

        sync_tracking._alert_on_cancelled_purchases(
            "BFMR", self.FakeClient(dead={"COMING"}),
            self._plan([(4, "COMING"), (5, "FINE")]), apply=True)

        subject, body = sent[0]
        assert subject.startswith("Action needed")
        assert "cancelled purchase" in subject
        assert "row 4" in body and "COMING" in body
        assert "FINE" not in body, "only the affected order"
        # The two things that make it actionable rather than merely alarming.
        assert "nothing will be paid" in body
        assert "Before they arrive" in body

    def test_it_says_plainly_that_nothing_was_changed(self, monkeypatch):
        """This tool never cancels anything at a buying group. The alert has to say so, or a reader
        could reasonably assume it tidied up on their behalf."""
        sent = []
        monkeypatch.setattr("sync_tracking.alert", lambda subject, body: sent.append(body))
        sync_tracking._alert_on_cancelled_purchases(
            "BFMR", self.FakeClient(dead={"COMING"}), self._plan([(4, "COMING")]), apply=True)
        assert "never cancels" in sent[0]

    def test_a_healthy_incoming_order_is_silent(self, monkeypatch):
        sent = []
        monkeypatch.setattr("sync_tracking.alert", lambda subject, body: sent.append(subject))
        sync_tracking._alert_on_cancelled_purchases(
            "BFMR", self.FakeClient(), self._plan([(4, "FINE")]), apply=True)
        assert sent == []

    def test_a_provider_without_the_read_is_skipped_not_crashed(self, monkeypatch):
        """MOD has no purchase concept at all, so the capability is probed rather than assumed."""
        class ModLike:
            group_key = "MOD"
        sync_tracking._alert_on_cancelled_purchases(
            "MOD", ModLike(), {"awaiting_by_group": {"MOD": [(4, "X")]}}, apply=True)

    def test_a_dry_run_sends_nothing(self, monkeypatch):
        sent = []
        monkeypatch.setattr("sync_tracking.alert", lambda subject, body: sent.append(subject))
        sync_tracking._alert_on_cancelled_purchases(
            "BFMR", self.FakeClient(dead={"COMING"}), self._plan([(4, "COMING")]), apply=False)
        assert sent == []

    def test_the_planner_keeps_awaiting_rows_per_group(self):
        rows = [
            shipped("AWAITING", "", group="BFMR", **{"Status": "ordered"}),
            shipped("SHIPPED", "T1", group="BFMR"),
            shipped("NOGROUP", "", group="Unclassified", **{"Status": "ordered"}),
        ]
        plan = plan_tracking_submissions(list(HEADER), rows)
        assert plan["awaiting_by_group"] == {"BFMR": [(2, "AWAITING")]}
        assert plan["skipped_no_tracking"] == 2, "still counted, as before"


class TestColumnsExist:
    def test_the_columns_this_module_writes_are_real_schema_columns(self):
        """These are looked up by name against HEADER at write time; a rename would otherwise fail
        only at runtime, against the ledger."""
        for column in (INSURANCE_COL, PAYOUT_AMOUNT_COL, PAYOUT_DATE_COL,
                       STATUS_COL, SUBMITTED_COL):
            assert column in HEADER_LIST


class TestSettledPackagesAreNotReSubmitted:
    """A package the group has PAID for needs no further submitting -- being paid is proof they hold
    the number, which is far stronger evidence than the `Tracking Submitted` checkbox this module
    deliberately refuses to trust (a local mirror drifts both ways; a payout cannot).

    It matters most for MOD, whose `already_submitted` is empty BY DESIGN -- it has no way to answer
    the question, so every run re-posted every number ever recorded. One batched call hides that
    today, but the batch grows with the ledger forever.
    """

    @staticmethod
    def _rows(status, payout):
        header = list(HEADER)
        row = [""] * len(header)

        def put(name, value):
            row[header.index(name)] = value

        put("Order ID", "O-1")
        put("Order Date", "2026-08-10")
        put("Item Name", "Thing")
        put("Quantity", "1")
        put("Tracking Number", "1Z1")
        put("Shipment", "1")
        put("Status", status)
        put("Total Cost", "100.00")
        put("Buying Group", "MOD")
        put("Actual Payout", payout)
        return header, [row]

    def test_a_paid_package_with_money_is_settled(self):
        header, rows = self._rows("paid", "95.00")
        plan = plan_tracking_submissions(header, rows)
        assert ("O-1", "1Z1") in plan["settled_keys"]

    def test_paid_with_a_ZERO_payout_is_NOT_settled(self):
        """The load-bearing half. BFMR marks a package paid BEFORE amount_paid lands, so keying on
        `paid` alone would stop submitting packages the group has not actually settled."""
        header, rows = self._rows("paid", "0")
        plan = plan_tracking_submissions(header, rows)
        assert ("O-1", "1Z1") not in plan["settled_keys"]

    def test_paid_with_a_blank_payout_is_NOT_settled(self):
        header, rows = self._rows("paid", "")
        plan = plan_tracking_submissions(header, rows)
        assert ("O-1", "1Z1") not in plan["settled_keys"]

    def test_an_unpaid_package_is_never_settled(self):
        header, rows = self._rows("delivered", "")
        plan = plan_tracking_submissions(header, rows)
        assert plan["settled_keys"] == set()

    def test_a_return_row_is_settled_regardless_of_payout(self):
        """Found live: a scheduled run tried to submit a `return` row's tracking to BFMR
        and raised ACTION NEEDED. `return` was terminal for re-scraping but nothing sync-side read
        it — a returned package has nothing left to submit or insure, with or without a payout."""
        for payout in ("", "0", "-387.00", "774.00"):
            header, rows = self._rows("return", payout)
            plan = plan_tracking_submissions(header, rows)
            assert ("O-1", "1Z1") in plan["settled_keys"], f"payout={payout!r}"

    def test_a_return_row_is_still_read_for_payouts(self):
        # The clawback lands through the read — settling must not drop the row from it.
        header, rows = self._rows("return", "")
        plan = plan_tracking_submissions(header, rows)
        assert plan["rows_by_tracking"].get("1Z1") == [2]
        assert plan["by_group"]["MOD"], "the row stays in the group's list for the payout read"

    def test_a_settled_row_is_still_read_for_payouts(self):
        """Deliberately NOT dropped from the payout read. BFMR reports `returned`, which outranks
        `paid`, so a post-payment CLAWBACK is a real forward transition -- and dropping settled rows
        from the read is the one way to never see it. The read is bulk for both providers, so keeping
        them costs nothing.
        """
        header, rows = self._rows("paid", "95.00")
        plan = plan_tracking_submissions(header, rows)
        assert plan["rows_by_tracking"].get("1Z1") == [2]
        assert plan["by_group"]["MOD"], "the row stays in the group's list for the payout read"


class TestGiftCardRowsAreUnroutedOnPurpose:
    """A gift card is bookkeeping, not a resale: it cost real money, it ships with a tracking number
    like anything else, and it will never be submitted to or paid by any buying group.

    The distinction that matters is DELIBERATE vs ACCIDENTAL. An Unclassified row with a tracking
    number is money about to be lost and rightly alerts on every run; a gift card doing the same thing
    forever would train the user to ignore that alert.
    """

    def test_a_shipped_gift_card_never_reaches_the_unroutable_alert(self):
        rows = [
            shipped("GC-1", "1Z111", group="Gift Card"),
            shipped("A", "1Z999", group="Unclassified"),
        ]

        plan = plan_tracking_submissions(list(HEADER), rows)

        assert [r[1] for r in plan["unroutable_tracked"]] == ["A"], \
            "the gift card must not be alerted on; the Unclassified row must be"

    def test_it_is_still_counted_as_skipped_so_it_stays_visible(self):
        # Silent is not the same as invisible -- the run summary should still say it was passed over.
        plan = plan_tracking_submissions(list(HEADER), [shipped("GC-1", "1Z111", group="Gift Card")])

        assert plan["skipped_unroutable"] == {"Gift Card": 1}

    def test_it_is_never_submitted_to_any_group(self):
        plan = plan_tracking_submissions(list(HEADER), [shipped("GC-1", "1Z111", group="Gift Card")])

        assert plan["by_group"] == {}


class TestPayoutsOnly:
    """--payouts-only: read payouts and tick what the group holds; submit and insure NOTHING.

    For a ledger carrying orders from buying-group accounts other than the connected ones (imported
    history, 2026-08-30): posting those numbers as new packages into these accounts would be wrong."""

    class Row:
        def __init__(self, n, oid, trk):
            self.row_number, self.order_id, self.tracking_number = n, oid, trk

    class FakeClient:
        group_key = "BFMR"

        def __init__(self):
            self.submitted_with, self.insured_with, self.fetched = None, None, None

        def already_submitted(self, rows):
            return {("A1", "T1")}

        def submit_tracking(self, rows):
            self.submitted_with = list(rows)

            class R:
                submitted, submitted_for, failed, needs_manual = [], [], [], []

                def summary(self):
                    return "0 submitted"
            return R()

        def file_insurance(self, rows):
            self.insured_with = list(rows)

            class I:
                skipped, failed = [], []

                def summary(self):
                    return "0 filed"
            return I()

        def fetch_payouts(self, numbers):
            self.fetched = list(numbers)
            return []

    def test_nothing_is_submitted_or_insured_but_payouts_are_read_and_known_rows_ticked(self, monkeypatch):
        client = self.FakeClient()
        monkeypatch.setattr(sync_tracking, "get_client", lambda group, dry_run: client)
        monkeypatch.setattr(sync_tracking, "allocate_payouts", lambda *a, **k: {})
        ticked = []
        monkeypatch.setattr(sync_tracking, "_tick_submitted", lambda rows, plan, apply: ticked.extend(rows) or {})
        rows = [self.Row(4, "A1", "T1"), self.Row(5, "B2", "T2")]
        plan = {"settled_keys": set(), "rows_by_tracking": {}, "costs_by_row": {}, "status_by_row": {},
                "insurance_by_row": {}, "awaiting_by_group": {}, "cancelled_by_group": {}}

        sync_tracking._run_one_group("BFMR", rows, plan, {}, apply=True, payouts_only=True)

        assert client.submitted_with == [], "payouts-only must submit nothing"
        assert client.insured_with is None, "payouts-only must file no insurance"
        assert client.fetched == ["T1", "T2"], "but it still reads payouts for every row"
        assert ticked == [4], "and ticks only what the group already holds"

    def test_the_normal_mode_still_submits(self, monkeypatch):
        client = self.FakeClient()
        monkeypatch.setattr(sync_tracking, "get_client", lambda group, dry_run: client)
        monkeypatch.setattr(sync_tracking, "allocate_payouts", lambda *a, **k: {})
        monkeypatch.setattr(sync_tracking, "_tick_submitted", lambda rows, plan, apply: {})
        rows = [self.Row(4, "A1", "T1"), self.Row(5, "B2", "T2")]
        plan = {"settled_keys": set(), "rows_by_tracking": {}, "costs_by_row": {}, "status_by_row": {},
                "insurance_by_row": {}, "awaiting_by_group": {}, "cancelled_by_group": {}}
        sync_tracking._run_one_group("BFMR", rows, plan, {}, apply=True)
        assert [r.order_id for r in client.submitted_with] == ["B2"] and client.insured_with is not None


class TestDonationShipments:
    """BFMR's donation program: a 1-cent deal is reserved and submitted like any
    package, but BFMR refuses its insurance filing with a 400. The client skips those with
    DONATION_SKIP_REASON; the sync's job is to turn that skip into a real $0.00 in the Insurance
    cell (blank cells only) so the ledger reads "no premium, by design" — and to ALERT on any
    filing failure the donation skip does not explain, because those used to abort the whole run
    and now merely land in `failed`."""

    class Row:
        def __init__(self, n, oid, trk):
            self.row_number, self.order_id, self.tracking_number = n, oid, trk

    def _client(self, skipped=(), failed=()):
        class FakeClient:
            group_key = "BFMR"

            def already_submitted(self, rows):
                return set()

            def submit_tracking(self, rows):
                class R:
                    submitted, submitted_for, failed, needs_manual = [], [], [], []

                    def summary(self):
                        return "0 submitted"
                return R()

            def file_insurance(self, rows):
                class I:
                    def summary(self):
                        return "insurance"
                I.skipped, I.failed = list(skipped), list(failed)
                return I()

            def fetch_payouts(self, numbers):
                return []
        return FakeClient()

    def _plan(self):
        return {"settled_keys": set(), "rows_by_tracking": {"T1": [4, 5]},
                "costs_by_row": {4: 100.0, 5: 50.0}, "status_by_row": {},
                "insurance_by_row": {4: "", 5: "4.5"},
                "awaiting_by_group": {}, "cancelled_by_group": {}}

    def _run(self, monkeypatch, client):
        from buying_groups.base import PayoutRecord  # noqa: F401
        alerts = []
        monkeypatch.setattr(sync_tracking, "get_client", lambda group, dry_run: client)
        monkeypatch.setattr(sync_tracking, "allocate_payouts", lambda *a, **k: {})
        monkeypatch.setattr(sync_tracking, "_tick_submitted", lambda rows, plan, apply: {})
        monkeypatch.setattr(sync_tracking, "_alert", lambda apply, subject, body: alerts.append(subject))
        all_writes = {}
        rows = [self.Row(4, "A1", "T1"), self.Row(5, "A1", "T1")]
        sync_tracking._run_one_group("BFMR", rows, self._plan(), all_writes, apply=True)
        return all_writes, alerts

    def test_a_donation_skip_writes_a_real_zero_into_blank_insurance_cells(self, monkeypatch):
        from buying_groups.bfmr import DONATION_SKIP_REASON

        writes, alerts = self._run(monkeypatch, self._client(skipped=[("T1", DONATION_SKIP_REASON)]))
        assert writes[4][sync_tracking.INSURANCE_COL] == 0.0
        assert 5 not in writes, "the hand-typed 4.5 on row 5 survives"
        assert alerts == [], "a donation is by design, not a problem to page about"

    def test_an_ordinary_skip_writes_nothing(self, monkeypatch):
        writes, alerts = self._run(monkeypatch, self._client(skipped=[("T1", "already insured")]))
        assert writes == {} and alerts == []

    def test_a_real_filing_failure_alerts_and_writes_nothing(self, monkeypatch):
        # A refusal on a package that SHOULD be covered used to abort the whole sync (loud); now it
        # is caught per shipment, so the alert is the only thing keeping it visible.
        writes, alerts = self._run(
            monkeypatch, self._client(failed=[("T1", "T1: POST ... returned 400: nope")]))
        assert writes == {}
        assert alerts and "insurance filing(s) rejected" in alerts[0]


class TestDealScopedAllocation:
    """One order, two deals, one box, DIVERGING outcomes. Records carry BFMR's own wording (`item_hint`); rows are partitioned by word
    overlap with their Item Name, and anything unclean falls back to the merged behavior."""

    ROWS = {"T1": [11, 12]}
    COSTS = {11: 68.0, 12: 60.0}
    STATUS = {11: "shipped", 12: "shipped"}
    ORDERS = {11: "O1", 12: "O1"}
    ITEMS = {11: "Apple AirTag (2nd Generation) - 4 Pack: Tracker",
             12: "Fitbit Google Air - Screenless Activity Tracker, Obsidian"}

    def _writes(self, records, items=None):
        from buying_groups.base import PayoutRecord
        return sync_tracking.allocate_payouts(
            [PayoutRecord(**r) for r in records],
            rows_by_tracking=dict(self.ROWS), costs_by_row=dict(self.COSTS),
            status_by_row=dict(self.STATUS), order_of_row=dict(self.ORDERS),
            item_of_row=dict(items if items is not None else self.ITEMS),
        )

    def test_the_returned_deal_marks_only_its_own_row(self):
        writes = self._writes([
            {"tracking_number": "T1", "order_id": "O1", "status": "return",
             "item_hint": "Apple AirTag - Four Pack (2nd Generation) Apple AirTag"},
            {"tracking_number": "T1", "order_id": "O1", "status": "paid", "payout_amount": 97.0,
             "payout_date": "2026-09-03",
             "item_hint": "Google - Fitbit Air - Obsidian Google - Fitbit Air"},
        ])
        assert writes[11][sync_tracking.STATUS_COL] == "return"
        assert sync_tracking.PAYOUT_AMOUNT_COL not in writes[11], "the returned deal has no money"
        assert writes[12][sync_tracking.STATUS_COL] == "paid"
        assert writes[12][sync_tracking.PAYOUT_AMOUNT_COL] == 97.0, "the paid deal's FULL amount"
        assert writes[12][sync_tracking.PAYOUT_DATE_COL] == "2026-09-03"

    def test_indistinguishable_rows_fall_back_to_the_merged_order_level(self):
        # Two rows with the SAME item name can't be told apart -- the old (coarse but money-safe)
        # merge applies rather than a guess.
        writes = self._writes(
            [
                {"tracking_number": "T1", "order_id": "O1", "status": "return",
                 "item_hint": "Apple AirTag - Four Pack"},
                {"tracking_number": "T1", "order_id": "O1", "status": "paid",
                 "payout_amount": 97.0, "item_hint": "Google - Fitbit Air"},
            ],
            items={11: "Widget", 12: "Widget"},
        )
        assert writes[11][sync_tracking.STATUS_COL] == "return"
        assert writes[12][sync_tracking.STATUS_COL] == "return"

    def test_a_deal_matching_no_row_falls_back_rather_than_dropping_its_money(self):
        writes = self._writes(
            [
                {"tracking_number": "T1", "order_id": "O1", "status": "paid",
                 "payout_amount": 50.0, "item_hint": "Apple AirTag"},
                {"tracking_number": "T1", "order_id": "O1", "status": "paid",
                 "payout_amount": 47.0, "item_hint": "PlayStation 6 Console"},
            ],
            items={11: "Apple AirTag 4 Pack", 12: "Apple AirTag 4 Pack Extra"},
        )
        total = sum(w[sync_tracking.PAYOUT_AMOUNT_COL] for w in writes.values())
        assert round(total, 2) == 97.0, "the unmatchable deal's money folds into the order, not away"

    def test_records_without_hints_keep_the_old_order_level_merge(self):
        writes = self._writes([
            {"tracking_number": "T1", "order_id": "O1", "status": "paid", "payout_amount": 97.0},
            {"tracking_number": "T1", "order_id": "O1", "status": "return"},
        ])
        assert writes[11][sync_tracking.STATUS_COL] == "return"
        assert writes[12][sync_tracking.STATUS_COL] == "return"


class TestOrderScopedAllocation:
    """One tracking number, two orders, two outcomes. The paid order's rows must get the full payout and
    stay paid; the returned order's rows get the return; nothing is netted across orders."""

    def _writes(self, records, order_of_row=None):
        from buying_groups.base import PayoutRecord
        return sync_tracking.allocate_payouts(
            [PayoutRecord(**r) for r in records],
            rows_by_tracking={"T1": [4, 5]},
            costs_by_row={4: 2392.0, 5: 299.0},
            status_by_row={4: "delivered", 5: "delivered"},
            order_of_row=order_of_row or {4: "A-PAID", 5: "B-RETURNED"},
        )

    def test_two_orders_one_tracking_are_scoped_not_netted(self):
        writes = self._writes([
            {"tracking_number": "T1", "order_id": "A-PAID", "payout_amount": 2392.0,
             "payout_date": "2026-06-05", "status": "paid"},
            {"tracking_number": "T1", "order_id": "B-RETURNED", "status": "return"},
        ])
        assert writes[4]["Actual Payout"] == 2392.0 and writes[4]["Status"] == "paid"
        assert "Actual Payout" not in writes[5] and writes[5]["Status"] == "return"

    def test_a_fee_row_without_an_order_shares_insurance_across_the_whole_package(self):
        writes = self._writes([
            {"tracking_number": "T1", "order_id": "A-PAID", "payout_amount": 2392.0, "status": "paid"},
            {"tracking_number": "T1", "insurance": 10.0},
        ])
        assert writes[4]["Insurance"] == round(10.0 * 2392.0 / 2691.0, 2)
        assert writes[5]["Insurance"] == round(10.0 * 299.0 / 2691.0, 2)
        assert writes[4]["Actual Payout"] == 2392.0 and "Actual Payout" not in writes[5]

    def test_an_unknown_order_folds_back_to_tracking_level(self):
        writes = self._writes([
            {"tracking_number": "T1", "order_id": "SOMEONE-ELSE", "payout_amount": 100.0, "status": "paid"},
        ], order_of_row={4: "A", 5: "B"})
        # nobody claims it by order, so it spreads across the package as before
        assert round(writes[4]["Actual Payout"] + writes[5]["Actual Payout"], 2) == 100.0

    def test_records_without_order_ids_behave_exactly_as_before(self):
        writes = self._writes([
            {"tracking_number": "T1", "payout_amount": 269.1, "payout_date": "2026-06-30", "status": "paid"},
        ])
        assert writes[4]["Actual Payout"] == round(269.1 * 2392.0 / 2691.0, 2)
        assert writes[5]["Actual Payout"] == round(269.1 * 299.0 / 2691.0, 2)


class TestExpectedPayoutPlanning:
    """The planner's half of the committed-price watch: which rows can carry the commitment.

    The commitment is keyed by ORDER, not tracking — BFMR publishes the price from the moment the
    user's hand-typed order number creates the purchase, which is before any tracking number
    exists — so rows still awaiting shipment must already be in the order index. It lands in the
    existing Actual Payout cell, so the planner also reads the cell as
    it stands (the baseline the change alert compares against) and the Payout Date (blank = not
    settled, the gate that keeps the commitment pass off real money).
    """

    def test_an_awaiting_row_joins_the_order_index_before_any_tracking_exists(self):
        plan = plan_tracking_submissions(HEADER_LIST, [
            shipped("O1", "", **{"Status": "ordered", "Total Cost": 250, "Item Name": "MacBook"}),
        ])
        assert plan["rows_by_order"]["BFMR"]["O1"] == [2]
        assert plan["costs_by_row"][2] == 250.0
        assert plan["item_of_row"][2] == "MacBook"
        assert plan["payout_by_row"][2] is None
        assert plan["date_by_row"][2] == ""
        assert not plan["rows_by_tracking"], "no tracking, so the payout allocation can't reach it"

    def test_a_tracked_row_carries_its_recorded_commitment_and_settlement_state(self):
        plan = plan_tracking_submissions(HEADER_LIST, [
            shipped("O1", "T1", **{EXPECTED_PAYOUT_COL: 500}),
            shipped("O2", "T2", **{PAYOUT_AMOUNT_COL: 97, PAYOUT_DATE_COL: "2026-09-01",
                                   EXPECTED_PAYOUT_COL: 100}),
        ])
        assert plan["expected_by_row"][2] == 500.0, "the recorded commitment is the baseline"
        assert plan["payout_by_row"][2] is None
        assert plan["date_by_row"][2] == ""
        assert plan["date_by_row"][3] == "2026-09-01", "a dated payout is settled money"
        assert plan["expected_by_row"][3] == 100.0 and plan["payout_by_row"][3] == 97.0

    def test_a_ledger_from_before_the_column_reads_no_commitment_and_still_syncs(self):
        header = [h for h in HEADER_LIST if h != EXPECTED_PAYOUT_COL]
        rows = [shipped("O1", "T1")[: len(header)]]
        plan = plan_tracking_submissions(header, rows)
        assert plan["expected_by_row"][2] is None
        assert [s.order_id for s in plan["by_group"]["BFMR"]] == ["O1"]

    def test_retired_and_cancelled_rows_stay_out_of_the_order_index(self):
        plan = plan_tracking_submissions(HEADER_LIST, [
            shipped("O1", "T1", **{"Status": "superseded"}),
            shipped("O2", "T2", **{"Status": "cancelled"}),
        ])
        assert plan["rows_by_order"] == {}

    def test_an_unroutable_awaiting_row_is_not_indexed(self):
        plan = plan_tracking_submissions(HEADER_LIST, [
            shipped("O1", "", **{"Status": "ordered"}, group="Unclassified"),
        ])
        assert plan["rows_by_order"] == {}


class TestExpectedPayoutAllocation:
    """allocate_expected_payouts: prorate the commitment into Expected Payout (its own column
    since 2026-09-18), notice when it MOVES, and never touch a row that holds (or is this run
    receiving) real money -- a settled row's commitment stays as the record of what was promised."""

    COL = EXPECTED_PAYOUT_COL

    @staticmethod
    def _alloc(records, rows_by_order, costs, items=None, status=None, expected=None,
               dates=None, settling=None, by_tracking=None):
        return allocate_expected_payouts(
            [PayoutRecord(**r) for r in records], rows_by_order, costs,
            items or {}, status or {}, expected or {}, dates or {}, settling or set(),
            rows_by_tracking=by_tracking,
        )

    def test_a_first_sighting_prorates_by_cost_and_raises_no_alert(self):
        writes, changes = self._alloc(
            [{"tracking_number": "", "order_id": "O1", "expected_amount": 1230.0}],
            {"O1": [2, 3]}, {2: 100.0, 3: 300.0},
        )
        assert writes == {2: {self.COL: 307.5}, 3: {self.COL: 922.5}}
        assert changes == []

    def test_an_unchanged_commitment_queues_no_writes_at_all(self):
        writes, changes = self._alloc(
            [{"tracking_number": "", "order_id": "O1", "expected_amount": 1230.0}],
            {"O1": [2, 3]}, {2: 100.0, 3: 300.0},
            expected={2: 307.5, 3: 922.5},
        )
        assert writes == {} and changes == []

    def test_a_moved_price_rewrites_the_cells_and_names_old_and_new(self):
        writes, changes = self._alloc(
            [{"tracking_number": "", "order_id": "O1", "expected_amount": 1100.0,
              "item_hint": "MacBook Air 13"}],
            {"O1": [2, 3]}, {2: 100.0, 3: 300.0},
            expected={2: 307.5, 3: 922.5},
        )
        assert writes == {2: {self.COL: 275.0}, 3: {self.COL: 825.0}}
        assert len(changes) == 1
        assert "$1,230.00 -> $1,100.00" in changes[0] and "O1" in changes[0]

    def test_proration_rounding_drift_is_not_a_price_change(self):
        """Three-way splits round each cell to the cent; the re-read must not see its own rounding
        as BFMR moving the price, or every run alerts forever."""
        writes, changes = self._alloc(
            [{"tracking_number": "", "order_id": "O1", "expected_amount": 100.0}],
            {"O1": [2, 3, 4]}, {2: 1.0, 3: 1.0, 4: 1.0},
            expected={2: 33.33, 3: 33.33, 4: 33.33},
        )
        assert changes == [], "99.99 vs 100.00 is rounding, not a repriced deal"

    def test_paid_and_returned_rows_keep_whatever_they_hold(self):
        writes, changes = self._alloc(
            [{"tracking_number": "", "order_id": "O1", "expected_amount": 1230.0}],
            {"O1": [2, 3]}, {2: 100.0, 3: 300.0},
            status={2: "paid", 3: "return"},
        )
        assert writes == {} and changes == []

    def test_a_dated_payout_is_settled_money_and_is_never_touched(self):
        """MOD-style dateless `paid` rows are covered by status; a dated cell is covered here —
        real money either way, and the commitment pass must not overwrite or 'correct' it."""
        writes, changes = self._alloc(
            [{"tracking_number": "", "order_id": "O1", "expected_amount": 1230.0}],
            {"O1": [2]}, {2: 100.0},
            expected={2: 905.0}, dates={2: "2026-09-01"},
        )
        assert writes == {} and changes == []

    def test_rows_being_settled_this_run_drop_out_without_a_spurious_price_alert(self):
        """A partial settlement (a 2+1 split, one box paid first): entry A settles — its record
        has left the commitment read AND its row is in settling_rows — while entry B stays open.
        The bucket total drops by A's share and A's row leaves the membership in the same run, so
        the remaining totals still agree: no fake 'price change', no write over the settlement."""
        writes, changes = self._alloc(
            [{"tracking_number": "", "order_id": "O1", "expected_amount": 1349.0,
              "item_hint": "MacBook Air"}],  # only B's commitment survives the read
            {"O1": [2, 3]}, {2: 2000.0, 3: 1000.0},
            expected={2: 2698.0, 3: 1349.0},  # the cells as the previous run left them
            settling={2},                     # A's row: allocate_payouts is writing it real money
        )
        assert writes == {}, "B's cell already agrees; A's belongs to allocate_payouts this run"
        assert changes == [], "a settlement is not a repricing"

    def test_a_zero_commitment_is_never_written(self):
        """Same rule as the settlement path: a literal 0 in Actual Payout makes Total Profit
        compute a large fictitious loss."""
        writes, changes = self._alloc(
            [{"tracking_number": "", "order_id": "O1", "expected_amount": 0.0}],
            {"O1": [2]}, {2: 100.0},
        )
        assert writes == {} and changes == []

    def test_a_new_row_under_the_same_commitment_is_filled_quietly(self):
        """A split appends a row: the total still agrees, so the cells re-prorate with NO alert."""
        writes, changes = self._alloc(
            [{"tracking_number": "", "order_id": "O1", "expected_amount": 1230.0}],
            {"O1": [2, 3, 4]}, {2: 100.0, 3: 100.0, 4: 100.0},
            expected={2: 615.0, 3: 615.0, 4: None},
        )
        assert writes == {2: {self.COL: 410.0}, 3: {self.COL: 410.0}, 4: {self.COL: 410.0}}
        assert changes == []

    def test_two_deals_of_one_order_split_by_item_words(self):
        writes, _ = self._alloc(
            [{"tracking_number": "", "order_id": "O1", "expected_amount": 80.0,
              "item_hint": "AirTag 4 Pack"},
             {"tracking_number": "", "order_id": "O1", "expected_amount": 97.0,
              "item_hint": "Fitbit Versa 4"}],
            {"O1": [2, 3]}, {2: 79.0, 3: 99.0},
            items={2: "Apple AirTag 4 Pack", 3: "Fitbit Versa 4 Smartwatch"},
        )
        assert writes == {2: {self.COL: 80.0}, 3: {self.COL: 97.0}}

    def test_an_unclean_partition_falls_back_to_the_order_level(self):
        writes, _ = self._alloc(
            [{"tracking_number": "", "order_id": "O1", "expected_amount": 80.0,
              "item_hint": "Mystery Deal"},
             {"tracking_number": "", "order_id": "O1", "expected_amount": 100.0,
              "item_hint": "Other Mystery"}],
            {"O1": [2, 3]}, {2: 100.0, 3: 100.0},
            items={2: "Widget", 3: "Widget"},
        )
        assert writes == {2: {self.COL: 90.0}, 3: {self.COL: 90.0}}, (
            "who-gets-what is unknowable, but the order's TOTAL commitment still is not")

    def test_an_order_the_ledger_does_not_know_waits_for_the_next_run(self):
        writes, changes = self._alloc(
            [{"tracking_number": "", "order_id": "NOT-SCRAPED-YET", "expected_amount": 500.0}],
            {"O1": [2]}, {2: 100.0},
        )
        assert writes == {} and changes == []

    def test_records_without_a_commitment_or_an_order_are_ignored(self):
        writes, changes = self._alloc(
            [{"tracking_number": "T1", "order_id": "O1"},               # no expected_amount
             {"tracking_number": "T1", "expected_amount": 500.0}],     # no order_id
            {"O1": [2]}, {2: 100.0},
        )
        assert writes == {} and changes == []


class TestExpectedPaymentMismatch:
    """The settlement-time half: BFMR paid a different figure than it committed to."""

    @staticmethod
    def _lines(record_kwargs, date_by_row=None, rows=(2,)):
        plan = {
            "rows_by_tracking": {"T1": list(rows)},
            "date_by_row": date_by_row if date_by_row is not None else {2: ""},
        }
        return _expected_payment_mismatches(
            [PayoutRecord(tracking_number="T1", **record_kwargs)], plan)

    def test_a_short_pay_is_reported_with_both_figures(self):
        lines = self._lines({"order_id": "O1", "payout_amount": 900.0, "expected_amount": 905.0,
                             "item_hint": "MacBook Air"})
        assert len(lines) == 1
        assert "$900.00" in lines[0] and "$905.00" in lines[0] and "O1" in lines[0]

    def test_a_payment_matching_the_commitment_is_silent(self):
        assert self._lines({"payout_amount": 905.0, "expected_amount": 905.0}) == []

    def test_a_cent_of_rounding_is_not_a_short_pay(self):
        assert self._lines({"payout_amount": 905.0, "expected_amount": 905.02}) == []

    def test_it_fires_only_while_the_payout_date_is_still_unwritten(self):
        """The run that first writes the settlement alerts; every later run sees the DATE filled
        and stays quiet — a mismatch repeated forever is a mismatch that gets muted. The amount
        cell can't be the gate: it holds the commitment long before settlement."""
        assert self._lines({"payout_amount": 900.0, "expected_amount": 905.0},
                           date_by_row={2: "2026-09-10"}) == []

    def test_a_package_the_ledger_does_not_know_is_silent(self):
        assert self._lines({"payout_amount": 900.0, "expected_amount": 905.0}, rows=()) == []

    def test_a_record_with_no_commitment_is_silent(self):
        assert self._lines({"payout_amount": 900.0}) == []


class TestRowsHeldByGroupIsPerOrder:
    """Amazon put two ORDERS in one box (TBA999000000010 -- 3 iPads of one order,
    2 toys of another). The iPad order's push landed; the toy order had no BFMR purchase and was
    refused. Keyed on the bare number, the tick marked the toy rows as submitted too."""

    def _row(self, n, order, trk="TBA999000000010"):
        from types import SimpleNamespace
        return SimpleNamespace(row_number=n, order_id=order, tracking_number=trk)

    def test_only_the_landed_orders_rows_are_ticked(self):
        from buying_groups.base import SubmissionResult
        from sync_tracking import _rows_held_by_group
        rows = [self._row(8, "111-9990022-9990022"),
                self._row(3, "111-9990007-9990007"), self._row(4, "111-9990007-9990007")]
        push = SubmissionResult(submitted=["TBA999000000010"],
                                submitted_for=[("111-9990022-9990022", "TBA999000000010")])
        assert _rows_held_by_group(rows, known=set(), push=push) == {8}

    def test_already_known_pairs_tick_alongside_this_runs_pushes(self):
        from buying_groups.base import SubmissionResult
        from sync_tracking import _rows_held_by_group
        rows = [self._row(2, "A", "T1"), self._row(3, "B", "T2"), self._row(4, "C", "T3")]
        push = SubmissionResult(submitted=["T2"], submitted_for=[("B", "T2")])
        assert _rows_held_by_group(rows, known={("A", "T1")}, push=push) == {2, 3}



class TestExpectedPayoutFollowsTheShipment:
    """Two reservations of ONE product on one order (BBY01-809900000012, 2026-09-21: qty 4 at $392
    and qty 1 at $399) cannot be split by item name. Once BFMR holds a tracking number for a
    purchase, its commitment scopes to that shipment's rows -- the settlement's own key -- so a
    4 + 1 split reads $1,568 / $399, not $1,967 prorated by cost."""

    COL = EXPECTED_PAYOUT_COL
    IPAD = "Apple - iPad 11-inch (A16) Wi-Fi 128GB - Silver"

    @staticmethod
    def _alloc(records, rows_by_order, costs, by_tracking, items=None, expected=None):
        return allocate_expected_payouts(
            [PayoutRecord(**r) for r in records], rows_by_order, costs, items or {}, {},
            expected or {}, {}, set(), rows_by_tracking=by_tracking,
        )

    def _deals(self, t4="T4", t1="T1"):
        return [
            {"tracking_number": t4, "order_id": "O1", "expected_amount": 1568.0,
             "item_hint": "Apple iPad 11 - 128gb - WiFi"},
            {"tracking_number": t1, "order_id": "O1", "expected_amount": 399.0,
             "item_hint": "Apple iPad 11 - 128gb - WiFi - Silver"},
        ]

    def test_a_split_lands_each_purchases_commitment_on_its_own_box(self):
        writes, changes = self._alloc(self._deals(), {"O1": [2, 3]}, {2: 2000.0, 3: 500.0},
                                      {"T4": [2], "T1": [3]}, items={2: self.IPAD, 3: self.IPAD})
        assert writes == {2: {self.COL: 1568.0}, 3: {self.COL: 399.0}} and changes == []

    def test_a_suffixed_spelling_resolves_to_the_ledgers_number(self):
        writes, _ = self._alloc(self._deals(t4="T4B"), {"O1": [2, 3]}, {2: 2000.0, 3: 500.0},
                                {"T4": [2], "T1": [3]}, items={2: self.IPAD, 3: self.IPAD})
        assert writes == {2: {self.COL: 1568.0}, 3: {self.COL: 399.0}}

    def test_one_box_holding_both_purchases_carries_their_sum(self):
        writes, _ = self._alloc(self._deals(t4="T1", t1="T1"), {"O1": [2]}, {2: 2500.0},
                                {"T1": [2]}, items={2: self.IPAD})
        assert writes == {2: {self.COL: 1967.0}}

    def test_a_purchase_still_waiting_takes_the_rows_no_shipment_has_claimed(self):
        # The qty-4 box shipped and is attached; the qty-1 purchase has no tracking yet.
        writes, _ = self._alloc(self._deals(t1=""), {"O1": [2, 3]}, {2: 2000.0, 3: 500.0},
                                {"T4": [2]}, items={2: self.IPAD, 3: self.IPAD})
        assert writes == {2: {self.COL: 1568.0}, 3: {self.COL: 399.0}}

    def test_a_waiting_purchase_with_no_row_left_falls_back_to_the_whole_order(self):
        # One row holds every unit; the attached purchase claims it, the waiting one would have
        # nowhere to land -- so the order-level merge keeps the full $1,967 on that row.
        writes, _ = self._alloc(self._deals(t1=""), {"O1": [2]}, {2: 2500.0},
                                {"T4": [2]}, items={2: self.IPAD})
        assert writes == {2: {self.COL: 1967.0}}

    def test_without_the_tracking_index_the_order_level_split_stands(self):
        writes, _ = self._alloc(self._deals(), {"O1": [2, 3]}, {2: 2000.0, 3: 500.0}, None,
                                items={2: self.IPAD, 3: self.IPAD})
        assert writes == {2: {self.COL: 1573.6}, 3: {self.COL: 393.4}}



class TestAGroupsReportIsJoinedWhateverTheCase:
    """MOD's received-items report upper-cased Best Buy's Roadie numbers
    ("1re2a6cf3fca6133" -> "1RE2A6CF3FCA6133"), so five received MacBooks never became paid."""

    def test_an_upper_cased_report_number_lands_on_the_ledgers_row(self):
        from sync_tracking import _in_ledger_spelling
        records = [PayoutRecord("1RE2A6CF3FCA6133", payout_amount=1202.0, payout_date="2026-09-22", status="paid")]
        fixed = _in_ledger_spelling(records, {"1re2a6cf3fca6133": [2]})
        writes = allocate_payouts(fixed, {"1re2a6cf3fca6133": [2]}, {2: 1199.0})
        assert writes[2][PAYOUT_AMOUNT_COL] == 1202.0 and writes[2][PAYOUT_DATE_COL] == "2026-09-22"

    def test_an_exact_or_ambiguous_spelling_is_left_alone(self):
        from sync_tracking import _in_ledger_spelling
        exact = [PayoutRecord("TBA1", payout_amount=1.0)]
        assert _in_ledger_spelling(exact, {"TBA1": [2]})[0].tracking_number == "TBA1"
        ambiguous = [PayoutRecord("ABC", payout_amount=1.0)]
        assert _in_ledger_spelling(ambiguous, {"abc": [2], "Abc": [3]})[0].tracking_number == "ABC"
