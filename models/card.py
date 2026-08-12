import re

from pydantic import BaseModel, Field, field_validator, model_validator


def normalize_retailer(text: str) -> str:
    """Lowercase and strip non-alphanumerics, so a retailer written any reasonable way still matches.

    The ledger stores display names ("Best Buy", "Amazon Business") but a config file is just as
    likely to say "bestbuy" or "best-buy" — the same spelling the CLI takes. Both sides go through
    this ONE function so a per-retailer cashback rate can't fail to apply over punctuation.
    """
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def parse_rate(value):
    """Accept a rate as either a decimal fraction (0.02) or a percentage string ("2%") -> 0.02.

    Public because the sheet side needs the same rule: a percent-FORMATTED cell reads back as the text
    "4%", and comparing that against 0.04 must not look like a disagreement.
    """
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        if cleaned.endswith("%"):
            return float(cleaned[:-1].strip()) / 100
        return float(cleaned)
    return value


def _check_rate(rate, where: str) -> None:
    if rate is not None and not 0 <= rate <= 1:
        raise ValueError(
            f"{where} cashback rate {rate} is outside 0-1. Rates are decimal fractions — write 2% "
            'as 0.02 or "2%", not 2.'
        )


def normalize_last4(text: str) -> str:
    """Reduce whatever a retailer reports for a card to its bare last 4 digits.

    Retailers word this differently — Amazon renders "ending in 4321", Best Buy's ss-api sends a
    masked PAN like "************4321", Costco sends "xxxx4321" — and the agent fallback copies
    whatever the page said. Both the config's `last4` and the scraped `card_last4` go through this ONE
    function so the lookup can't miss on formatting alone. Returns "" when there aren't 4 digits.
    """
    digits = re.sub(r"\D", "", text or "")
    return digits[-4:] if len(digits) >= 4 else ""


class Card(BaseModel):
    """One credit card, matched to a ledger row by the last 4 digits the scraper already captures.

    Rates are stored as DECIMAL fractions (0.02 = 2%) because that's what the sheet's Total Profit
    formula multiplies by. The config may write either form — `0.02` or `"2%"` — and the validator
    normalizes. A bare `2` is REJECTED rather than guessed at: "2" is equally readable as 200% or 2%,
    and silently picking wrong would misstate every profit number on the sheet by 100x.

    TWO TIERS OF RATE, because a card's earn rate is category-dependent in real life:

    - `cashback_rate` — the card's OVERALL rate, used wherever no retailer-specific rate applies.
    - `retailer_rates` — per-retailer OVERRIDES, e.g. a card that earns 1.5% everywhere but 5% at
      Amazon. Keys are matched through `normalize_retailer`, so "Best Buy" / "bestbuy" / "best-buy"
      are the same key and punctuation can't quietly stop an override from applying.

    Omitting both means "use the global default" (settings.default_cashback_rate), so a wallet of
    flat-rate cards only needs names.

    `profile` is an optional scope for the one case last-4 alone can't resolve: two DIFFERENT cards,
    in different accounts, that happen to end in the same 4 digits. A scoped entry wins over an
    unscoped one; an unscoped entry matches every row (see config.cards.resolve_card). There is no
    retailer scope — one physical card is used at many retailers, and what varies per retailer is the
    RATE, which is what `retailer_rates` expresses.
    """

    last4: str
    name: str
    cashback_rate: float | None = None
    retailer_rates: dict[str, float] = Field(default_factory=dict)
    profile: str = ""  # matches ProfileConfig.label

    @field_validator("last4", mode="before")
    @classmethod
    def _clean_last4(cls, v):
        return normalize_last4(str(v)) if v is not None else v

    @field_validator("cashback_rate", mode="before")
    @classmethod
    def _percent_to_fraction(cls, v):
        return parse_rate(v)

    @field_validator("retailer_rates", mode="before")
    @classmethod
    def _normalize_retailer_rates(cls, v):
        if not isinstance(v, dict):
            return v
        return {normalize_retailer(k): parse_rate(rate) for k, rate in v.items()}

    @model_validator(mode="after")
    def _validate(self):
        if not self.last4:
            raise ValueError(
                f"Card {self.name or '(unnamed)'} has no usable last4; give it the 4 digits the "
                "retailer shows (e.g. \"4321\")."
            )
        _check_rate(self.cashback_rate, f"Card {self.name}'s")
        for retailer, rate in self.retailer_rates.items():
            _check_rate(rate, f"Card {self.name}'s {retailer}")
        return self

    def rate_for(self, retailer: str) -> float | None:
        """This card's rate at `retailer`: the per-retailer override if one is set, else the card's
        overall rate, else None (meaning "fall back to the global default")."""
        override = self.retailer_rates.get(normalize_retailer(retailer))
        return override if override is not None else self.cashback_rate

    def matches(self, last4: str, profile: str) -> bool:
        """True when this entry can describe a row's card. A blank `profile` means "any account"."""
        if self.last4 != last4:
            return False
        if self.profile and self.profile != profile:
            return False
        return True

    def specificity(self) -> int:
        """How many scope fields this entry pins down — the tie-breaker when several entries share a
        last4, so the profile-specific card beats the catch-all instead of file order deciding."""
        return bool(self.profile)
