"""OrderItem validators — the last line of defence between agent output and the ledger."""

import pytest

from models.order import OrderItem


def _item(**overrides):
    base = dict(retailer="Amazon", order_id="111-2223334-5556667", order_date="2026-08-08", item_name="Thing")
    return OrderItem(**{**base, **overrides})


class TestBlankToNone:
    """Blank numerics must become None, never 0 — _merge_row only protects existing data from
    BLANK cells, so a 0 would happily overwrite a real recorded price."""

    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    def test_blank_numeric_becomes_none(self, blank):
        item = _item(quantity=blank, cost_per_item=blank, shipping=blank, total_cost=blank)
        assert (item.quantity, item.cost_per_item, item.shipping, item.total_cost) == (None,) * 4

    def test_real_numbers_survive(self):
        item = _item(quantity=3, cost_per_item=189.99, shipping=0.0, total_cost=569.97)
        assert item.quantity == 3
        assert item.cost_per_item == 189.99
        assert item.shipping == 0.0

    def test_zero_is_preserved_not_treated_as_blank(self):
        # Free shipping is a real 0, not a missing value.
        assert _item(shipping=0).shipping == 0.0


class TestStatusNormalization:
    @pytest.mark.parametrize("value", ["ordered", "shipped", "delivered", "cancelled"])
    def test_known_statuses_pass_through(self, value):
        # tracking_number present so 'shipped' survives _shipped_requires_tracking (see that class).
        assert _item(status=value, tracking_number="1Z999").status == value

    @pytest.mark.parametrize("value", ["Delivered", "SHIPPED", "  Ordered  ", "Cancelled"])
    def test_case_and_whitespace_are_normalized(self, value):
        # load_order_state compares against lowercase literals; "Delivered" would never roll up.
        assert _item(status=value, tracking_number="1Z999").status == value.strip().lower()

    @pytest.mark.parametrize("value", ["processing", "returned", "out for delivery"])
    def test_unknown_status_coerces_to_ordered_and_warns(self, value, caplog):
        # Coerce rather than reject: raising here would abort model_validate_json for the WHOLE
        # batch and lose every other order in the run. "ordered" keeps it open and re-checked.
        with caplog.at_level("WARNING"):
            item = _item(status=value)
        assert item.status == "ordered"
        assert "Unrecognized status" in caplog.text

    def test_blank_status_defaults_to_ordered_without_warning(self, caplog):
        with caplog.at_level("WARNING"):
            assert _item(status="").status == "ordered"
        assert "Unrecognized status" not in caplog.text

    def test_default_status_is_ordered(self):
        assert _item().status == "ordered"


class TestShippedRequiresTracking:
    """A row can't claim 'shipped' without a tracking number. Amazon's agent marks a shipment
    'shipped' from the "Arriving <date>" text while leaving the number blank (CDP reads it later),
    which would otherwise assert shipped with no proof. The invariant downgrades that to 'ordered'
    until the number lands, then the CDP re-check promotes it back to 'shipped'."""

    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    def test_shipped_without_tracking_downgrades_to_ordered(self, blank):
        assert _item(status="shipped", tracking_number=blank).status == "ordered"

    def test_shipped_with_tracking_stays_shipped(self):
        assert _item(status="shipped", tracking_number="1Z999").status == "shipped"

    def test_case_shipped_without_tracking_still_downgrades(self):
        # Normalization runs first (SHIPPED -> shipped), then the invariant downgrades it.
        assert _item(status="SHIPPED").status == "ordered"

    def test_delivered_without_tracking_is_untouched(self):
        # delivered is terminal and its own proof; only 'shipped' requires a number.
        assert _item(status="delivered", tracking_number="").status == "delivered"

    def test_ordered_without_tracking_is_untouched(self):
        assert _item(status="ordered", tracking_number="").status == "ordered"


class TestTotalCost:
    """total_cost is computed per row = quantity * cost_per_item, so the column sums to the order
    total. Previously the agent reported the order grand total on every row."""

    def test_computed_from_quantity_and_cost(self):
        assert _item(quantity=3, cost_per_item=1134.0).total_cost == 3402.0

    def test_single_unit(self):
        assert _item(quantity=1, cost_per_item=899.99).total_cost == 899.99

    def test_computed_value_overrides_agent_supplied_total(self):
        # The agent used to send the order grand total here; the computed line total must win.
        assert _item(quantity=1, cost_per_item=899.99, total_cost=5201.98).total_cost == 899.99

    def test_rounded_to_cents(self):
        assert _item(quantity=3, cost_per_item=19.999).total_cost == 60.0

    @pytest.mark.parametrize("q,c", [(None, 10.0), (2, None), (None, None)])
    def test_left_untouched_when_a_factor_is_missing(self, q, c):
        # A tracking-only re-check sends both blank; total_cost must stay None so _merge_row keeps
        # whatever was already recorded rather than zeroing it.
        assert _item(quantity=q, cost_per_item=c).total_cost is None
