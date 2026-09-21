import logging

from config.loader import config_section
from config.settings import settings
from models.card import Card, normalize_last4, normalize_retailer

__all__ = [
    "KNOWN_RETAILERS",
    "add_promos",
    "boosted_last4s",
    "load_cards",
    "normalize_last4",
    "resolve_card",
    "tag_cards",
]

log = logging.getLogger(__name__)

# Retailer names a `retailer_rates` key may refer to, in both the display and CLI spellings. A key
# that matches none of these is a TYPO, and a typo'd override would silently never apply — the row
# would quietly fall back to the card's overall rate and understate profit with no error anywhere.
# So load_cards warns about it. Kept here rather than imported from main.SCRAPERS (config importing
# main would invert the dependency); tests/test_cards.py asserts the two stay in sync.
KNOWN_RETAILERS = ("Amazon", "Amazon Business", "Best Buy", "Costco", "amazon-business", "bestbuy")  # models/retailers.py has the one spelling


def load_cards() -> list[Card]:
    """Load the credit-card config from config.json's `cards` section.

    Returns [] when the section is absent — every row then gets a blank Card name and the default
    cashback rate, which is the safe default (nothing is misattributed to the wrong card).
    """
    cards = [Card.model_validate(entry) for entry in config_section("cards")]
    _warn_about_unknown_retailer_rates(cards)
    _warn_about_unlinked_virtual_cards(cards)
    return cards


def _warn_about_unlinked_virtual_cards(cards: list[Card]) -> None:
    """A virtual number that names no card pools no spend with it; one naming a last4 no entry has pools nothing either."""
    known = {c.last4 for c in cards}
    for card in cards:
        if card.virtual and not card.virtual_of:
            log.warning("Card %r is virtual but names no card it belongs to (`virtual_of`): its spend "
                        "does not count against any cap. Set it on the Settings page.", card.name)
        elif card.virtual_of and card.virtual_of not in known:
            log.warning("Card %r is a virtual number of ...%s, which no `cards` entry has.",
                        card.name, card.virtual_of)
        elif card.virtual_of and any(c.last4 == card.virtual_of and c.virtual for c in cards):
            log.warning("Card %r is a virtual number of ...%s, itself a virtual number: it belongs to a real card.",
                        card.name, card.virtual_of)


def _warn_about_unknown_retailer_rates(cards: list[Card]) -> None:
    """Flag `retailer_rates` keys that name no real retailer — they'd never apply, silently."""
    known = {normalize_retailer(name) for name in KNOWN_RETAILERS}
    for card in cards:
        unknown = sorted({key for key in card.retailer_rates if key not in known}
                         | {r for cap in card.caps for r in cap.retailers if r not in known})
        if unknown:
            log.warning(
                "Card %r has retailer_rates for %s, which match no retailer this ledger scrapes "
                "(%s). Those rates will never apply — check the spelling.",
                card.name, unknown, ", ".join(KNOWN_RETAILERS[:4]),
            )


def boosted_last4s(retailer: str, cards: list[Card]) -> frozenset[str]:
    """The last-4s of cards carrying an EXPLICIT `retailer_rates` entry for this retailer.

    Such a card is a reselling card — you gave it a rate at this retailer on purpose — which is the
    signal the Amazon mappings use to decide that a gift-card reload bought on it is funding inventory
    rather than personal spending. A card with only an overall `cashback_rate`, or one missing from
    cards.json entirely, is not boosted.

    Deliberately keyed on the OVERRIDE EXISTING rather than on it beating the base rate: a card can be
    listed with a retailer rate and no overall rate at all (there is one in the live file), so there is
    nothing to compare against.
    """
    key = normalize_retailer(retailer)
    return frozenset(c.last4 for c in cards if key in c.retailer_rates)


def resolve_card(
    card_last4: str,
    cards: list[Card],
    profile_label: str = "",
    retailer: str = "",
    default_rate: float | None = None,
) -> tuple[str, float | None]:
    """Map a row's scraped card digits to `(card_name, cashback_rate)`.

    THE RATE IS RESOLVED IN THREE TIERS, most specific first:

        1. the card's `retailer_rates[retailer]`  — this card's rate AT this retailer
        2. the card's `cashback_rate`             — this card's overall rate
        3. `default_rate`                         — the global DEFAULT_CASHBACK_RATE from .env

    And the name/rate pair by case:

    - Blank/unreadable `card_last4` -> `("", None)`, NOT the default rate. A partial re-check carries
      no card, and blanks are what let ledger_sync._merge_row preserve whatever the first full
      extraction recorded. Writing the default rate here would overwrite a card-specific rate on
      every re-check.
    - A configured card -> its name, and its rate by the tiers above.
    - A REAL but unconfigured card (digits present, no entry) -> `("", default_rate)`. The name stays
      blank so the gap is visible on the ledger (and so a hand-typed name survives), while the default
      rate keeps Total Profit computable — this is exactly what the default rate is for.

    When several entries share a last4, the most specific scope wins (see Card.specificity), then
    file order. An unscoped duplicate is logged once per resolve so a genuinely ambiguous config
    doesn't silently attribute spend to the wrong card.

    `default_rate` overrides settings.default_cashback_rate (the .env value), which is what the
    callers use in practice — it's a parameter so the fallback is injectable rather than read from
    frozen global state.
    """
    if default_rate is None:
        default_rate = settings.default_cashback_rate
    last4 = normalize_last4(card_last4)
    if not last4:
        return "", None

    matches = [c for c in cards if c.matches(last4, profile_label)]
    if not matches:
        return "", default_rate

    matches.sort(key=lambda c: -c.specificity())
    best = matches[0]
    if len(matches) > 1 and matches[1].specificity() == best.specificity():
        log.warning(
            "Cards %s both match last4 %s for profile=%r; using %r. Add a `profile` scope to "
            "disambiguate.",
            [c.name for c in matches if c.specificity() == best.specificity()],
            last4, profile_label, best.name,
        )
    rate = _rates_of(best, cards).rate_for(retailer)  # a virtual number earns its card's rates
    return best.name, rate if rate is not None else default_rate


def _rates_of(card: Card, cards: list[Card]) -> Card:
    """The entry whose rates a purchase on `card` earns: the card it is a virtual number of,
   else itself."""
    if card.virtual_of:
        parents = [c for c in cards if c.last4 == card.virtual_of and (not c.profile or c.profile == card.profile)]
        if parents:
            parents.sort(key=lambda c: -c.specificity())
            return parents[0]
    return card


def tag_cards(items, cards: list[Card], default_rate: float | None = None,
              apply_promo: bool = True) -> int:
    """Fill each item's `card_name` / `cashback_rate` from its `card_last4`, in place.

    Returns the number of rows whose card digits were present but matched no configured card — the
    caller logs it, mirroring how the Unclassified warehouse count surfaces a config gap instead of
    letting it pass unnoticed.

    `apply_promo` (AMAZON_PROMO_CASHBACK_ENABLED) ADDS any per-order promo the scraper parsed off the
    order page — Amazon advertises "... plus an extra 1% back ..." under the payment method — on top of
    the card's configured rate, so the ledger's single Cashback Rate column carries the true total. Only
    the Amazon mapping sets it, so every other retailer is unaffected. A row with no resolvable rate
    (blank card_last4 -> None) is left alone: writing a promo-only rate there would defeat the blank
    that lets ledger_sync._merge_row preserve what an earlier full extraction recorded.
    """
    unknown = 0
    for item in items:
        name, rate = resolve_card(
            item.card_last4, cards, item.profile_label, item.retailer, default_rate
        )
        item.card_name = name
        item.cashback_rate = rate
        if normalize_last4(item.card_last4) and not name:
            unknown += 1
    if apply_promo:
        add_promos(items)
    return unknown


def add_promos(items) -> int:
    """The Amazon promo add-on, as its own step: main._tag_cards applies the cards' spend caps
    between the rate and the promo (ledger/cashback_caps.py), so the promo rides on top of a capped
    rate -- it is Amazon's, not the card's. Returns how many rows got one."""
    added = 0
    for item in items:
        promo = getattr(item, "promo_rate", None)
        if promo and item.cashback_rate is not None:
            item.cashback_rate = round(item.cashback_rate + promo, 4)
            added += 1
            log.info(
                "%s order %s: +%.2f%% promo cashback from the order page (rate now %.2f%%).",
                item.retailer, item.order_id, promo * 100, item.cashback_rate * 100,
            )
    return added
