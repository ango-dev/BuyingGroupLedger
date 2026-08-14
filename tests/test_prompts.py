"""Prompt regression guards.

The prompts are the scraper's real logic, and a careless edit degrades data quality silently — the
run still "succeeds", it just writes wrong rows. These assert the instructions that data integrity
depends on are still present, in both retailers.
"""

import pytest

from models.profile import ProfileConfig, RetailerAuth
from scrapers.amazon import AmazonScraper
from scrapers.bestbuy import BestBuyScraper
from scrapers.costco import CostcoScraper

SCRAPERS = pytest.mark.parametrize(
    "scraper_cls",
    [AmazonScraper, BestBuyScraper, CostcoScraper],
    ids=["amazon", "bestbuy", "costco"],
)


def build(scraper_cls, skip_ids=None, recheck=None, auth=None):
    profile = ProfileConfig(
        label="test-profile",
        profile_id="pid",
        retailers=[scraper_cls.retailer_key],
        auth=auth or {},
    )
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


def test_amazon_numbers_shipments_by_status_card_matching_the_api():
    """The deterministic API path splits shipments by Amazon's own shipmentId (one per status card),
    so a 2-package order reads Shipment 1 / Shipment 2 and a returned item is its own shipment. The
    agent must match that or the two paths write divergent Shipment numbers -> duplicate rows on any
    order both paths touch. Guard the wording that makes the agent count one shipment per status card,
    including same-status cards and returns."""
    prompt = build(AmazonScraper)
    assert "exactly ONE shipment\n  per status card" in prompt or "ONE shipment per status card" in prompt.replace("\n  ", " ")
    assert "SAME status word AND the SAME date" in prompt
    assert "Return started" in prompt and "its OWN shipment" in prompt


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


def test_costco_job2_is_full_extraction():
    """Costco agent-fallback re-checks now FULL-extract every shipment (not the trimmed form), so a
    split order's new shipment rows get their own quantity/cost/card and Shipment 1's quantity updates.
    Ported from the Best Buy split fix; the primary GraphQL path was already immune. Unlike Amazon,
    Costco shows the tracking number on the order page, so the agent fills it (no CDP reader)."""
    prompt = build(
        CostcoScraper,
        recheck=[{"order_id": "A1", "order_date": "2026-08-08", "status": "shipped"}],
    )
    # The trimmed form is gone for Costco (no retailer uses it anymore).
    assert "leaving every other field empty" not in prompt
    assert "this shape, not" not in prompt  # trimmed example object removed
    # Re-check is told to fill the per-shipment cost/quantity/card fields, and why (splits).
    assert "filling ALL fields for each shipment" in prompt
    assert "quantity, cost_per_item, shipping" in prompt
    assert "can SPLIT" in prompt
    # Identity fields still copied verbatim so the row key still matches (no duplicate row).
    assert "Copy order_date and item_name" in prompt


def test_amazon_job2_is_full_extraction():
    """Amazon re-checks FULL-extract every shipment (not the trimmed form), so when an order splits the
    new shipment rows get their own quantity/cost/card and Shipment 1's quantity updates. The Amazon
    twist vs Best Buy: tracking_number STAYS BLANK on re-check because the CDP selector reader owns
    Amazon's number — filling it would clobber CDP. Regression for the latent split bug."""
    prompt = build(
        AmazonScraper,
        recheck=[{"order_id": "A1", "order_date": "2026-08-08", "status": "shipped"}],
    )
    # The trimmed form is gone for Amazon.
    assert "leave the rest empty" not in prompt
    assert "leaving every other field empty" not in prompt
    assert "this shape, not" not in prompt  # trimmed example object removed
    # Re-check is told to fill the per-shipment cost/quantity/card fields, and why (splits).
    assert "filling ALL fields for each shipment" in prompt
    assert "quantity, cost_per_item, shipping" in prompt
    assert "can SPLIT" in prompt
    # ...but tracking_number stays blank (CDP owns it) and the agent must not open the tracking page.
    assert 'leave tracking_number ""' in prompt
    assert "do NOT open any tracking page" in prompt
    # Identity fields still copied verbatim so the row key still matches (no duplicate row).
    assert "Copy order_date and item_name" in prompt


def test_bestbuy_job2_is_full_extraction():
    """Best Buy re-checks FULL-extract every shipment (not the trimmed form), so when an order splits
    the new shipment rows get their own quantity/cost/card and Shipment 1's quantity updates. Regression
    for the split bug where Shipments 2-5 landed blank and Shipment 1 kept its pre-split quantity."""
    prompt = build(
        BestBuyScraper,
        recheck=[{"order_id": "A1", "order_date": "2026-08-08", "status": "shipped"}],
    )
    # The trimmed form is gone for Best Buy.
    assert "leave the rest empty" not in prompt
    assert "this shape, not" not in prompt  # trimmed example object removed
    # Re-check is told to fill the per-shipment cost/quantity/card fields, and why (splits).
    assert "filling ALL fields for each shipment" in prompt
    assert "quantity, cost_per_item, shipping" in prompt
    assert "can SPLIT" in prompt
    # Identity fields still copied verbatim so the row key still matches (no duplicate row).
    assert "Copy order_date and item_name" in prompt


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


def test_bestbuy_without_auth_still_reports_logged_out():
    """No auth configured = pre-auto-auth behavior: report logged_out, do not try to log in."""
    prompt = build(BestBuyScraper)
    assert "do not attempt to log in" in prompt
    assert "Continue with Google" not in prompt


def test_bestbuy_password_auth_supplies_credentials():
    """The agent gets username + password and the exact 3-screen click path.

    Naming the steps and ids is what keeps this cheap: "Use password" is the LAST radio on the
    method chooser and sits below the fold, so left to itself the agent burns ~40 steps and a pile
    of screenshots hunting for it.
    """
    prompt = build(
        BestBuyScraper,
        auth={"bestbuy": RetailerAuth(
            method="password", username="me@example.com", password="hunter2")},
    )
    assert "me@example.com" in prompt
    assert "hunter2" in prompt
    assert "Keep me signed in" in prompt
    assert "password-radio" in prompt
    # Still able to bail out cleanly if login fails.
    assert '{"logged_out": true, "items": []}' in prompt
    # f-string braces stay balanced with the JSON in the sign-in block.
    assert "{{" not in prompt and "}}" not in prompt
    assert prompt.count("{") == prompt.count("}")


def test_the_removed_auth_methods_leave_no_trace_in_the_prompt():
    """Google SSO, Apple and in-browser TOTP were removed.

    Asserted on the PROMPT, not just the config, because the prompt is the whole product of this
    module — a stray "or use Google" line would send the agent down a path nothing supports, and it
    would only ever be discovered on a run where the session had actually lapsed.
    """
    prompt = build(
        BestBuyScraper,
        auth={"bestbuy": RetailerAuth(
            method="password", username="me@example.com", password="hunter2")},
    )
    assert "Continue with Google" not in prompt
    assert "crypto.subtle" not in prompt          # the TOTP generator
    assert "changes every 30 seconds" not in prompt


def test_a_2fa_challenge_bails_out_instead_of_looping():
    """Nothing can answer a challenge now, so 2-step verification must be OFF on the account. The
    agent has to report logged_out rather than sit on the challenge screen burning steps."""
    prompt = build(
        BestBuyScraper,
        auth={"bestbuy": RetailerAuth(method="password", username="u", password="p")},
    )
    assert "cannot complete it" in prompt
    assert "2-step verification is supposed to be OFF" in prompt
    assert '{"logged_out": true, "items": []}' in prompt


def test_bestbuy_prompt_gives_an_efficient_method():
    """After a 65-step / 4.9M-token run, the prompt now tells the coding agent to work economically:
    handle lazy-loading in one pass, read compact JSON not full-page blobs, navigate directly to
    order-details rather than clicking through the UI."""
    prompt = build(BestBuyScraper)
    assert "WORK EFFICIENTLY" in prompt
    assert "LAZY-LOAD" in prompt
    assert "navigate DIRECTLY to its details URL" in prompt
    assert "COMPACT JSON" in prompt


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


def test_costco_ignores_page_wording_for_labels():
    prompt = build(CostcoScraper)
    assert "Do NOT copy Costco's own" in prompt  # phrase wraps across lines in the prompt
    assert "ignore it and use the numbering rule" in prompt


def test_costco_never_infers_ordered_from_a_failed_tracking_read():
    """OBSERVED LIVE, twice, with two different symptoms.

    Run 1: the agent reported box 1's tracking number for BOTH boxes. The prompt gained an explicit
    "if you are about to report the SAME tracking number for two shipments, STOP" rule, and run 2
    obeyed it — by reporting the second box's number BLANK instead. Blank is the safe failure (the
    ledger keeps what it already had); a duplicate manufactures a box that does not exist.

    But blank then cascaded: the prompt defined "ordered" as "not shipped yet: no tracking number",
    so a failed READ was reported as a fact about the WORLD, and a delivered row was walked back to
    `ordered`. Status is now judged from the shipment's own status text, and "ordered" needs positive
    evidence.
    """
    prompt = build(CostcoScraper)
    assert "NEVER use \"ordered\" merely because you could not find a tracking number" in prompt
    assert "POSITIVE evidence" in prompt
    # The rule that turned the dangerous failure into the safe one must stay.
    assert "SAME tracking number for two different shipments" in prompt


def test_costco_knows_one_track_link_can_cover_several_boxes():
    """THE ROOT CAUSE of both bad agent runs, found by reading real order-details JSON.

    `trackingSiteUrl` belongs to the ORDER LINE, not the package: order 1399000004 has one line whose
    `shipment` list holds TWO packages sharing `fid=399000001`. So a quantity-2 single-SKU order that
    ships in two boxes shows ONE product row with ONE "Track" link, and that link's page lists both
    packages.

    The prompt used to assert the opposite — that each shipment has "its OWN status, tracking number,
    tracking link" — and told the agent to follow the link "once to read it". Between them, those made
    the correct read impossible to describe: the agent found one number where it needed two, and had
    to either duplicate it or leave it blank. Both happened, on consecutive runs.
    """
    prompt = build(CostcoScraper)
    assert "ONE \"Track\" LINK CAN COVER SEVERAL BOXES" in prompt
    assert "read EVERY package listed on it" in prompt
    # A shared tracking_url is now stated to be NORMAL, so it can't be read as evidence of a bad read.
    assert "identical tracking_url is never a reason to doubt your read" in prompt
    # And the old false claim is gone.
    assert "OWN status, tracking number,\n  tracking link" not in prompt


def test_costco_prefers_a_blank_tracking_number_over_a_duplicated_one():
    """The two failures are not equally bad and the prompt must say so: a blank is corrected next
    run, a duplicate manufactures a box that does not exist."""
    prompt = build(CostcoScraper)
    assert "Never duplicate a number to fill the gap" in prompt


def test_costco_prompt_gives_an_efficient_method():
    """Costco has no cheap CDP path, so the whole re-check rides the agent — keep step count low the
    same way Best Buy does: handle lazy-loading in one pass and read compact JSON, not full-page blobs."""
    prompt = build(CostcoScraper)
    assert "WORK EFFICIENTLY" in prompt
    assert "LAZY-LOAD" in prompt
    assert "COMPACT JSON" in prompt


def test_costco_prompt_stops_exploring_once_no_tracking_is_found():
    """A live run (2026-08-12) burned 67 steps / $0.237 (vs. an ~11-step / $0.02 baseline) on an order
    that simply hadn't shipped yet — the agent kept re-querying selectors and delegating sub-agent reads
    to CONFIRM the tracking section was genuinely absent, rather than accepting one clean read that
    already said so. The prompt now tells it explicitly to stop after one read."""
    prompt = build(CostcoScraper)
    assert "MOVE ON" in prompt  # wraps across a line before "immediately."
    assert "Do NOT try a second or third selector" in prompt
    assert "do NOT delegate a sub-agent task" in prompt


def test_costco_scopes_to_online_shipped_orders_only():
    """Costco's Orders & Purchases page mixes shippable online orders with in-warehouse pickups and
    Same-Day/Instacart grocery — only the first ships with carrier tracking, so the rest are skipped."""
    prompt = build(CostcoScraper)
    assert "ONLINE SHIPPED ORDERS ONLY" in prompt
    assert "warehouse" in prompt.lower()
    assert "Instacart" in prompt


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
