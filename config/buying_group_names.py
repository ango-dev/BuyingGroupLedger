"""The buying groups the code has an adapter for, by canonical key, and every spelling of each.

Data only -- no client, no network -- so the dashboard can name the built-in groups (Settings >
Warehouses, user 2026-09-22: "those names are keyed to the actual running script") without
importing a buying-group client, which web/ never does (tests/test_web.py). buying_groups/registry.py
builds its alias table from ALIASES and refuses to import when PROVIDER_KEYS and its PROVIDERS
differ, so a new provider is added here and there together.
"""

from __future__ import annotations

from models.card import normalize_retailer

__all__ = ["ALIASES", "PROVIDER_KEYS", "canonical"]

#: Canonical group key -> every spelling that means it (the live config says "MOD", the example
#: "MaxOutDeals"; both fold to the same provider).
ALIASES: dict[str, tuple[str, ...]] = {
    "BFMR": ("BFMR", "BuyForMeRetail"),
    "MOD": ("MOD", "MaxOutDeals", "Max Out Deals"),
}

PROVIDER_KEYS: tuple[str, ...] = tuple(ALIASES)


def canonical(name: str) -> str:
    """The provider key a spelling means, or "" for any other name."""
    key = normalize_retailer(name or "")
    if not key:
        return ""
    for canon, spellings in ALIASES.items():
        if any(normalize_retailer(s) == key for s in spellings):
            return canon
    return ""
