from config.loader import config_section
from models.warehouse import Warehouse, normalize_address

__all__ = [
    "PERSONAL",
    "UNCLASSIFIED",
    "classify_address",
    "is_personal",
    "load_warehouses",
    "normalize_address",
    "tag_and_filter_personal",
]

# Tag for an address that matched no configured jig. Deliberately NOT "Personal": a real warehouse the
# user simply hasn't configured yet would then look like a personal order and be skipped by the future
# buying-group posting step — a missed reimbursement. "Unclassified" makes the gap visible instead.
UNCLASSIFIED = "Unclassified"

# The reserved group name for the user's own (consumer/reship) addresses. Rows classified as Personal are
# DROPPED before the ledger is written — the user doesn't want personal orders on the sheet at all. Only
# EXPLICIT matches are dropped; Unclassified (unknown) rows are kept so a not-yet-configured warehouse
# stays visible rather than silently disappearing.
PERSONAL = "Personal"


# The reserved group name for a purchase that is DELIBERATELY not a buying-group order — a gift card
# bought to fund a later order being the case this exists for. Such a row is real bookkeeping (it cost
# real money) but it will never route anywhere and will never be paid out, so it must be told apart
# from the two accidental non-routing tags above:
#
#   Unclassified  a warehouse someone forgot to configure       -> a gap to FIX, worth alerting on
#   Personal      the user's own address                        -> DROPPED, never reaches the ledger
#   Gift Card     deliberately not a resale                     -> KEPT, and silent
#
# Without the distinction a gift card shipped with a tracking number lands in sync_tracking's
# `unroutable_tracked`, which alerts on every run as though a reimbursement were about to be lost.
#
# The COST still counts: the scrapers net a gift card applied to an order OFF that order's cost (see
# amazon_mapping), so the card's own row carries the outlay exactly once and the totals stay right.
GIFT_CARD = "Gift Card"

#: Tags that route to no buying group ON PURPOSE. Widen this rather than special-casing "Gift Card"
#: in each consumer, if a second kind of non-resale purchase ever needs the same treatment.
DELIBERATELY_UNROUTED = (GIFT_CARD,)


def is_personal(buying_group: str) -> bool:
    return buying_group.strip().lower() == PERSONAL.lower()


def is_deliberately_unrouted(buying_group: str) -> bool:
    """Is this row unrouted BY DESIGN (a gift card), rather than through a config gap?

    The difference decides whether a shipped, unroutable row is an emergency or a non-event.
    """
    return (buying_group or "").strip().lower() in {t.lower() for t in DELIBERATELY_UNROUTED}


def load_warehouses() -> list[Warehouse]:
    """Load the buying-group warehouse/jig config from config.json's `warehouses` section.

    Returns [] when the section is absent — then every non-blank address classifies as Unclassified,
    which is the safe default (nothing is silently called personal).
    """
    return [Warehouse.model_validate(entry) for entry in config_section("warehouses")]


def classify_address(delivery_address: str, warehouses: list[Warehouse]) -> str:
    """Map a delivery address to its buying group.

    - Blank/whitespace address -> "" (NOT Unclassified): a partial re-check often carries no address,
      and a blank tag lets ledger_sync._merge_row preserve whatever was recorded earlier.
    - The first jig (in config order) whose required substrings ALL appear in the normalized address
      wins -> its buying_group.
    - A non-blank address matching no jig -> Unclassified.
    """
    if not delivery_address or not delivery_address.strip():
        return ""
    normalized = normalize_address(delivery_address)
    for warehouse in warehouses:
        for jig in warehouse.jigs:
            substrings = jig.required_substrings()
            if substrings and all(s in normalized for s in substrings):
                return warehouse.buying_group
    return UNCLASSIFIED


def tag_and_filter_personal(items, warehouses) -> tuple[list, int, int]:
    """Tag each item's buying_group from its delivery address and DROP personal rows.

    Mutates each item's `buying_group` in place, then returns `(kept, dropped_personal, unclassified)`:
    rows classified Personal are excluded from `kept` (the user doesn't want personal orders on the
    sheet); Unclassified rows are kept (and counted) so an unrecognized-but-real warehouse stays visible.
    Rows with a blank address (partial re-checks) classify to "" — kept, and _merge_row later preserves
    whatever tag was recorded on the first full extraction.
    """
    kept: list = []
    dropped_personal = 0
    unclassified = 0
    for item in items:
        # A DELIBERATE tag already on the item wins over the address. The gift-card purchase row the
        # Amazon mappings create carries `Gift Card` and no address at all; re-deriving from the
        # address would blank it, and a gift card that DID have an address would be classified into a
        # buying group it was never part of (or as Personal, and silently dropped).
        if is_deliberately_unrouted(item.buying_group):
            kept.append(item)
            continue
        item.buying_group = classify_address(item.delivery_address, warehouses)
        if is_personal(item.buying_group):
            dropped_personal += 1
            continue
        if item.buying_group == UNCLASSIFIED:
            unclassified += 1
        kept.append(item)
    return kept, dropped_personal, unclassified
