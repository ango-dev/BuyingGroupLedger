"""Credit-card resolution: last-4 -> card name + cashback rate.

The failure mode this guards is quiet: a card that resolves to the wrong entry, or a rate read as
200% instead of 2%, produces a plausible-looking profit number that's off by orders of magnitude and
nothing in the run would complain. These tests pin the normalization, the fallbacks, and the
blank-means-preserve rule the sheet upsert depends on.
"""

import json

import pytest
from pydantic import ValidationError

from config import cards as cards_module
from config.cards import KNOWN_RETAILERS, load_cards, resolve_card, tag_cards
from models.card import Card, normalize_last4, normalize_retailer
from models.order import OrderItem


def item(**values):
    base = dict(retailer="Amazon", order_id="A1", order_date="2026-08-11", item_name="Widget")
    base.update(values)
    return OrderItem(**base)


class TestNormalizeLast4:
    @pytest.mark.parametrize(
        "raw",
        [
            "4321",
            "ending in 4321",           # Amazon's order-details wording
            "************4321",         # Best Buy ss-api masked PAN
            "xxxx4321",                 # Costco
            "Visa ...4321",
            " 4321 ",
        ],
    )
    def test_every_retailer_spelling_reduces_to_the_digits(self, raw):
        assert normalize_last4(raw) == "4321"

    @pytest.mark.parametrize("raw", ["", "   ", "Visa", "12", None])
    def test_unusable_input_is_blank(self, raw):
        assert normalize_last4(raw) == ""

    def test_longer_digit_runs_take_the_LAST_four(self):
        # A full PAN would be a config mistake, but taking the last 4 is still the right reading.
        assert normalize_last4("4111111111114321") == "4321"


class TestCardModel:
    def test_percent_string_becomes_a_fraction(self):
        assert Card(last4="4321", name="C", cashback_rate="2%").cashback_rate == 0.02

    def test_decimal_fraction_is_kept(self):
        assert Card(last4="4321", name="C", cashback_rate=0.015).cashback_rate == 0.015

    def test_omitted_rate_is_none_not_zero(self):
        # None means "use the default"; 0 would mean "this card earns nothing" — different things.
        assert Card(last4="4321", name="C").cashback_rate is None

    def test_bare_two_is_rejected_rather_than_guessed(self):
        # "2" reads equally as 2% or 200%. Guessing wrong misstates every profit number on the sheet.
        with pytest.raises(ValidationError, match="outside 0-1"):
            Card(last4="4321", name="C", cashback_rate=2)

    def test_a_bad_retailer_rate_is_rejected_too(self):
        # The per-retailer overrides get the same 100x guard as the card's overall rate.
        with pytest.raises(ValidationError, match="outside 0-1"):
            Card(last4="4321", name="C", retailer_rates={"Amazon": 5})

    def test_negative_rate_is_rejected(self):
        with pytest.raises(ValidationError, match="outside 0-1"):
            Card(last4="4321", name="C", cashback_rate=-0.01)

    def test_card_without_usable_digits_is_rejected(self):
        with pytest.raises(ValidationError, match="no usable last4"):
            Card(last4="oops", name="C")

    def test_last4_is_normalized_from_config_too(self):
        # So a config written as "ending in 4321" still matches a scraped "4321".
        assert Card(last4="ending in 4321", name="C").last4 == "4321"


class TestResolveCard:
    cards = [
        Card(last4="4321", name="Freedom Unlimited", cashback_rate=0.015),
        Card(last4="8765", name="Flat Rate Card"),  # no rate of its own -> default
    ]

    def test_known_card_uses_its_own_rate(self):
        assert resolve_card("4321", self.cards, default_rate=0.02) == ("Freedom Unlimited", 0.015)

    def test_known_card_without_a_rate_falls_back_to_the_default(self):
        assert resolve_card("8765", self.cards, default_rate=0.02) == ("Flat Rate Card", 0.02)

    def test_unconfigured_card_keeps_the_default_rate_but_no_name(self):
        # Name blank on purpose: the gap stays visible on the sheet AND a hand-typed name survives
        # _merge_row. The rate still applies so Total Profit remains computable.
        assert resolve_card("0000", self.cards, default_rate=0.02) == ("", 0.02)

    def test_blank_last4_yields_blanks_not_the_default_rate(self):
        # THE important one: a partial re-check carries no card. Returning the default rate here would
        # overwrite a card-specific rate on the sheet every single re-check.
        assert resolve_card("", self.cards, default_rate=0.02) == ("", None)
        assert resolve_card("   ", self.cards, default_rate=0.02) == ("", None)

    def test_retailer_spelling_still_matches_the_config(self):
        assert resolve_card("************4321", self.cards, default_rate=0.02)[0] == "Freedom Unlimited"

    def test_no_config_at_all_still_applies_the_default_rate(self):
        assert resolve_card("4321", [], default_rate=0.02) == ("", 0.02)


class TestScopedCards:
    """Two cards can genuinely share a last4 across accounts; scope breaks the tie."""

    cards = [
        Card(last4="1111", name="Catch-all Amex"),
        Card(last4="1111", name="Business Amex", cashback_rate=0.04, profile="profile-alpha"),
    ]

    def test_scoped_entry_beats_the_unscoped_one(self):
        assert resolve_card("1111", self.cards, profile_label="profile-alpha",
                            default_rate=0.01) == ("Business Amex", 0.04)

    def test_other_profiles_get_the_catch_all(self):
        assert resolve_card("1111", self.cards, profile_label="profile-bravo",
                            default_rate=0.01) == ("Catch-all Amex", 0.01)

    def test_ambiguous_duplicates_warn_instead_of_silently_picking(self, caplog):
        cards = [Card(last4="3333", name="A"), Card(last4="3333", name="B")]
        with caplog.at_level("WARNING"):
            name, _ = resolve_card("3333", cards, default_rate=0.0)
        assert name == "A"  # file order decides, but noisily
        assert "3333" in caplog.text


class TestRetailerRates:
    """A card's earn rate is category-dependent, so each card carries an overall rate plus optional
    per-retailer overrides. The rate resolves in three tiers: retailer override -> card overall ->
    the global DEFAULT_CASHBACK_RATE."""

    card = Card(
        last4="4321",
        name="Freedom Unlimited",
        cashback_rate=0.015,
        retailer_rates={"Amazon": "5%", "Best Buy": 0.03},
    )

    def test_retailer_override_beats_the_cards_overall_rate(self):
        assert resolve_card("4321", [self.card], retailer="Amazon", default_rate=0.0) == (
            "Freedom Unlimited", 0.05,
        )

    def test_retailer_without_an_override_falls_back_to_the_cards_overall_rate(self):
        assert resolve_card("4321", [self.card], retailer="Costco", default_rate=0.0) == (
            "Freedom Unlimited", 0.015,
        )

    def test_card_with_only_overrides_falls_back_to_the_global_default(self):
        # No overall rate set, and this retailer has no override -> tier 3.
        card = Card(last4="9999", name="Amex", retailer_rates={"Amazon Business": "5%"})
        assert resolve_card("9999", [card], retailer="Amazon Business", default_rate=0.01)[1] == 0.05
        assert resolve_card("9999", [card], retailer="Costco", default_rate=0.01)[1] == 0.01

    @pytest.mark.parametrize("spelling", ["Best Buy", "bestbuy", "best-buy", "BEST BUY", "BestBuy"])
    def test_retailer_keys_match_regardless_of_spelling(self, spelling):
        # The ledger stores "Best Buy" but a config is just as likely to say "bestbuy" (the CLI name).
        # Punctuation must not quietly stop an override from applying.
        card = Card(last4="4321", name="C", cashback_rate=0.01, retailer_rates={spelling: "3%"})
        assert card.rate_for("Best Buy") == 0.03

    def test_lookup_side_is_normalized_too(self):
        assert self.card.rate_for("amazon") == 0.05

    def test_blank_retailer_uses_the_cards_overall_rate(self):
        assert self.card.rate_for("") == 0.015

    def test_percent_and_decimal_forms_both_work_in_the_map(self):
        assert self.card.retailer_rates == {"amazon": 0.05, "bestbuy": 0.03}

    def test_a_typoed_retailer_key_warns_at_load(self, tmp_path, monkeypatch, caplog):
        # A typo'd override never applies, and nothing else would ever say so — the row would just
        # quietly earn the card's overall rate and understate profit.
        path = tmp_path / "cards.json"
        path.write_text(json.dumps([
            {"last4": "4321", "name": "Freedom", "retailer_rates": {"Amazonn": "5%"}},
        ]), encoding="utf-8")
        monkeypatch.setattr(cards_module, "CARDS_FILE", path)

        with caplog.at_level("WARNING"):
            load_cards()

        assert "amazonn" in caplog.text

    def test_known_retailers_stays_in_sync_with_the_registered_scrapers(self):
        # KNOWN_RETAILERS is a hand-kept copy (config importing main would invert the dependency),
        # so this is the tripwire: adding Walmart must extend it, or its overrides warn spuriously.
        import main

        registered = {normalize_retailer(cls.retailer_name) for cls in main.SCRAPERS.values()}
        registered |= {normalize_retailer(key) for key in main.SCRAPERS}
        assert registered <= {normalize_retailer(n) for n in KNOWN_RETAILERS}


class TestTagCards:
    def test_tags_in_place_and_counts_unknown_cards(self):
        items = [
            item(card_last4="4321"),
            item(card_last4="ending in 9999"),  # real card, not configured
            item(card_last4=""),                # partial re-check
        ]
        unknown = tag_cards(items, [Card(last4="4321", name="Known", cashback_rate=0.05)],
                            default_rate=0.02)

        assert unknown == 1, "only the real-but-unconfigured card counts as a gap"
        assert (items[0].card_name, items[0].cashback_rate) == ("Known", 0.05)
        assert (items[1].card_name, items[1].cashback_rate) == ("", 0.02)
        assert (items[2].card_name, items[2].cashback_rate) == ("", None)

    def test_profile_scope_and_retailer_rate_both_come_from_the_row_itself(self):
        rows = [item(card_last4="1111", profile_label="profile-alpha", retailer="Best Buy")]
        tag_cards(
            rows,
            [Card(last4="1111", name="BB Card", profile="profile-alpha",
                  cashback_rate=0.01, retailer_rates={"Best Buy": "3%"})],
            default_rate=0.0,
        )
        assert (rows[0].card_name, rows[0].cashback_rate) == ("BB Card", 0.03)


class TestPromoCashback:
    """An Amazon order page can advertise a per-order bonus ("... plus an extra 1% back ..."), which
    the mapping hangs on the row and tag_cards ADDS to the card's own rate — cashback is deliberately
    one summed rate on the sheet, not two columns."""

    CARDS = [Card(last4="4321", name="Prime", cashback_rate=0.05)]

    def _row(self, promo, **kwargs):
        kwargs.setdefault("card_last4", "4321")
        kwargs.setdefault("retailer", "Amazon")
        row = item(**kwargs)
        row._promo_cashback_rate = promo
        return row

    def test_promo_is_added_to_the_cards_own_rate(self):
        rows = [self._row(0.01)]
        tag_cards(rows, self.CARDS, default_rate=0.0)
        assert rows[0].cashback_rate == 0.06

    def test_no_promo_leaves_the_rate_alone(self):
        rows = [self._row(None)]
        tag_cards(rows, self.CARDS, default_rate=0.0)
        assert rows[0].cashback_rate == 0.05

    def test_apply_promo_false_ignores_it(self):
        # AMAZON_PROMO_CASHBACK_ENABLED=0 — the escape hatch if the earn line proves to be card-level
        # marketing rather than a real per-order promo.
        rows = [self._row(0.01)]
        tag_cards(rows, self.CARDS, default_rate=0.0, apply_promo=False)
        assert rows[0].cashback_rate == 0.05

    def test_promo_rides_the_default_rate_for_an_unconfigured_card(self):
        rows = [self._row(0.01, card_last4="9999")]
        tag_cards(rows, self.CARDS, default_rate=0.02)
        assert rows[0].cashback_rate == 0.03

    def test_a_blank_card_stays_blank_even_with_a_promo(self):
        # A partial re-check carries no card; writing a promo-only rate here would defeat the blank
        # that lets ledger_sync._merge_row keep what the first full extraction recorded.
        rows = [self._row(0.01, card_last4="")]
        tag_cards(rows, self.CARDS, default_rate=0.02)
        assert rows[0].cashback_rate is None

    def test_other_retailers_are_unaffected(self):
        # Only the Amazon mapping sets the attribute, so a Best Buy row never has one.
        rows = [item(card_last4="4321", retailer="Best Buy")]
        tag_cards(rows, self.CARDS, default_rate=0.0)
        assert rows[0].cashback_rate == 0.05


class TestLoadCards:
    def test_missing_file_is_not_an_error(self, tmp_path, monkeypatch):
        # Same posture as warehouses.json: no config means no card names, never a crashed run.
        monkeypatch.setattr(cards_module, "CARDS_FILE", tmp_path / "nope.json")
        assert load_cards() == []

    def test_reads_the_example_shape(self, tmp_path, monkeypatch):
        path = tmp_path / "cards.json"
        path.write_text(json.dumps([
            {"last4": "4321", "name": "Freedom", "cashback_rate": "1.5%"},
            {"last4": "8765", "name": "Flat"},
        ]), encoding="utf-8")
        monkeypatch.setattr(cards_module, "CARDS_FILE", path)

        loaded = load_cards()

        assert [c.name for c in loaded] == ["Freedom", "Flat"]
        assert loaded[0].cashback_rate == 0.015
        assert loaded[1].cashback_rate is None

    def test_committed_example_file_actually_parses(self):
        # cards.example.json is what a user copies; a typo in it would break their first run.
        data = json.loads((cards_module.CARDS_FILE.parent / "cards.example.json").read_text("utf-8"))
        assert [Card.model_validate(entry).name for entry in data]
