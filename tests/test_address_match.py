"""Address -> buying-group classification (config.warehouses).

Pure and offline: no warehouses.json on disk is touched — every test builds Warehouse/Jig objects
directly and calls classify_address, so it exercises the matching logic, not the file loader.
"""

import pytest

from config.warehouses import (
    UNCLASSIFIED,
    classify_address,
    is_personal,
    normalize_address,
    tag_and_filter_personal,
)
from models.order import OrderItem
from models.warehouse import Jig, Warehouse


def wh(group, *jig_kwargs):
    return Warehouse(buying_group=group, jigs=[Jig(**kw) for kw in jig_kwargs])


class TestNormalizeAddress:
    def test_lowercases_and_strips_punctuation_and_collapses_space(self):
        assert normalize_address("123 Main St., Ste #4,  New York") == "123 main st ste 4 new york"

    def test_blank_stays_blank(self):
        assert normalize_address("   ") == ""


class TestClassify:
    def test_matches_a_jig_by_all_required_substrings(self):
        warehouses = [wh("BFMR", dict(street="123 Main St", zip="10001", name_contains="c/o BFMR"))]
        addr = "John Smith c/o BFMR, 123 Main St Ste 4, New York NY 10001"
        assert classify_address(addr, warehouses) == "BFMR"

    def test_no_match_when_one_required_substring_is_absent(self):
        # street + zip present but the required name token isn't -> not this jig -> Unclassified.
        warehouses = [wh("BFMR", dict(street="123 Main St", zip="10001", name_contains="c/o BFMR"))]
        addr = "Jane Doe, 123 Main St, New York NY 10001"
        assert classify_address(addr, warehouses) == UNCLASSIFIED

    def test_partial_field_jig_matches_on_what_it_specifies(self):
        # A jig with only a zip matches any address carrying that zip.
        warehouses = [wh("MaxOutDeals", dict(zip="30303"))]
        assert classify_address("9 Oak Ave, Atlanta GA 30303", warehouses) == "MaxOutDeals"

    def test_first_matching_jig_wins_across_groups(self):
        # Two groups could match; config order decides.
        warehouses = [
            wh("BFMR", dict(zip="10001")),
            wh("MaxOutDeals", dict(street="123 Main St")),
        ]
        addr = "123 Main St, New York NY 10001"
        assert classify_address(addr, warehouses) == "BFMR"

    def test_personal_group_tags_the_users_own_address(self):
        warehouses = [
            wh("BFMR", dict(zip="10001")),
            wh("Personal", dict(zip="94103", name_contains="Test Buyer")),
        ]
        assert classify_address("Test Buyer, 5 Home St, San Francisco CA 94103", warehouses) == "Personal"

    def test_non_blank_no_match_is_unclassified(self):
        warehouses = [wh("BFMR", dict(zip="10001"))]
        assert classify_address("Somewhere else, 99999", warehouses) == UNCLASSIFIED

    def test_empty_config_makes_everything_unclassified(self):
        assert classify_address("123 Main St, New York NY 10001", []) == UNCLASSIFIED

    def test_blank_address_returns_empty_not_unclassified(self):
        # A partial re-check carries no address; a blank tag lets _merge_row preserve the earlier one.
        warehouses = [wh("BFMR", dict(zip="10001"))]
        assert classify_address("", warehouses) == ""
        assert classify_address("   ", warehouses) == ""

    def test_matching_is_case_and_punctuation_insensitive(self):
        warehouses = [wh("BFMR", dict(name_contains="C/O BFMR", zip="10001"))]
        assert classify_address("john, c/o bfmr., 10001", warehouses) == "BFMR"

    def test_contains_list_requires_all_of_its_entries(self):
        warehouses = [wh("BFMR", dict(contains=["suite 200", "acme logistics"]))]
        assert classify_address("Acme Logistics, Suite 200, TX", warehouses) == "BFMR"
        assert classify_address("Acme Logistics, TX", warehouses) == UNCLASSIFIED


def _item(address):
    return OrderItem(retailer="Amazon", order_id="1", order_date="2026-08-11",
                     item_name="Thing", delivery_address=address)


class TestTagAndFilterPersonal:
    warehouses = [
        Warehouse(buying_group="BFMR", jigs=[Jig(zip="10001")]),
        Warehouse(buying_group="Personal", jigs=[Jig(zip="94103")]),
    ]

    def test_personal_rows_are_dropped(self):
        items = [_item("123 Main St, New York NY 10001"), _item("5 Home St, SF CA 94103")]
        kept, dropped, unclassified = tag_and_filter_personal(items, self.warehouses)
        assert [it.buying_group for it in kept] == ["BFMR"]
        assert dropped == 1
        assert unclassified == 0

    def test_unclassified_rows_are_kept_not_dropped(self):
        # The reliability rule: an unrecognized address might be a real warehouse -> keep it, don't drop.
        items = [_item("77 Random Rd, Reno NV 89501")]
        kept, dropped, unclassified = tag_and_filter_personal(items, self.warehouses)
        assert [it.buying_group for it in kept] == [UNCLASSIFIED]
        assert dropped == 0
        assert unclassified == 1

    def test_blank_address_row_is_kept(self):
        # A partial re-check has no address; keep the row so its tag is preserved on merge.
        items = [_item("")]
        kept, dropped, unclassified = tag_and_filter_personal(items, self.warehouses)
        assert len(kept) == 1
        assert kept[0].buying_group == ""
        assert dropped == 0

    def test_personal_match_is_case_insensitive(self):
        warehouses = [Warehouse(buying_group="personal", jigs=[Jig(zip="94103")])]
        kept, dropped, _ = tag_and_filter_personal([_item("5 Home St 94103")], warehouses)
        assert kept == []
        assert dropped == 1

    def test_is_personal_helper(self):
        assert is_personal("Personal")
        assert is_personal("  personal ")
        assert not is_personal("BFMR")
        assert not is_personal(UNCLASSIFIED)


class TestJigValidation:
    def test_a_jig_with_no_match_fields_is_rejected(self):
        # An empty jig would match every address and silently tag personal orders with a buying group.
        with pytest.raises(ValueError, match="no match fields"):
            Jig(label="oops")

    def test_whitespace_only_fields_count_as_empty(self):
        with pytest.raises(ValueError, match="no match fields"):
            Jig(street="   ", zip="")
