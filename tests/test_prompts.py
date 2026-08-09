"""Prompt regression guards.

The prompts are the scraper's real logic, and a careless edit degrades data quality silently — the
run still "succeeds", it just writes wrong rows. These assert the instructions that data integrity
depends on are still present, in both retailers.
"""

import json
import re

import pytest

from models.profile import ProfileConfig
from scrapers.amazon import AmazonScraper
from scrapers.bestbuy import BestBuyScraper

SCRAPERS = pytest.mark.parametrize("scraper_cls", [AmazonScraper, BestBuyScraper], ids=["amazon", "bestbuy"])


def build(scraper_cls, skip_ids=None, recheck=None):
    profile = ProfileConfig(label="test-profile", profile_id="pid", retailers=[scraper_cls.retailer_key])
    return scraper_cls(profile).task_prompt(skip_ids or [], recheck or [])


@SCRAPERS
def test_shipments_are_numbered_not_named(scraper_cls):
    """Shipment is part of the upsert key. Both retailers must NUMBER shipments so the same
    shipment gets the same label every re-check — a label that varies appends a duplicate row."""
    prompt = build(scraper_cls)
    assert '"Shipment 1", "Shipment 2"' in prompt
    assert "INCLUDING a single-shipment order" in prompt


@SCRAPERS
def test_no_word_numbered_shipment_labels(scraper_cls):
    """Best Buy used to be told to copy the page's "Shipment One" wording; that caused the
    duplicate-row risk. Neither prompt should instruct word-numbered labels again."""
    prompt = build(scraper_cls)
    for banned in ("Shipment One", "Shipment Two", "Shipment Three"):
        assert banned not in prompt, f"{banned!r} reintroduces the varying-label duplicate-row bug"


@SCRAPERS
def test_digital_items_are_skipped(scraper_cls):
    prompt = build(scraper_cls)
    assert "IGNORE digital items entirely" in prompt


@SCRAPERS
def test_cancelled_orders_are_skipped_but_not_a_stopping_point(scraper_cls):
    """A cancelled order between two valid ones must not halt discovery — the suspected cause of
    the missed 08/02 and 08/05 orders in the first Best Buy run."""
    prompt = build(scraper_cls)
    assert "KEEP scanning" in prompt
    assert "NOT a stopping point" in prompt


@SCRAPERS
def test_cancelled_is_a_valid_status(scraper_cls):
    prompt = build(scraper_cls)
    assert "ordered | shipped | delivered | cancelled" in prompt


@SCRAPERS
def test_discovery_scans_the_whole_window(scraper_cls):
    prompt = build(scraper_cls)
    assert "Do NOT stop early after the first order" in prompt


@SCRAPERS
def test_total_cost_is_computed_not_order_grand_total(scraper_cls):
    # The agent used to be told "order grand total"; now it leaves total_cost blank and the model
    # computes quantity * cost_per_item per row.
    prompt = build(scraper_cls)
    assert "order grand total" not in prompt
    assert "computed as quantity x cost_per_item" in prompt


@SCRAPERS
def test_quantity_is_preserved_not_exploded_into_rows(scraper_cls):
    prompt = build(scraper_cls)
    assert "do NOT create duplicate rows" in prompt
    assert "quantity = the total count" in prompt


@SCRAPERS
def test_logged_out_contract(scraper_cls):
    prompt = build(scraper_cls)
    assert "do not attempt to log in" in prompt
    assert '{"logged_out": true, "items": []}' in prompt


@SCRAPERS
def test_recheck_block_absent_when_nothing_to_recheck(scraper_cls):
    assert "RE-CHECK these already-recorded" not in build(scraper_cls)


@SCRAPERS
def test_recheck_block_lists_the_orders(scraper_cls):
    prompt = build(scraper_cls, recheck=[{"order_id": "A1", "order_date": "2026-08-08", "status": "ordered"}])
    assert "RE-CHECK these already-recorded" in prompt
    assert "A1" in prompt


@SCRAPERS
def test_recheck_entries_told_to_copy_identity_fields_exactly(scraper_cls):
    """order_date and item_name are part of the upsert key — a re-worded item name duplicates."""
    prompt = build(scraper_cls, recheck=[{"order_id": "A1", "order_date": "2026-08-08", "status": "ordered"}])
    assert "Copy order_date and item_name" in prompt
    assert "EXACTLY as already recorded" in prompt


@SCRAPERS
def test_job2_shows_a_trimmed_example_not_just_the_full_one(scraper_cls):
    """The prose says 'fill only these fields' but the schema example used to show all 18, so the
    agent had to infer scope from prose the example contradicted. Both shapes must now be shown."""
    prompt = build(scraper_cls)
    assert "this shape, not" in prompt
    objects = re.findall(r"\{\s*\n\s*\"retailer\".*?\n\}", prompt, re.DOTALL)
    assert len(objects) >= 1, "expected a standalone JOB 2 example object"
    trimmed = json.loads(objects[0])
    assert trimmed["delivery_address"] == "", "JOB 2 example should show address blank"
    assert trimmed["quantity"] is None, "JOB 2 example should show numerics null"
    assert trimmed["tracking_url"] == "...", "JOB 2 example should still capture the tracking link"
    # Amazon's tracking number lives on a separate page and is read by the selector reader, so the
    # agent leaves it blank. Best Buy shows it on the order-details page, so the agent fills it.
    expected = "" if scraper_cls is AmazonScraper else "..."
    assert trimmed["tracking_number"] == expected


@SCRAPERS
def test_skip_list_is_rendered(scraper_cls):
    prompt = build(scraper_cls, skip_ids=["A1", "B2"])
    assert "Already recorded" in prompt
    assert "A1, B2" in prompt


@SCRAPERS
def test_prompt_renders_without_stray_format_braces(scraper_cls):
    """The prompts are f-strings containing JSON, so every literal brace must be doubled. A missed
    pair silently swallows part of the schema."""
    prompt = build(scraper_cls, skip_ids=["A1"], recheck=[{"order_id": "A1", "status": "ordered"}])
    assert "{{" not in prompt and "}}" not in prompt
    assert prompt.count("{") == prompt.count("}")


def test_bestbuy_ignores_page_wording_for_labels():
    prompt = build(BestBuyScraper)
    assert "Do NOT copy Best Buy's own" in prompt  # phrase wraps across lines in the prompt
    assert "ignore it and use the numbering rule" in prompt


def test_amazon_captures_tracking_url_even_before_shipping():
    # The tracking page is where the number appears later; losing the link costs an extra hop.
    assert "IF the shipment hasn't shipped yet" in build(AmazonScraper)


def test_amazon_does_not_open_tracking_pages():
    """The single biggest token cost was the agent opening one tracking page per shipment. That
    work belongs to the CDP selector reader now; the agent only captures the link."""
    prompt = build(AmazonScraper, recheck=[{"order_id": "A1", "status": "shipped"}])
    assert "do NOT open it" in prompt
    assert "do NOT open any tracking page" in prompt


def test_amazon_leaves_tracking_number_to_the_selector_reader():
    assert 'tracking_number: leave ""' in build(AmazonScraper)


@SCRAPERS
def test_status_is_read_from_the_order_details_page(scraper_cls):
    """Both retailers judge status from the shipment's status text on the order-details page, so
    a row means the same thing whichever retailer wrote it."""
    prompt = build(scraper_cls)
    assert "status text on the order-details page" in prompt
    assert '* "delivered" — the shipment says "Delivered"' in prompt


@SCRAPERS
def test_delivery_address_is_per_shipment(scraper_cls):
    assert "the shipping address for THIS shipment" in build(scraper_cls)
