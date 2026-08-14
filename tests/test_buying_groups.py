"""Offline tests for the buying-group layer. No credentials, no network, no real requests.

Every test that touches HTTP replaces `requests.request` at the transport boundary in
buying_groups.base, which is the only place either adapter reaches the network — so there is no
branch where a real call could leak out, the same structural approach conftest.py takes to alerts.

The tests are organised around the things that would cost real money or real reimbursements if they
broke: the exact JSON bodies each provider receives, split handling (create vs update, and the order
they go in), MOD's per-package grouping, and the pro-rata payout split.
"""

import json

import pytest

from buying_groups.base import (
    BuyingGroupError,
    DailyCallBudget,
    PayoutRecord,
    TrackingSubmission,
    normalize_group,
)
from buying_groups.bfmr import BFMRClient, _reductions_first, _tracker_object
from buying_groups.maxoutdeals import (
    MaxOutDealsClient,
    _group_by_tracking,
    _tracking_object,
    parse_received_items_csv,
)
from buying_groups.registry import resolve_group


# --- helpers ------------------------------------------------------------------------------------


def submission(**kw) -> TrackingSubmission:
    defaults = dict(
        row_number=2, order_id="111-2222222-3333333", tracking_number="TBA1",
        quantity=1, item_name="Widget", total_cost=100.0, shipment="1",
        order_date="2026-08-01", buying_group="BFMR",
    )
    return TrackingSubmission(**{**defaults, **kw})


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class Transport:
    """Records every request and returns queued responses, so a test can assert the exact body."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [])

    def __call__(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0) if self.responses else FakeResponse(payload={})

    def bodies(self):
        return [c.get("json") for c in self.calls if c.get("json") is not None]


@pytest.fixture
def transport(monkeypatch):
    t = Transport()
    monkeypatch.setattr("buying_groups.base.requests.request", t)
    return t


# Settings is a frozen dataclass, so the credentials are overridden on the CLIENT instead — which
# also guarantees these tests never pick up whatever happens to be in the developer's real .env.
@pytest.fixture
def bfmr():
    client = BFMRClient(dry_run=False)
    client.base_url = "https://api.bfmr.com"
    client.api_key, client.api_secret = "KEY", "SECRET"
    client.min_insurance_value = 0.0
    return client


@pytest.fixture
def mod():
    client = MaxOutDealsClient(dry_run=False)
    client.base_url = "https://www.maxoutdeals.com"
    client.api_key = "TOKEN"
    client.user_id, client.email = "26779", "me@example.com"
    return client


# --- group routing ------------------------------------------------------------------------------


class TestGroupRouting:
    def test_the_live_and_example_spellings_resolve_to_the_same_provider(self):
        """warehouses.json says "MOD", warehouses.example.json says "MaxOutDeals". Neither config is
        wrong, so both must route — otherwise whichever one wasn't chosen silently stops posting."""
        assert resolve_group("MOD") == "MOD"
        assert resolve_group("MaxOutDeals") == "MOD"
        assert resolve_group("max-out-deals") == "MOD"
        assert resolve_group("BFMR") == "BFMR"
        assert resolve_group("buyformeretail") == "BFMR"

    @pytest.mark.parametrize("value", ["Unclassified", "Personal", "", "   ", "SomeNewGroup"])
    def test_unroutable_groups_resolve_to_nothing_rather_than_a_default(self, value):
        """There is deliberately no fallback provider. An Unclassified row is a REAL warehouse
        nobody has configured yet; posting it to whichever group sorted first would send someone
        else's package to the wrong buying group."""
        assert resolve_group(value) == ""

    def test_normalize_group_ignores_punctuation_and_case(self):
        assert normalize_group("Max Out Deals") == normalize_group("maxoutdeals")


# --- BFMR ---------------------------------------------------------------------------------------


class TestBfmrAuth:
    def test_auth_uses_two_headers_not_a_bearer_token(self, bfmr, transport):
        """The placeholder this replaced sent `Authorization: Bearer <key>`, which BFMR rejects."""
        bfmr.get_json("/api/v2/my-tracker")
        headers = transport.calls[0]["headers"]
        assert headers["API-KEY"] == "KEY"
        assert headers["API-SECRET"] == "SECRET"
        assert "Authorization" not in headers

    def test_missing_credentials_raise_before_any_request(self, bfmr, transport, monkeypatch):
        monkeypatch.setattr(bfmr, "api_secret", "")
        with pytest.raises(BuyingGroupError, match="BFMR_API_SECRET"):
            bfmr.get_json("/api/v2/my-tracker")
        assert transport.calls == []


class TestBfmrSubmission:
    def _tracker(self, **extra):
        return {
            "my_tracker": [{
                "reserve_id": "R1", "purchase_id": "P1", "shipment_id": None,
                "order_no": "111-2222222-3333333", "qty": 3, **extra,
            }]
        }

    def test_a_new_tracking_number_is_sent_as_a_create(self, bfmr, transport):
        transport.responses = [
            FakeResponse(payload=self._tracker()),
            FakeResponse(payload={"reservations_response": {"valid_items": ["ok"]}}),
            tracker({"tracking_number": "TBA1", "shipment_id": "S9"}),  # the confirming re-read
        ]
        result = bfmr.submit_tracking([submission(quantity=3)])

        body = transport.bodies()[-1]["tracker_data"]
        assert body == [{
            "reserve_id": "R1", "purchase_id": "P1", "shipment_id": None,
            "order_no": "111-2222222-3333333", "tracking_number": "TBA1", "qty": 3,
        }]
        assert result.submitted == ["TBA1"]

    def test_a_known_tracking_number_is_sent_as_an_update_carrying_its_shipment_id(
        self, bfmr, transport
    ):
        """`shipment_id: null` means CREATE. Sending null for a number BFMR already holds would
        duplicate the shipment instead of adjusting it — the core split hazard."""
        transport.responses = [
            FakeResponse(payload={"my_tracker": [
                {"reserve_id": "R1", "purchase_id": "P1", "shipment_id": None,
                 "order_no": "111-2222222-3333333", "qty": 3},
                {"reserve_id": "R1", "purchase_id": "P1", "shipment_id": "S1",
                 "order_no": "111-2222222-3333333", "tracking_number": "TBA1", "qty": 3},
            ]}),
            FakeResponse(payload={"reservations_response": {}}),
        ]
        bfmr.submit_tracking([submission(quantity=2)])
        assert transport.bodies()[-1]["tracker_data"][0]["shipment_id"] == "S1"

    def test_an_unchanged_quantity_is_skipped_rather_than_resent(self, bfmr, transport):
        transport.responses = [FakeResponse(payload={"my_tracker": [
            {"reserve_id": "R1", "purchase_id": "P1", "shipment_id": "S1",
             "order_no": "111-2222222-3333333", "tracking_number": "TBA1", "qty": 2},
        ]})]
        result = bfmr.submit_tracking([submission(quantity=2)])
        assert result.skipped and not result.submitted
        assert len(transport.calls) == 1  # the tracker read only; nothing was posted

    def test_a_quantity_increase_is_reported_not_silently_sent(self, bfmr, transport):
        """BFMR only ever allows a quantity to be REDUCED. An increase means our ledger and their
        record genuinely disagree, which a human has to resolve — sending it would just 400."""
        transport.responses = [FakeResponse(payload={"my_tracker": [
            {"reserve_id": "R1", "purchase_id": "P1", "shipment_id": "S1",
             "order_no": "111-2222222-3333333", "tracking_number": "TBA1", "qty": 1},
        ]})]
        result = bfmr.submit_tracking([submission(quantity=5)])
        assert not result.submitted
        assert "only allows a quantity to be REDUCED" in result.failed[0][1]

    def test_an_order_bfmr_has_no_purchase_for_is_reported_not_invented(self, bfmr, transport):
        """v1 deliberately never creates a purchase: choosing the wrong reserve_id books the wrong
        deal, and since qty only reduces, that is not cheaply undone.

        The message says what a missing purchase IMPLIES, too. A reservation only stays valid if its
        order number goes in right after ordering, so a shipped package without one means the
        reservation lapsed — not that this tool skipped a step."""
        transport.responses = [FakeResponse(payload={"my_tracker": [
            {"reserve_id": "R9", "purchase_id": None, "order_no": "", "qty": 1},
        ]})]
        result = bfmr.submit_tracking([submission()])
        assert not result.submitted
        message = result.needs_manual[0][1]
        assert "no purchase recorded" in message and "LAPSED" in message

    def test_a_cancelled_purchase_is_not_misreported_as_a_combined_package(self, bfmr, transport):
        """BFMR halts on an inactive purchase, so submitting against a CANCELLED one fails in exactly
        the same accepted-but-absent way a Best Buy combined carton does. Diagnosing it as the carton
        case would send the user off appending letters to a reservation that no longer exists."""
        transport.responses = [FakeResponse(payload={"my_tracker": [
            {"reserve_id": "R1", "purchase_id": "P1", "order_id": "O1", "qty": 1,
             "status": "cancelled", "deal_title": "PS5"},
        ]})]
        result = bfmr.submit_tracking([submission(order_id="O1")])

        message = result.needs_manual[0][1]
        assert "CANCELLED the purchase" in message
        assert "NOT the Best Buy combined-package case" in message
        assert len(transport.calls) == 1, "nothing was posted against a dead purchase"

    def test_an_active_purchase_wins_over_a_cancelled_one_for_the_same_order(self, bfmr, transport):
        """Re-ordering the same deal leaves both on the tracker; the live one must be chosen."""
        transport.responses = [
            FakeResponse(payload={"my_tracker": [
                {"reserve_id": "R0", "purchase_id": "DEAD", "order_id": "O1", "qty": 1,
                 "status": "cancelled", "deal_title": "PS5"},
                {"reserve_id": "R1", "purchase_id": "LIVE", "order_id": "O1", "qty": 1,
                 "status": "shipped", "deal_title": "PS5"},
            ]}),
            FakeResponse(payload={"reservations_response": {}}),
            tracker({"tracking_number": "TBA1", "shipment_id": "S9"}),
        ]
        bfmr.submit_tracking([submission(order_id="O1")])
        assert transport.bodies()[-1]["tracker_data"][0]["purchase_id"] == "LIVE"

    def test_a_rejected_object_is_attributed_to_its_own_row(self, bfmr, transport):
        transport.responses = [
            FakeResponse(payload={"my_tracker": [
                {"reserve_id": "R1", "purchase_id": "P1", "order_no": "O1", "qty": 5},
            ]}),
            FakeResponse(payload={"reservations_response": {
                "invalid_items": [{"tracking_number": "BAD", "error": "nope"}]
            }}),
            tracker({"tracking_number": "GOOD", "shipment_id": "S9"}),
        ]
        result = bfmr.submit_tracking([
            submission(order_id="O1", tracking_number="GOOD"),
            submission(order_id="O1", tracking_number="BAD"),
        ])
        assert result.submitted == ["GOOD"]
        assert result.failed[0][0] == "BAD"


class TestBfmrSplitOrdering:
    def test_reductions_are_sent_before_new_shipments(self):
        """When a qty-3 box splits into 2+1, applying the create first would momentarily claim
        3 + 1 = 4 units against a 3-unit purchase."""
        objects = [
            {"tracking_number": "NEW", "_is_reduction": False},
            {"tracking_number": "OLD", "_is_reduction": True},
        ]
        assert [o["tracking_number"] for o in _reductions_first(objects)] == ["OLD", "NEW"]

    def test_the_private_ordering_marker_never_reaches_the_api(self):
        sent = _reductions_first([{"tracking_number": "X", "_is_reduction": True}])
        assert sent == [{"tracking_number": "X"}]

    def test_ordering_is_stable_within_a_category(self):
        objects = [{"tracking_number": n, "_is_reduction": False} for n in ("A", "B", "C")]
        assert [o["tracking_number"] for o in _reductions_first(objects)] == ["A", "B", "C"]

    def test_a_shrinking_shipment_is_marked_as_a_reduction(self):
        obj = _tracker_object(
            submission(quantity=2), {"reserve_id": "R", "purchase_id": "P"},
            {"shipment_id": "S", "qty": 3},
        )
        assert obj["_is_reduction"] is True

    def test_batches_respect_the_documented_500_object_ceiling(self, bfmr, transport):
        rows = [submission(tracking_number=f"T{i}", row_number=i + 2) for i in range(501)]
        landed = tracker(*[{"tracking_number": f"T{i}", "shipment_id": f"S{i}"} for i in range(501)])
        transport.responses = [FakeResponse(payload={"my_tracker": [
            {"reserve_id": "R1", "purchase_id": "P1",
             "order_no": "111-2222222-3333333", "qty": 9}
        ]})] + [FakeResponse(payload={"reservations_response": {}}), landed,
                FakeResponse(payload={"reservations_response": {}}), landed]

        bfmr.submit_tracking(rows)
        posted = transport.bodies()
        assert [len(b["tracker_data"]) for b in posted] == [500, 1]


def tracker(*rows):
    """A my-tracker response. Insurance state and tracking numbers both come from here now."""
    return FakeResponse(payload={"my_tracker": list(rows)})


def insured(*shipments, last_page=1):
    """A `GET /api/v2/insurance/shipments` page — the authoritative record of what is insured."""
    return FakeResponse(payload={
        "message": "List of insured shipments",
        "insurance": {
            "shipments": [
                {"tracking_number": t, "cost_of_insurance": c, "package_value": v}
                for t, c, v in shipments
            ],
            "paging": {"current_page": 1, "last_page": last_page, "total": len(shipments)},
        },
    })


class TestBfmrInsurance:
    """Whether a package is insured is decided by `GET /api/v2/insurance/shipments` — the real record.

    That route 404'd when this adapter was written, so the guard used My Tracker's `insurance_status`
    instead. Probing it live once it returned (2026-08-13) showed the substitute had been wrong the
    whole time:

        insured  insurance_status  BFMR status        rows
        yes      not_eligible      paid / processed     58
        yes      insured           shipped               3
        no       not_eligible      paid / returned       4

    It is a LIFECYCLE field, reading `not_eligible` for all 30 genuinely insured shipments. The guard
    survived only because `insured` and `not_eligible` both happened to skip — and because it could
    not tell "already insured" from "not insurable", `file_insurance` had never once filed anything.
    """

    def test_an_already_insured_shipment_is_never_filed_twice(self, bfmr, transport):
        transport.responses = [tracker({"tracking_number": "TBA1"}), insured(("TBA1", 8.18, 1796))]
        result = bfmr.file_insurance([submission(tracking_number="TBA1")])
        assert result.skipped[0][1] == "already insured"
        assert [c["method"] for c in transport.calls] == ["GET", "GET"]  # no filing attempted

    def test_not_eligible_no_longer_blocks_an_open_shipment(self, bfmr, transport):
        """THE CHANGE THAT UNBLOCKS FILING. `not_eligible` marks a TERMINAL purchase, not an
        uninsurable one, so an open shipment carrying it must still be filed."""
        transport.responses = [
            tracker({"tracking_number": "TBA1", "insurance_status": "not_eligible",
                     "status": "shipped"}),
            insured(),
            FakeResponse(payload={"message": "filed"}),
            insured(("TBA1", 4.0, 900)),
        ]
        assert bfmr.file_insurance([submission(tracking_number="TBA1")]).submitted == ["TBA1"]

    def test_a_terminal_purchase_is_skipped(self, bfmr, transport):
        """Decided on BFMR's own `status`, which is what `not_eligible` was really tracking."""
        transport.responses = [
            tracker({"tracking_number": "TBA1", "status": "paid"}),
            insured(),
        ]
        result = bfmr.file_insurance([submission(tracking_number="TBA1")])
        assert "terminal" in result.skipped[0][1]
        assert [c["method"] for c in transport.calls] == ["GET", "GET"]

    def test_pagination_is_followed_or_older_shipments_read_as_uninsured(self, bfmr, transport):
        """A single-page read would report the older half of the account as uninsured — which on the
        filing path means paying a second premium for each one."""
        transport.responses = [
            tracker({"tracking_number": "OLD"}),
            insured(("NEW", 8.18, 1796), last_page=2),
            insured(("OLD", 2.0, 219), last_page=2),
        ]
        result = bfmr.file_insurance([submission(tracking_number="OLD")])
        assert result.skipped[0][1] == "already insured"

    def test_an_unreadable_insured_list_files_NOTHING(self, bfmr, transport):
        """FAIL SAFE. These routes have vanished before, and the fallback provably cannot tell
        uninsured from ineligible — so filing on it would risk a duplicate premium."""
        transport.responses = [
            tracker({"tracking_number": "TBA1"}),
            FakeResponse(status_code=500, text="boom"),
        ]
        result = bfmr.file_insurance([submission(tracking_number="TBA1")])
        assert result.submitted == []
        assert "not filing on a guess" in result.skipped[0][1]
        assert "POST" not in [c["method"] for c in transport.calls]

    def test_a_filing_that_never_lands_is_reported_not_counted(self, bfmr, transport):
        """BFMR has already once returned success for a submission it dropped. Here money moved, so
        success is confirmed by re-reading — and it is NOT retried, since a retry is the one thing
        that could charge twice."""
        transport.responses = [
            tracker({"tracking_number": "TBA1"}),
            insured(),
            FakeResponse(payload={"message": "filed"}),
            insured(),  # still absent afterwards
        ]
        result = bfmr.file_insurance([submission(tracking_number="TBA1")])
        assert result.submitted == []
        assert "MAY OR MAY NOT BE COVERED" in result.needs_manual[0][1]

    def test_package_value_is_never_sent(self, bfmr, transport):
        """BFMR derives the value from the shipment items it already holds. Declaring our own
        number risks over-declaring and paying a bigger premium than the box warrants."""
        transport.responses = [
            tracker(), insured(), FakeResponse(payload={"message": "filed"}),
            insured(("TBA1", 6.0, 1299)),
        ]
        bfmr.file_insurance([submission(total_cost=1299.0)])
        posted = [c for c in transport.calls if c["method"] == "POST"]
        assert posted[0]["data"] == {"tracking_number": "TBA1"}

    def test_the_value_threshold_excludes_cheap_shipments(self, bfmr, transport, monkeypatch):
        monkeypatch.setattr(bfmr, "min_insurance_value", 500.0)
        transport.responses = [tracker(), insured()]
        result = bfmr.file_insurance([submission(total_cost=100.0)])
        assert "threshold" in result.skipped[0][1]
        assert "POST" not in [c["method"] for c in transport.calls]

    def test_the_default_threshold_of_zero_insures_everything(self, bfmr, transport):
        transport.responses = [
            tracker(), insured(), FakeResponse(payload={"message": "filed"}),
            insured(("TBA1", 2.0, 0.01)),
        ]
        assert bfmr.file_insurance([submission(total_cost=0.01)]).submitted == ["TBA1"]

    def test_a_dry_run_files_nothing(self, bfmr, transport):
        bfmr.dry_run = True
        transport.responses = [tracker(), insured()]
        result = bfmr.file_insurance([submission()])
        assert result.skipped[0][1] == "dry run"
        assert "POST" not in [c["method"] for c in transport.calls]


class TestInsurancePremiumSource:
    """The premium now comes from BFMR's insurance record, with the fee row as the fallback."""

    def test_the_authoritative_premium_wins_over_the_fee_row(self, bfmr, transport):
        """Both sources agreed on all 30 real filings, so this is about TIMING: a package insured
        during this run has an insurance record immediately, while its fee row appears later."""
        transport.responses = [
            tracker({"tracking_number": "TBA1", "total_payout": "-7.40"}),
            insured(("TBA1", 7.4, 1599.96)),
        ]
        records = bfmr.fetch_payouts(["TBA1"])
        premiums = [r.insurance for r in records if r.insurance is not None]
        assert premiums == [7.4], "one record, not two — the fee row must not be booked as well"

    def test_the_fee_row_still_covers_a_package_the_list_does_not_mention(self, bfmr, transport):
        transport.responses = [
            tracker({"tracking_number": "TBA1", "total_payout": "-7.40"}),
            insured(),
        ]
        assert [r.insurance for r in bfmr.fetch_payouts(["TBA1"])] == [7.4]

    def test_an_unreadable_list_leaves_the_fee_row_premium_alone(self, bfmr, transport):
        """A read failure here is not fatal — unlike on the filing path — because the fee rows
        already produced the right answer."""
        transport.responses = [
            tracker({"tracking_number": "TBA1", "total_payout": "-7.40"}),
            FakeResponse(status_code=500, text="boom"),
        ]
        assert [r.insurance for r in bfmr.fetch_payouts(["TBA1"])] == [7.4]


class TestInsuredPresenceIsNotValue:
    """Presence and value are different questions, and conflating them costs a duplicate premium.

    `insured_shipments()` maps a tracking number to its PREMIUM, and an unparseable premium is
    `None`. A `lookup(...) is not None` test would then read a genuinely insured package as
    uninsured and file it a SECOND TIME — a real charge. `_has_any_spelling` tests membership.
    """

    def test_an_insured_package_with_an_unreadable_premium_is_still_not_re_filed(
        self, bfmr, transport
    ):
        transport.responses = [
            tracker({"tracking_number": "TBA1"}),
            FakeResponse(payload={"insurance": {"shipments": [
                {"tracking_number": "TBA1", "cost_of_insurance": "n/a"}   # unparseable
            ], "paging": {"last_page": 1}}}),
        ]
        result = bfmr.file_insurance([submission(tracking_number="TBA1")])
        assert result.skipped[0][1] == "already insured"
        assert "POST" not in [c["method"] for c in transport.calls]


class TestBfmrReads:
    def test_already_submitted_reads_the_tracker_once_not_a_call_per_number(self, bfmr, transport):
        """The spec documents no tracking number on my-tracker; the live API returns one on 65 of 76
        rows. One read beats N `shipments/status` calls, and it's the same payload the push needs."""
        transport.responses = [tracker(
            {"tracking_number": "KNOWN", "shipment_id": "S1", "order_id": "O1"}
        )]
        held = bfmr.already_submitted([
            submission(order_id="O1", tracking_number="KNOWN"),
            submission(order_id="O2", tracking_number="UNKNOWN"),
        ])
        assert held == {("O1", "KNOWN")}
        assert len(transport.calls) == 1

    def test_an_ad_hoc_shipment_status_treats_a_404_as_not_known(self, bfmr, transport):
        transport.responses = [FakeResponse(status_code=404, text="no such shipment")]
        assert bfmr.shipment_status("NOPE") == {}

    def test_shipment_status_reads_the_object_the_live_api_returns(self, bfmr, transport):
        """The spec draws `tracker_data` as an ARRAY; the live API returns an OBJECT."""
        transport.responses = [FakeResponse(payload={
            "tracker_data": {"tracking_number": "T1", "status": "pending"}
        })]
        assert bfmr.shipment_status("T1")["status"] == "pending"

    def test_a_deadline_warning_needs_both_urgency_and_a_missing_tracking_number(
        self, bfmr, transport
    ):
        """BFMR cancels a purchase whose tracking arrives late, and the box that ships late is the
        second half of a split — so this is a money-protecting read, not a nicety."""
        transport.responses = [FakeResponse(payload={"my_tracker": [
            {"purchase_id": "P1", "order_deadline": 3600},                      # urgent, untracked
            {"purchase_id": "P2", "order_deadline": 3600, "tracking_number": "T"},  # already sent
            {"purchase_id": "P3", "order_deadline": 999999},                    # not urgent
            {"purchase_id": "P4", "order_deadline": -1},                        # no deadline
        ]})]
        assert [w["purchase_id"] for w in bfmr.deadline_warnings()] == ["P1"]


# --- MaxOutDeals --------------------------------------------------------------------------------


class TestModSubmission:
    def test_rows_sharing_a_tracking_number_become_one_object_with_a_summed_amount(self):
        """MOD takes one object per PACKAGE, not per line item. Sending two objects with the same
        tracking number would have the second ignored as a duplicate, under-reporting the box."""
        rows = [
            submission(tracking_number="1Z9", item_name="Switch", quantity=2, total_cost=600.0),
            submission(tracking_number="1Z9", item_name="Case", quantity=1, total_cost=25.0),
        ]
        grouped = _group_by_tracking(rows)
        assert list(grouped) == ["1Z9"]
        obj = _tracking_object("1Z9", grouped["1Z9"])
        assert obj["amount"] == 625.0
        assert obj["notes"] == "2-Switch, 1-Case"

    def test_the_request_body_matches_mods_documented_shape(self, mod, transport):
        transport.responses = [FakeResponse(payload={"success": True, "affected": 1})]
        mod.submit_tracking([submission(tracking_number="1Z9", order_id="O9", total_cost=66.77)])

        body = transport.bodies()[0]
        assert body["user"] == 26779 and body["email"] == "me@example.com"
        assert body["trackings"] == [
            {"tracking": "1Z9", "order": "O9", "amount": 66.77, "notes": "1-Widget"}
        ]

    def test_auth_is_a_bearer_token(self, mod, transport):
        transport.responses = [FakeResponse(payload={"success": True})]
        mod.submit_tracking([submission()])
        assert transport.calls[0]["headers"]["Authorization"] == "Bearer TOKEN"

    def test_everything_goes_in_one_call_because_of_the_daily_limit(self, mod, transport):
        transport.responses = [FakeResponse(payload={"success": True, "affected": 50})]
        mod.submit_tracking([submission(tracking_number=f"T{i}") for i in range(50)])
        assert len(transport.calls) == 1

    def test_already_submitted_spends_no_calls_because_mod_dedupes_server_side(self, mod, transport):
        assert mod.already_submitted(["1Z9"]) == set()
        assert transport.calls == []

    def test_a_failure_response_raises_rather_than_reporting_success(self, mod, transport):
        transport.responses = [FakeResponse(payload={"success": False, "response": "bad token"})]
        with pytest.raises(BuyingGroupError, match="bad token"):
            mod.submit_tracking([submission()])

    def test_per_object_errors_are_attributed_to_their_tracking_number(self, mod, transport):
        transport.responses = [FakeResponse(payload={
            "success": True, "affected": 1,
            "errors": [{"tracking": "BAD", "message": "invalid"}],
        })]
        result = mod.submit_tracking([
            submission(tracking_number="GOOD"), submission(tracking_number="BAD"),
        ])
        assert result.submitted == ["GOOD"] and result.failed[0][0] == "BAD"

    def test_missing_credentials_name_the_ip_allowlist_too(self, mod, monkeypatch):
        """A correct token still fails if the machine's IP isn't registered, and that failure looks
        exactly like a bad key — so the error says so up front."""
        monkeypatch.setattr(mod, "api_key", "")
        with pytest.raises(BuyingGroupError, match="firewall"):
            mod.submit_tracking([submission()])


class TestModCsvParsing:
    """Built from the REAL header captured by scripts/bg_probe.py on 2026-08-12, which differs from
    MOD's published one (VOID/VERIFIED swapped, USER dropped, COMMITTED + TOTAL PRICE added). Using
    the documented header here would have tested a shape that does not exist."""

    HEADER = ('"VOID","VERIFIED","COMMITTED","ID","RECEIPT ID","ITEM","QTY","PRICE","TAX",'
              '"COMMISSION","TOTAL PRICE","EXT TOTAL","CREATED DATE","WAREHOUSE","STATUS",'
              '"DELIVERY","TRACKING NUMBERS"')

    def _csv(self, *rows):
        return "\n".join([self.HEADER, *rows])

    def _row(self, void="0", qty="1", price="899", tax="0", commission="6",
             ext_total="905", created="2026-08-12 15:16:09", tracking='="529900000008"'):
        # The tracking cell is CSV-quoted with its inner quotes DOUBLED, exactly as the live capture
        # has it (`"=""529900000008"""`). Escaping it by hand here would produce malformed CSV that
        # the parser happens to tolerate, which would make these tests agree with a shape MOD never
        # actually sends.
        cell = '"' + tracking.replace('"', '""') + '"'
        return (f'{void},1,1,58084,636981,"PS5 PRO 2TB",{qty},{price},{tax},{commission},905,'
                f'{ext_total},"{created}",DELAWARE,RECEIVING,"SHIP DIRECT",{cell}')

    def test_the_payout_is_the_full_reimbursement_not_the_commission(self):
        """EXT TOTAL = (PRICE + TAX + COMMISSION) x QTY, verified on 49/49 real receipts. The profit
        formula SUBTRACTS Total Cost, so feeding it the $6 commission instead of the $905
        reimbursement would book a $893 loss on a profitable order."""
        records = parse_received_items_csv(self._csv(self._row()))
        assert records[0].payout_amount == 905.0

    def test_the_excel_escaping_on_the_join_key_is_stripped(self):
        """MOD ships tracking numbers as `="529900000008"` — a spreadsheet wrapper that stops Excel
        rendering them in scientific notation. It is undocumented, and left in place the number
        matches no ledger row, so EVERY payout would silently fail to land while looking exactly
        like "MOD hasn't paid anything yet"."""
        records = parse_received_items_csv(self._csv(self._row()))
        assert records[0].tracking_number == "529900000008"

    def test_a_plain_unescaped_tracking_number_still_works(self):
        records = parse_received_items_csv(self._csv(self._row(tracking="1Z9")))
        assert records[0].tracking_number == "1Z9"

    def test_the_receipt_date_becomes_plain_iso_text(self):
        records = parse_received_items_csv(self._csv(self._row()))
        assert records[0].payout_date == "2026-08-12"  # time dropped; the column is ISO text

    def test_mod_insurance_is_recorded_as_zero_not_blank(self):
        """A blank would leave the Insurance cell untouched forever under _merge_row's
        blank-never-overwrites rule; 0 states the confirmed fact that MOD never charges any."""
        records = parse_received_items_csv(self._csv(self._row()))
        assert records[0].insurance == 0.0

    def test_a_multi_quantity_line_pays_out_the_extended_total(self):
        records = parse_received_items_csv(
            self._csv(self._row(qty="3", price="309", commission="1", ext_total="930"))
        )
        assert records[0].payout_amount == 930.0

    def test_several_rows_for_one_package_sum_into_a_single_payout(self):
        records = parse_received_items_csv(self._csv(
            self._row(ext_total="100"), self._row(ext_total="50"),
        ))
        assert len(records) == 1 and records[0].payout_amount == 150.0

    def test_one_cell_holding_several_tracking_numbers_is_split(self):
        records = parse_received_items_csv(
            self._csv(self._row(tracking='="1Z9", ="1Z8"'))
        )
        assert {r.tracking_number for r in records} == {"1Z9", "1Z8"}

    def test_voided_receipts_are_excluded(self):
        assert parse_received_items_csv(self._csv(self._row(void="1"))) == []

    def test_a_json_error_body_raises_instead_of_reading_as_no_payouts(self):
        """MOD answers CSV on success and JSON on failure. Returning [] here would look exactly like
        "nothing received yet" and would stall every payout silently, forever."""
        with pytest.raises(BuyingGroupError, match="error instead of a CSV"):
            parse_received_items_csv('{"success":false,"response":"rate limit exceeded"}')

    def test_a_csv_without_the_join_column_raises(self):
        with pytest.raises(BuyingGroupError, match="TRACKING NUMBERS"):
            parse_received_items_csv('"A","B"\n"1","2"')

    def test_an_empty_body_is_simply_no_payouts(self):
        assert parse_received_items_csv("   ") == []


class TestDailyCallBudget:
    def test_it_refuses_rather_than_burning_the_daily_allowance(self):
        """MOD allows 10 receipt searches a day. A retry loop would spend them silently and the next
        SCHEDULED run would then fail to submit anything, with nothing pointing at the cause."""
        budget = DailyCallBudget(2, "MOD receipts")
        budget.spend()
        budget.spend()
        with pytest.raises(BuyingGroupError, match="call budget of 2"):
            budget.spend()

    def test_the_configured_limits_match_mods_published_ones(self, mod):
        assert mod.push_budget.limit == 30
        assert mod.pull_budget.limit == 10


# --- shared transport ---------------------------------------------------------------------------


class TestTransport:
    def test_a_dry_run_blocks_mutating_calls_but_still_allows_reads(self, bfmr, transport):
        """The dry run's whole job is to show what it WOULD change, which it can only do by reading
        the group's real current state first."""
        bfmr.dry_run = True
        assert bfmr.request("POST", "/x", mutating=True, json_body={"a": 1}) is None
        bfmr.request("GET", "/y", mutating=False)
        assert [c["method"] for c in transport.calls] == ["GET"]

    def test_a_429_is_retried_and_then_succeeds(self, bfmr, transport, monkeypatch):
        monkeypatch.setattr("buying_groups.base.time.sleep", lambda _s: None)
        transport.responses = [
            FakeResponse(status_code=429, text="slow down"),
            FakeResponse(payload={"my_tracker": []}),
        ]
        assert bfmr.get_json("/api/v2/my-tracker") == {"my_tracker": []}
        assert len(transport.calls) == 2

    def test_a_4xx_that_is_not_429_raises_immediately(self, bfmr, transport):
        transport.responses = [FakeResponse(status_code=500, text="boom")]
        with pytest.raises(BuyingGroupError, match="500"):
            bfmr.get_json("/api/v2/my-tracker")
        assert len(transport.calls) == 1

    def test_a_network_error_becomes_a_buying_group_error(self, bfmr, monkeypatch):
        import requests

        def explode(*_a, **_k):
            raise requests.RequestException("dns")

        monkeypatch.setattr("buying_groups.base.requests.request", explode)
        with pytest.raises(BuyingGroupError, match="dns"):
            bfmr.get_json("/api/v2/my-tracker")


class TestBestBuyDuplicateTrackingSuffix:
    """Best Buy reuses a tracking number across orders; BFMR rejects the duplicate and tells you to
    append B/C/D until it takes. So BFMR stores `529900000009B` where the ledger — which only ever
    sees what the retailer printed — stores `529900000009`.

    https://support.bfmr.com/hc/en-us/articles/50968170907547

    Compared literally, the package looks ABSENT from BFMR at all four join points, each failing
    silently and expensively. These tests pin all four.
    """

    LEDGER = "529900000009"
    BFMR = "529900000009B"

    def _row(self, **extra):
        return {
            "tracking_number": self.BFMR, "shipment_id": "S1", "purchase_id": "P1",
            "reserve_id": "R1", "order_id": "O1", "qty": 4, **extra,
        }

    def test_a_suffixed_record_counts_as_already_submitted(self, bfmr, transport):
        """Otherwise we resubmit a package BFMR already has, and BFMR rejects it as a duplicate."""
        transport.responses = [tracker(self._row())]
        held = bfmr.already_submitted([submission(order_id="O1", tracking_number=self.LEDGER)])
        assert held == {("O1", self.LEDGER)}

    def test_a_suffixed_shipment_is_updated_not_duplicated(self, bfmr, transport):
        """`shipment_id: null` means CREATE. Missing the suffix sends a create for a shipment that
        already exists."""
        transport.responses = [
            tracker(self._row()),
            FakeResponse(payload={"reservations_response": {}}),
            tracker(self._row()),
        ]
        bfmr.submit_tracking([submission(order_id="O1", tracking_number=self.LEDGER, quantity=2)])
        sent = transport.bodies()[-1]["tracker_data"][0]
        assert sent["shipment_id"] == "S1"
        assert sent["tracking_number"] == self.BFMR, "must echo back BFMR's own spelling"

    def test_a_suffixed_payout_lands_on_the_ledgers_tracking_number(self, bfmr, transport):
        """The record must come back under OUR spelling or it joins to no row and the money is
        never recorded."""
        transport.responses = [tracker(self._row(
            status="paid", amount_paid="1,584.00", date_paid="08/11/2026",
        ))]
        record = bfmr.fetch_payouts([self.LEDGER])[0]
        assert record.tracking_number == self.LEDGER
        assert record.payout_amount == 1584.0

    def test_insurance_is_filed_against_bfmrs_spelling_not_ours(self, bfmr, transport):
        """A package BFMR holds as "…B" must be FILED as "…B". Sending the ledger's bare number posts
        against a shipment they have no record of — a 2xx that matches nothing, reported as success,
        leaving the package uninsured. Seen live on 529900000009."""
        transport.responses = [
            tracker(self._row()),                             # known to BFMR, not yet insured
            insured(),                                        # BFMR's insurance list: empty
            FakeResponse(payload={"message": "filed"}),
            insured((self.BFMR, 7.4, 1599.96)),               # ...and it landed
        ]
        result = bfmr.file_insurance([submission(tracking_number=self.LEDGER)])
        posted = [c for c in transport.calls if c["method"] == "POST"]
        assert posted[0]["data"] == {"tracking_number": self.BFMR}
        assert result.submitted == [self.LEDGER], "reported under the ledger's spelling"

    def test_a_package_bfmr_has_never_seen_is_filed_under_our_own_number(self, bfmr, transport):
        transport.responses = [
            tracker(), insured(), FakeResponse(payload={"message": "filed"}),
            insured(("BRAND-NEW", 2.0, 100)),
        ]
        bfmr.file_insurance([submission(tracking_number="BRAND-NEW")])
        posted = [c for c in transport.calls if c["method"] == "POST"]
        assert posted[0]["data"] == {"tracking_number": "BRAND-NEW"}

    def test_a_void_also_targets_bfmrs_spelling(self, bfmr, transport):
        transport.responses = [
            tracker(self._row(insurance_status="insured")),
            FakeResponse(payload={"message": "voided"}),
        ]
        bfmr.void_insurance([self.LEDGER])
        assert transport.calls[-1]["data"] == {"tracking_number": self.BFMR}

    def test_an_already_insured_suffixed_package_is_not_insured_again(self, bfmr, transport):
        """This one costs money: a second filing on an already-insured package is a real premium."""
        transport.responses = [
            tracker(self._row()),
            insured((self.BFMR, 7.4, 1599.96)),   # insured under BFMR's OWN spelling
        ]
        result = bfmr.file_insurance([submission(tracking_number=self.LEDGER)])
        assert result.skipped[0][1] == "already insured"
        assert "POST" not in [c["method"] for c in transport.calls]  # nothing was filed

    def test_a_second_order_in_the_same_box_is_not_marked_done_by_the_first(self, bfmr, transport):
        """THE COMBINED-BOX CASE, and the reason submitted-state is keyed on (order, tracking).

        Best Buy ships two ORDERS in one carton under one tracking number. Each still needs its own
        BFMR shipment (the second carrying a B/C/D suffix). Keyed on the tracking number alone,
        order A's submission marked order B as already done — so order B was never submitted, never
        paid, and its checkbox ticked to say otherwise. Silent, and it costs a whole reimbursement.
        """
        transport.responses = [tracker(
            {"tracking_number": "T123", "shipment_id": "S1", "order_id": "ORDER-A",
             "deal_title": "Laptop", "total_payout": "100.00"},
        )]
        held = bfmr.already_submitted([
            submission(order_id="ORDER-A", tracking_number="T123"),
            submission(order_id="ORDER-B", tracking_number="T123"),
        ])
        assert ("ORDER-A", "T123") in held
        assert ("ORDER-B", "T123") not in held

    def test_the_second_order_creates_its_own_shipment_rather_than_rewriting_the_first(
        self, bfmr, transport
    ):
        """The mirror hazard: keyed on tracking alone, order B would find order A's record and be
        sent as an UPDATE carrying A's `shipment_id` — overwriting A's shipment with B's details."""
        held = tracker(
            {"reserve_id": "RA", "purchase_id": "PA", "shipment_id": "SA", "order_id": "ORDER-A",
             "tracking_number": "T123", "qty": 1, "deal_title": "Laptop"},
            {"reserve_id": "RB", "purchase_id": "PB", "order_id": "ORDER-B", "qty": 1},
        )
        transport.responses = [held, FakeResponse(payload={"reservations_response": {}}), held]
        bfmr.submit_tracking([submission(order_id="ORDER-B", tracking_number="T123")])

        sent = transport.bodies()[-1]["tracker_data"][0]
        assert sent["purchase_id"] == "PB", "must target order B's own purchase"
        assert sent["shipment_id"] is None, "a CREATE, not an update of order A's shipment"

    def test_we_extend_our_number_rather_than_stripping_theirs(self):
        """One-directional on purpose: stripping a trailing character off BFMR's spelling could
        merge two genuinely different packages onto one row."""
        from buying_groups.bfmr import bfmr_spellings

        spellings = bfmr_spellings("1Z999AAB")
        assert spellings[0] == "1Z999AAB", "the bare number must come first"
        assert spellings[1:4] == ["1Z999AABA", "1Z999AABB", "1Z999AABC"]

    def test_the_whole_alphabet_is_matched_not_just_the_documented_three(self, bfmr, transport):
        """BFMR's article names B/C/D as an example, not a limit — a carton can hold more orders than
        that. The trade is asymmetric: recognising a suffix we didn't need costs nothing, while
        missing one loses the package at all four join points, silently."""
        from buying_groups.bfmr import BFMR_DUPLICATE_SUFFIXES

        assert len(BFMR_DUPLICATE_SUFFIXES) == 26
        transport.responses = [tracker(
            {"tracking_number": "T123Q", "shipment_id": "S1", "order_id": "O1",
             "deal_title": "Laptop", "total_payout": "10.00"},
        )]
        held = bfmr.already_submitted([submission(order_id="O1", tracking_number="T123")])
        assert held == {("O1", "T123")}

    def test_a_ledger_row_recorded_with_the_suffix_keeps_its_own_identity(self, bfmr, transport):
        """If someone records the suffixed number on the sheet, both rows exist as distinct tracking
        numbers. Exact matches are claimed FIRST so the bare number's variants can't swallow the
        suffixed row and attribute its payout to the wrong order."""
        transport.responses = [tracker(
            {"tracking_number": "T123", "shipment_id": "S1", "order_id": "ORDER-A",
             "deal_title": "A", "total_payout": "10.00"},
            {"tracking_number": "T123B", "shipment_id": "S2", "order_id": "ORDER-B",
             "deal_title": "B", "total_payout": "20.00"},
        )]
        records = bfmr.fetch_payouts(["T123", "T123B"])
        by_number = {r.tracking_number: r.order_id for r in records}
        assert by_number == {"T123": "ORDER-A", "T123B": "ORDER-B"}


class TestSilentlyDroppedSubmission:
    def test_a_submission_that_never_lands_is_reported_as_failed(self, bfmr, transport):
        """OBSERVED LIVE: a duplicate tracking number came back with an empty `invalid_items` and
        was counted as submitted, while My Tracker's row count never moved. Trusting the response
        shape means reporting success for a package that was never handed over — the quietest
        possible way to lose a reimbursement. Success is confirmed by re-reading instead."""
        transport.responses = [
            tracker({"reserve_id": "R1", "purchase_id": "P1", "order_id": "O1", "qty": 4}),
            FakeResponse(payload={"reservations_response": {}}),   # looks fine
            tracker({"reserve_id": "R1", "purchase_id": "P1", "order_id": "O1", "qty": 4}),
        ]
        result = bfmr.submit_tracking([submission(order_id="O1", tracking_number="529900000009")])
        assert result.submitted == []
        assert result.failed == [], "not a transient failure — no retry of ours can clear it"
        assert "NO spelling" in result.needs_manual[0][1]

    def test_the_alert_leads_with_the_check_that_costs_nothing(self, bfmr, transport):
        """Since BFMR automated the suffixing (2026-08-13), landing under no spelling at all is
        AMBIGUOUS: either their Best Buy check is still running, or the submission was dropped. Those
        want opposite responses, so the message must put the free check first and only then describe
        the manual repair — the old text prescribed three steps BFMR now performs itself."""
        transport.responses = [
            tracker({"reserve_id": "R1", "purchase_id": "P1", "order_id": "O1", "qty": 4}),
            FakeResponse(payload={"reservations_response": {}}),
            tracker({"reserve_id": "R1", "purchase_id": "P1", "order_id": "O1", "qty": 4}),
        ]
        result = bfmr.submit_tracking([submission(order_id="O1", tracking_number="529900000009")])
        message = result.needs_manual[0][1]

        assert "LOOK IN MY TRACKER FIRST" in message
        assert "RESOLVES ITSELF" in message
        assert "IF IT IS STILL ABSENT ON THE NEXT RUN" in message
        assert "support.bfmr.com/hc/en-us/articles/50968170907547" in message
        assert "NOTHING TO EDIT ON THE SHEET" in message
        # The instructions BFMR took over. Telling someone to do these by hand is now busy-work that
        # also contradicts what their dashboard is already doing.
        assert "FILE THE INSURANCE BY HAND" not in message
        assert "ADD THE TRACKING BY HAND to the purchase" not in message

    def test_a_manual_fix_is_picked_up_on_the_next_run(self, bfmr, transport):
        """THE ROUND TRIP. Once the package exists in My Tracker under any letter, the next run must
        recognise it, stop trying to submit it, leave its insurance alone, and carry the payout back
        — with no edit to the sheet. Without this the alert would repeat every six hours forever."""
        after_manual_fix = tracker({
            "reserve_id": "R1", "purchase_id": "P1", "shipment_id": "S1", "order_id": "O1",
            "tracking_number": "529900000009B", "qty": 4, "deal_title": "Samsung SSD",
            "status": "paid", "amount_paid": "1,584.00", "date_paid": "08/12/2026",
            "insurance_status": "insured",
        })
        row = submission(order_id="O1", tracking_number="529900000009")

        transport.responses = [after_manual_fix]
        assert bfmr.already_submitted([row]) == {("O1", "529900000009")}, "recognised, not resubmitted"

        transport.responses = [after_manual_fix, insured(("529900000009B", 7.4, 1599.96))]
        assert bfmr.file_insurance([row]).skipped[0][1] == "already insured"

        transport.responses = [after_manual_fix]
        payout = bfmr.fetch_payouts(["529900000009"])[0]
        assert payout.tracking_number == "529900000009", "reported under the LEDGER's spelling"
        assert payout.payout_amount == 1584.0 and payout.status == "paid"

    def test_the_hint_does_not_auto_append_letters(self, bfmr, transport):
        """The suffix is BFMR's to assign, not ours to guess. Since 2026-08-13 they append it
        server-side after their own Best Buy check, so a bot that sent letters until something stuck
        would be racing that check and could claim a spelling for the wrong order."""
        transport.responses = [
            tracker({"reserve_id": "R1", "purchase_id": "P1", "order_id": "O1", "qty": 4}),
            FakeResponse(payload={"reservations_response": {}}),
            tracker({"reserve_id": "R1", "purchase_id": "P1", "order_id": "O1", "qty": 4}),
        ]
        bfmr.submit_tracking([submission(order_id="O1", tracking_number="529900000009")])
        posted = [b["tracker_data"][0]["tracking_number"] for b in transport.bodies()]
        assert posted == ["529900000009"], "only the number as the retailer printed it"


class TestBfmrSuffixesDuplicatesItself:
    """BFMR's 2026-08-13 change: submitting a tracking number another earner already used triggers a
    Best Buy check, BFMR APPENDS THE LETTER ITSELF, and the suffixed number can be insured.

    Before this, `_post_tracker_batch` confirmed a push by testing the BARE number against My
    Tracker. That reading turns BFMR's success into a reported failure — and the damage is not just a
    misleading alert: `sync_tracking._run_one_group` blocks anything in `needs_manual` from
    `file_insurance`, so it would skip insuring exactly the cartons BFMR just made insurable.
    """

    def test_a_number_bfmr_recorded_with_its_own_letter_counts_as_submitted(self, bfmr, transport):
        transport.responses = [
            tracker({"reserve_id": "R1", "purchase_id": "P1", "order_id": "O1", "qty": 4}),
            FakeResponse(payload={"reservations_response": {}}),
            # BFMR ran its Best Buy check and stored the shipment under "…B".
            tracker({"reserve_id": "R1", "purchase_id": "P1", "shipment_id": "S1", "order_id": "O1",
                     "tracking_number": "529900000009B", "qty": 4}),
        ]
        result = bfmr.submit_tracking([submission(order_id="O1", tracking_number="529900000009")])

        assert result.submitted == ["529900000009"], "recorded under the LEDGER's spelling"
        assert result.needs_manual == [], "BFMR's own suffixing is not a failure"
        assert result.failed == []

    def test_any_letter_in_the_alphabet_counts_not_just_b(self, bfmr, transport):
        """A carton can hold more orders than BFMR's B/C/D example names, and we no longer choose the
        letter, so the recogniser has to span the whole alphabet or it fails silently."""
        transport.responses = [
            tracker({"reserve_id": "R1", "purchase_id": "P1", "order_id": "O1", "qty": 4}),
            FakeResponse(payload={"reservations_response": {}}),
            tracker({"reserve_id": "R1", "purchase_id": "P1", "shipment_id": "S1", "order_id": "O1",
                     "tracking_number": "529900000009Q", "qty": 4}),
        ]
        result = bfmr.submit_tracking([submission(order_id="O1", tracking_number="529900000009")])
        assert result.submitted == ["529900000009"]
        assert result.needs_manual == []

    def test_a_suffixed_package_is_no_longer_withheld_from_insurance(self, bfmr, transport):
        """The point of the fix. `needs_manual` feeds sync_tracking's `blocked` set, so as long as a
        BFMR-suffixed carton reads as unsubmitted it is also never insured."""
        landed = tracker({"reserve_id": "R1", "purchase_id": "P1", "shipment_id": "S1",
                          "order_id": "O1", "tracking_number": "529900000009B", "qty": 4,
                          "insurance_status": ""})
        transport.responses = [
            tracker({"reserve_id": "R1", "purchase_id": "P1", "order_id": "O1", "qty": 4}),
            FakeResponse(payload={"reservations_response": {}}),
            landed,
        ]
        row = submission(order_id="O1", tracking_number="529900000009")
        push = bfmr.submit_tracking([row])
        blocked = {t for t, _ in push.failed} | {t for t, _ in push.needs_manual}
        assert row.tracking_number not in blocked

        transport.responses = [
            landed, insured(), FakeResponse(payload={"success": True}),
            insured(("529900000009B", 7.4, 1599.96)),
        ]
        assert bfmr.file_insurance([row]).submitted == ["529900000009"]
        filed = [c["data"]["tracking_number"] for c in transport.calls if c.get("data")]
        assert filed == ["529900000009B"], "filed against the spelling BFMR holds, not the bare one"


class TestBfmrPayoutsAndStatus:
    """All of these encode findings from the live probe rather than from BFMR's published spec."""

    def _payout_row(self, **extra):
        return tracker({
            "tracking_number": "TBA1", "shipment_id": "S1", "total_payout": "905.00",
            "amount_paid": "905.00", "date_paid": "08/10/2026", "status": "paid",
            "order_id": "O1", **extra,
        })

    def test_a_paid_package_reports_the_ledgers_paid_status(self, bfmr, transport):
        transport.responses = [self._payout_row()]
        record = bfmr.fetch_payouts(["TBA1"])[0]
        assert record.status == "paid"
        assert record.payout_amount == 905.0  # amount_paid arrives as a STRING

    def test_a_thousands_separator_does_not_zero_the_payout(self, bfmr, transport):
        """BFMR formats money as "2,210.00". A bare float() raises, the caller swallows it to None,
        and the amount silently becomes 0 — so every payout over $999 vanished while the small
        unformatted FEE line survived, booking a $2,210 order as -$10.20."""
        transport.responses = [self._payout_row(amount_paid="2,210.00")]
        assert bfmr.fetch_payouts(["TBA1"])[0].payout_amount == 2210.0

    def test_the_fee_row_becomes_insurance_and_the_deal_row_stays_gross(self, bfmr, transport):
        """Each package has TWO tracker rows: the deal, and a negative FEE row (no order_id, no
        deal_title) which is the insurance premium. Summing them nets to the right bottom line but
        makes a real cost invisible — the sheet would show $2,199.80 with no hint that $10.20 of
        insurance came out of a $2,210 payout. Total Profit subtracts Insurance, so splitting keeps
        the arithmetic identical while making the deduction legible."""
        transport.responses = [tracker(
            {"tracking_number": "TBA1", "shipment_id": "S1", "amount_paid": "2,210.00",
             "total_payout": "2,210.00", "date_paid": "08/05/2026", "status": "paid",
             "order_id": "O1", "deal_title": "MacBook Pro"},
            {"tracking_number": "TBA1", "shipment_id": "S2", "amount_paid": "-10.20",
             "total_payout": "-10.20", "date_paid": "08/05/2026", "status": "paid",
             "order_id": None, "deal_title": None},
        )]
        records = bfmr.fetch_payouts(["TBA1"])
        assert sum(r.payout_amount or 0 for r in records) == 2210.0
        assert sum(r.insurance or 0 for r in records) == 10.20

    def test_the_premium_is_recorded_before_the_package_is_paid(self, bfmr, transport):
        """The premium is committed when the package is insured; `amount_paid` stays "0.00" until
        BFMR settles. Recording it early costs nothing — Total Profit stays blank until Payout
        Amount lands — and shows the cost while the package is still open."""
        transport.responses = [tracker(
            {"tracking_number": "TBA1", "total_payout": "-8.18", "amount_paid": "0.00",
             "status": "processed", "order_id": None, "deal_title": None},
        )]
        assert bfmr.fetch_payouts(["TBA1"])[0].insurance == 8.18

    def test_a_negative_deal_row_is_not_mistaken_for_insurance(self, bfmr, transport):
        """All three signals are required. A genuinely negative DEAL — a chargeback — must stay in
        the payout column rather than being silently reclassified as an insurance cost."""
        transport.responses = [tracker(
            {"tracking_number": "TBA1", "total_payout": "-50.00", "amount_paid": "-50.00",
             "date_paid": "08/05/2026", "status": "paid", "order_id": "O1",
             "deal_title": "Refunded item"},
        )]
        record = bfmr.fetch_payouts(["TBA1"])[0]
        assert record.payout_amount == -50.0 and record.insurance is None

    def test_an_unpaid_package_reports_no_payout_even_though_bfmr_states_its_value(
        self, bfmr, transport
    ):
        """`total_payout` is what the deal is WORTH and exists from the moment a purchase does.
        Writing it would fill Payout Amount — and light up Total Profit — for money that has not
        arrived. `amount_paid` reads "0.00" until BFMR actually pays."""
        transport.responses = [self._payout_row(
            status="shipped", total_payout="1,756.00", amount_paid="0.00", date_paid=None,
        )]
        record = bfmr.fetch_payouts(["TBA1"])[0]
        assert record.payout_amount is None and record.payout_date == ""

    def test_bfmrs_returned_maps_to_the_ledgers_return_spelling(self, bfmr, transport):
        transport.responses = [self._payout_row(status="returned")]
        assert bfmr.fetch_payouts(["TBA1"])[0].status == "return"

    @pytest.mark.parametrize("bfmr_status", ["shipped", "processed", "cancelled"])
    def test_journey_statuses_are_left_to_the_retailer_scrape(self, bfmr, transport, bfmr_status):
        """BFMR reports these too, but the retailer already tracks the package far more precisely —
        letting both write would have the two sources overwriting each other every run. BFMR
        'cancelled' also means the PURCHASE was cancelled, not the retailer order."""
        transport.responses = [self._payout_row(status=bfmr_status)]
        assert bfmr.fetch_payouts(["TBA1"])[0].status == ""

    def test_date_paid_is_us_format_text_not_a_unix_epoch(self, bfmr, transport):
        """The spec types `date_paid` as an integer, which reads as an epoch. It is `MM/DD/YYYY`;
        parsing it as an epoch would have written 1970 dates that merely looked odd."""
        transport.responses = [self._payout_row()]
        assert bfmr.fetch_payouts(["TBA1"])[0].payout_date == "2026-08-10"

    def test_an_unparseable_date_is_left_blank_rather_than_guessed(self, bfmr, transport):
        transport.responses = [self._payout_row(date_paid="soon")]
        assert bfmr.fetch_payouts(["TBA1"])[0].payout_date == ""

    def test_insurance_is_not_reported_because_both_read_endpoints_are_absent(self, bfmr, transport):
        transport.responses = [self._payout_row()]
        assert bfmr.fetch_payouts(["TBA1"])[0].insurance is None


class TestBfmrOrderIdFieldName:
    def test_the_purchase_lookup_reads_order_id_not_order_no(self, bfmr, transport):
        """THE REQUEST AND RESPONSE DISAGREE: the POST takes `order_no`, the GET returns `order_id`.
        Reading only `order_no` matched nothing, so every submission failed with "BFMR has no
        purchase for this order" — a total no-op that looks like a config problem."""
        transport.responses = [
            tracker({"reserve_id": "R1", "purchase_id": "P1", "order_id": "O1", "qty": 3}),
            FakeResponse(payload={"reservations_response": {}}),
            tracker({"tracking_number": "TBA1", "shipment_id": "S9"}),
        ]
        result = bfmr.submit_tracking([submission(order_id="O1", quantity=3)])
        assert result.submitted == ["TBA1"]
        assert transport.bodies()[-1]["tracker_data"][0]["order_no"] == "O1"

    def test_the_older_order_no_spelling_is_still_accepted(self, bfmr, transport):
        transport.responses = [
            tracker({"reserve_id": "R1", "purchase_id": "P1", "order_no": "O1", "qty": 3}),
            FakeResponse(payload={"reservations_response": {}}),
            tracker({"tracking_number": "TBA1", "shipment_id": "S9"}),
        ]
        assert bfmr.submit_tracking([submission(order_id="O1", quantity=3)]).submitted == ["TBA1"]


class TestModPaidStatus:
    def test_appearing_in_the_receipts_report_means_paid(self):
        """MOD has no payment flag and every live row reads STATUS=RECEIVING, so being reported as
        received IS the payment signal."""
        csv_text = TestModCsvParsing.HEADER + "\n" + TestModCsvParsing()._row()
        assert parse_received_items_csv(csv_text)[0].status == "paid"


class TestNothingEverCancels:
    """Cancelling gives up the RESERVATION — the spot in the deal.

    A retailer-cancelled order is often one worth re-ordering into that same spot, and a released
    spot may not be reclaimable, while a cancellation certainly can't be undone. So the asymmetry
    says: never cancel automatically, alert instead. BFMR exposes the endpoints; this pins that no
    code path reaches them, which a reviewer adding "tidy up cancelled purchases" would otherwise
    not think to check.
    """

    @pytest.mark.parametrize("endpoint", [
        "my-tracker/purchase/cancel",
        "my-tracker/reservation/cancel",
    ])
    def test_no_cancel_endpoint_is_referenced_anywhere(self, endpoint):
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        sources = [*(root / "buying_groups").glob("*.py"), root / "sync_tracking.py",
                   *(root / "scripts").glob("*.py")]
        offenders = [
            path.name for path in sources
            if endpoint in path.read_text(encoding="utf-8")
            and "never" not in path.read_text(encoding="utf-8").lower()
        ]
        assert offenders == [], f"{endpoint} must never be called: {offenders}"

    def test_the_client_exposes_no_cancel_method(self, bfmr):
        assert not [name for name in dir(bfmr) if "cancel" in name.lower()]


class TestCancelledOrderDetection:
    def test_an_active_purchase_for_a_cancelled_order_is_reported(self, bfmr, transport):
        transport.responses = [tracker(
            {"purchase_id": "P1", "order_id": "STILL-OPEN", "status": "shipped",
             "deal_title": "PS5", "total_payout": "100.00"},
            {"purchase_id": "P2", "order_id": "ALREADY-DEAD", "status": "cancelled",
             "deal_title": "PS5", "total_payout": "100.00"},
        )]
        assert bfmr.active_purchases_for(["STILL-OPEN", "ALREADY-DEAD"]) == {"STILL-OPEN"}

    def test_an_insurance_fee_row_cannot_be_mistaken_for_an_open_purchase(self, bfmr, transport):
        """Fee rows carry no order number at all, which is what actually excludes them — the
        `_is_insurance_fee_row` check alongside is belt-and-braces. Worth pinning either way: a fee
        row counted as an open purchase would alert on every cancelled order forever."""
        transport.responses = [tracker(
            {"purchase_id": "P1", "order_id": None, "total_payout": "-7.40", "deal_title": None},
        )]
        assert bfmr.active_purchases_for(["O1"]) == set()

    def test_an_order_with_no_tracker_row_at_all_is_not_reported(self, bfmr, transport):
        transport.responses = [tracker()]
        assert bfmr.active_purchases_for(["O1"]) == set()


class TestEmptyReservations:
    def test_no_reservations_is_a_STRING_not_an_empty_list(self, bfmr, transport):
        """BFMR returns the string "No reservations available" in the same field that otherwise
        holds a list. `len()` of it is 25 and iterating yields characters, so scripts/bg_probe.py
        reported "active reservations: 25" against an account holding zero — a wrong number that
        looked entirely plausible."""
        transport.responses = [FakeResponse(payload={
            "reservation_list": "No reservations available", "paging": {"total": 0},
        })]
        assert bfmr.active_reservations() == []

    def test_a_real_list_passes_through(self, bfmr, transport):
        transport.responses = [FakeResponse(payload={
            "reservation_list": [{"reserve_id": "R1", "reserved_quantity": 2}],
        })]
        assert bfmr.active_reservations() == [{"reserve_id": "R1", "reserved_quantity": 2}]


class TestSubmissionSummary:
    def test_skips_are_broken_out_by_reason(self):
        """A dry run skips everything for the reason "dry run". Reporting that as "already known"
        would say the provider has it all — the opposite of the truth, and reason enough for someone
        to skip the real submission."""
        from buying_groups.base import SubmissionResult

        result = SubmissionResult(
            submitted=["A"],
            skipped=[("B", "dry run"), ("C", "dry run"), ("D", "already insured")],
        )
        summary = result.summary()
        assert "2 skipped (dry run)" in summary
        assert "1 skipped (already insured)" in summary
        assert "1 submitted" in summary and "0 failed" in summary


class TestPayoutRecordDefaults:
    def test_only_the_tracking_number_is_required(self):
        """The two providers expose different subsets — MOD's CSV has no paid-date column at all."""
        record = PayoutRecord(tracking_number="1Z9")
        assert record.payout_amount is None and record.payout_date == ""
