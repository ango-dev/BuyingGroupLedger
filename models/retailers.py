"""The retailers this ledger knows, spelled ONE way.

Two spellings live on: the scrapers' `retailer_key` (`amazon`, `amazon-business`, `bestbuy`,
`costco` -- the CLI argument and what the code compares), and the NAME the ledger's Retailer
column and every page show (`Amazon`, `Amazon Business`, `Best Buy`, `Costco`). config.json is
written with the names, by the Settings page and the wizard; the models accept any spelling
through `key_of`, so an older file with `bestbuy` or `best-buy` still loads.
"""

from __future__ import annotations

import re

__all__ = ["KEYS", "NAMES", "key_of", "name_of"]

#: retailer_key -> the name shown everywhere (the ledger's Retailer column spelling)
NAMES: dict[str, str] = {
    "amazon": "Amazon",
    "amazon-business": "Amazon Business",
    "bestbuy": "Best Buy",
    "costco": "Costco",
}
KEYS: tuple[str, ...] = tuple(NAMES)
_BY_NORMALIZED = {re.sub(r"[^a-z0-9]+", "", key): key for key in KEYS}


def key_of(text: str) -> str:
    """Any spelling of a known retailer -> its retailer_key ("Best Buy", "best-buy", "bestbuy" ->
    "bestbuy"); an unknown one comes back lower-cased and trimmed, so a typo stays visible."""
    normalized = re.sub(r"[^a-z0-9]+", "", (text or "").lower())
    return _BY_NORMALIZED.get(normalized, (text or "").strip().lower())


def name_of(key_or_name: str) -> str:
    """The shown name for any spelling ("bestbuy" -> "Best Buy"); an unknown one Title Cased."""
    key = key_of(key_or_name)
    return NAMES.get(key, (key_or_name or "").strip().replace("-", " ").title())
