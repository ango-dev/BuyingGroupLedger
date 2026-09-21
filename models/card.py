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

    Public because the ledger side needs the same rule: a percent-FORMATTED cell reads back as the text
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
    masked PAN like "************4321", Costco sends "xxxx4321". Both the config's `last4` and the scraped `card_last4` go through this ONE
    function so the lookup can't miss on formatting alone. Returns "" when there aren't 4 digits.
    """
    digits = re.sub(r"\D", "", text or "")
    return digits[-4:] if len(digits) >= 4 else ""


def _money(value) -> float:
    if isinstance(value, str):
        cleaned = value.strip().replace("$", "").replace(",", "")
        return float(cleaned) if cleaned else 0.0
    return float(value)


class CashbackCap(BaseModel):
    """A SPEND cap on a card's boosted rate: once the period's spend within the
    cap's scope passes `spend_limit`, purchases earn `fallback_rate` instead of the card's rate,
    and the purchase that crosses the line earns the exact blend (ledger/cashback_caps.py).

    - `retailers`: the scope -- these retailers share ONE allowance (Amazon Business Prime's 5% on
      $120k covers Amazon and Amazon Business together); EMPTY means the catch-all, every retailer
      without a cap of its own (Aven's 5% to $25k). Keys match through `normalize_retailer`.
    - `spend_limit`: dollars of spend at the boosted rate per period.
    - `fallback_rate`: the rate past the limit, a fraction or "1%" like every other rate.
    - `resets`: "calendar-year" (the default), "never", or an "MM-DD" the period starts on each
      year (a cardmember anniversary).
    - `outside_spend`: spend the ledger never sees -- personal purchases on the card -- per period
      ("2026": 4000; "all" for a cap that never resets), taken off the allowance first.
    """

    retailers: list[str] = Field(default_factory=list)
    spend_limit: float
    fallback_rate: float
    resets: str = "calendar-year"
    outside_spend: dict[str, float] = Field(default_factory=dict)

    @field_validator("retailers", mode="before")
    @classmethod
    def _normalize_retailers(cls, v):
        if isinstance(v, str):
            v = [part for part in re.split(r"[,;]", v)]
        return [normalize_retailer(r) for r in (v or []) if normalize_retailer(r)]

    @field_validator("spend_limit", mode="before")
    @classmethod
    def _limit(cls, v):
        return _money(v)

    @field_validator("fallback_rate", mode="before")
    @classmethod
    def _fallback(cls, v):
        return parse_rate(v)

    @field_validator("outside_spend", mode="before")
    @classmethod
    def _offsets(cls, v):
        if isinstance(v, str):
            pairs = {}
            for part in re.split(r"[;,](?=\s*[^,;:]+:)", v):  # "2026: 4,000, 2027: 0": the thousands comma stays
                if ":" in part:
                    period, amount = part.split(":", 1)
                    if period.strip():
                        pairs[period.strip()] = amount
            v = pairs
        return {str(k).strip(): _money(amount) for k, amount in (v or {}).items()}

    @model_validator(mode="after")
    def _validate(self):
        if self.spend_limit <= 0:
            raise ValueError(f"A cashback cap needs a spend limit above 0, not {self.spend_limit}.")
        _check_rate(self.fallback_rate, "A cashback cap's fallback")
        if self.fallback_rate is None:
            raise ValueError("A cashback cap needs a fallback rate (the rate past the limit).")
        resets = self.resets.strip().lower()
        if resets not in ("calendar-year", "never") and not re.fullmatch(r"(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])", resets):
            raise ValueError(
                f"A cashback cap's resets must be \"calendar-year\", \"never\" or an MM-DD, not {self.resets!r}.")
        self.resets = resets
        for period, amount in self.outside_spend.items():
            if amount < 0:
                raise ValueError(f"Outside spend for {period} is negative ({amount}).")
        return self

    def covers(self, retailer: str) -> bool:
        return normalize_retailer(retailer) in self.retailers


class Card(BaseModel):
    """One credit card, matched to a ledger row by the last 4 digits the scraper already captures.

    Rates are stored as DECIMAL fractions (0.02 = 2%) because that's what the ledger's Total Profit
    formula multiplies by. The config may write either form — `0.02` or `"2%"` — and the validator
    normalizes. A bare `2` is REJECTED rather than guessed at: "2" is equally readable as 200% or 2%,
    and silently picking wrong would misstate every profit number on the ledger by 100x.

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
    #: Spend caps on the boosted rate (CashbackCap): per retailer group and/or
    #: one catch-all. Which cap a purchase counts against is `cap_for`.
    caps: list[CashbackCap] = Field(default_factory=list)
    profile: str = ""  # matches ProfileConfig.label
    # A virtual card number (another card's, or a card with no account of its own): it earns
    # cashback like any card, but the Taxes page does not ask for a sign-up bonus for it.
    virtual: bool = False
    #: The last 4 of the card this virtual number belongs to: it earns that card's rates, its spend counts against that card's caps
    #: and those caps apply to it (config.cards.resolve_card, ledger/cashback_caps). The Settings
    #: page insists on it for a virtual card; an older config without it loads with a warning
    #: (config.cards.load_cards) and stands on its own rates.
    virtual_of: str = ""
    #: A virtual number with a sign-up bonus of its own -- an Amex employee card: it shares the
    #: card's rates and limits like any virtual number, but the Taxes page still asks for its bonus.
    #:Only meaningful with `virtual`.
    own_bonus: bool = False

    @field_validator("last4", mode="before")
    @classmethod
    def _clean_last4(cls, v):
        return normalize_last4(str(v)) if v is not None else v

    @field_validator("cashback_rate", mode="before")
    @classmethod
    def _percent_to_fraction(cls, v):
        return parse_rate(v)

    @field_validator("virtual_of", mode="before")
    @classmethod
    def _clean_virtual_of(cls, v):
        return normalize_last4(str(v)) if v else ""

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
        seen: dict[str, int] = {}
        catch_alls = 0
        for i, cap in enumerate(self.caps):
            if not cap.retailers:
                catch_alls += 1
            for r in cap.retailers:
                if r in seen:
                    raise ValueError(f"Card {self.name}: {r!r} is in two cashback caps (#{seen[r] + 1} and #{i + 1}).")
                seen[r] = i
        if catch_alls > 1:
            raise ValueError(f"Card {self.name} has {catch_alls} catch-all cashback caps; one at most.")
        if self.virtual_of:
            self.virtual = True
            if self.virtual_of == self.last4:
                raise ValueError(f"Card {self.name} cannot be a virtual number of itself.")
        return self

    def cap_for(self, retailer: str):
        """The cap a purchase at `retailer` counts against: the one naming it, else the catch-all,
        else None (no cap: the rate applies without limit)."""
        key = normalize_retailer(retailer)
        for cap in self.caps:
            if key in cap.retailers:
                return cap
        for cap in self.caps:
            if not cap.retailers:
                return cap
        return None

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
