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
    @pytest.mark.parametrize("value", ["ordered", "shipped", "delivered"])
    def test_known_statuses_pass_through(self, value):
        assert _item(status=value).status == value

    @pytest.mark.parametrize("value", ["Delivered", "SHIPPED", "  Ordered  "])
    def test_case_and_whitespace_are_normalized(self, value):
        # load_order_state compares against lowercase literals; "Delivered" would never roll up.
        assert _item(status=value).status == value.strip().lower()

    @pytest.mark.parametrize("value", ["processing", "cancelled", "returned", "out for delivery"])
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
