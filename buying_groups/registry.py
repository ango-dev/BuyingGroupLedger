"""Which buying group a ledger row belongs to, and which client speaks to it.

The `Buying Group` column is already filled in by config/warehouses.py from the delivery address, so
routing is a lookup rather than a matching problem. The only wrinkle is spelling: the live
warehouses.json says **"MOD"** while warehouses.example.json says **"MaxOutDeals"**. Rather than
declaring one of them wrong and breaking whichever config disagrees, both fold to the same provider
through `_ALIASES` — the same tolerance config/cards.py applies to retailer names.

Adding a buying group means writing an adapter and adding one line here. Nothing in the planner, the
orchestrator or the ledger needs to change.
"""

import logging

from buying_groups.base import BuyingGroupClient, normalize_group
from buying_groups.bfmr import BFMRClient
from buying_groups.maxoutdeals import MaxOutDealsClient
from config.warehouses import DELIBERATELY_UNROUTED, PERSONAL, UNCLASSIFIED

__all__ = ["PROVIDERS", "ENABLED_SETTING", "get_client", "is_enabled", "resolve_group"]

log = logging.getLogger(__name__)

#: Canonical group key -> client class.
PROVIDERS: dict[str, type] = {
    "BFMR": BFMRClient,
    "MOD": MaxOutDealsClient,
}

#: Canonical group key -> the Settings field that switches it on and off. A new
#: provider adds its line here and a `<KEY>_ENABLED` setting beside its keys; one without a switch
#: is always on.
ENABLED_SETTING: dict[str, str] = {
    "BFMR": "bfmr_enabled",
    "MOD": "mod_enabled",
}


def is_enabled(group_key: str) -> bool:
    """Is this group switched on (Settings > Buying Groups > its dropdown)?"""
    from config.settings import settings

    field = ENABLED_SETTING.get(group_key)
    return True if field is None else bool(getattr(settings, field, True))


#: Every spelling that means a given provider, folded through normalize_group.
_ALIASES: dict[str, str] = {
    normalize_group("BFMR"): "BFMR",
    normalize_group("BuyForMeRetail"): "BFMR",
    normalize_group("MOD"): "MOD",
    normalize_group("MaxOutDeals"): "MOD",
    normalize_group("Max Out Deals"): "MOD",
}


def resolve_group(buying_group: str) -> str:
    """Map a `Buying Group` cell to a canonical provider key, or "" if it routes nowhere.

    Returns "" for `Unclassified`, `Personal`, `Gift Card`, a blank cell, and any group with no
    adapter. All are legitimately un-postable, and the caller reports the counts rather than guessing:
    an Unclassified row is a real warehouse someone forgot to configure, and silently posting it to
    whichever group happened to be first would be far worse than leaving it visible.

    NOTE the caller must still tell these apart — `Gift Card` is unrouted on purpose and silent, while
    a blank or Unclassified row with a tracking number is money about to be lost. See
    config.warehouses.is_deliberately_unrouted.
    """
    key = normalize_group(buying_group)
    unrouted = {normalize_group(UNCLASSIFIED), normalize_group(PERSONAL)}
    unrouted |= {normalize_group(t) for t in DELIBERATELY_UNROUTED}
    if not key or key in unrouted:
        return ""
    return _ALIASES.get(key, "")


def get_client(group_key: str, *, dry_run: bool = True) -> BuyingGroupClient:
    """Instantiate the adapter for a canonical group key from `resolve_group`."""
    try:
        return PROVIDERS[group_key](dry_run=dry_run)
    except KeyError:
        raise KeyError(
            f"No buying-group adapter for {group_key!r}; known: {sorted(PROVIDERS)}"
        ) from None
