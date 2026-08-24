from config.loader import config_section
from models.warehouse import InsuranceAddress, Warehouse, normalize_address

__all__ = [
    "PERSONAL",
    "UNCLASSIFIED",
    "classify_address",
    "insurance_address_for",
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


def is_personal(buying_group: str) -> bool:
    return buying_group.strip().lower() == PERSONAL.lower()


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


def insurance_address_for(delivery_address: str, warehouses: list[Warehouse]):
    """The REAL warehouse address behind a jig, or None if there isn't one configured.

    A jig is a deliberately misspelled address variant — BFMR hands out "THIRTEEN SAMMPLE DR1VE"
    and friends so each order routes distinctly — so what the ledger records as the delivery address
    is a routing token, not somewhere the post office believes in. Insuring a package against it
    would put a fictional street on the policy.

    Matching reuses `classify_address`'s rule exactly (first jig in config order whose required
    substrings all appear) so routing and insuring can never disagree about which jig an address is.

    None is a SAFE answer, not a failure: the caller then sends no address at all, and BFMR falls
    back to the profile. That is the documented behaviour and the right default for an unmapped jig
    — better a profile address than a misspelled one.
    """
    if not delivery_address or not delivery_address.strip():
        return None
    normalized = normalize_address(delivery_address)
    for warehouse in warehouses:
        for jig in warehouse.jigs:
            substrings = jig.required_substrings()
            if substrings and all(s in normalized for s in substrings):
                return warehouse.insurance_addresses.get(jig.insure_as) if jig.insure_as else None
    return None


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
        item.buying_group = classify_address(item.delivery_address, warehouses)
        if is_personal(item.buying_group):
            dropped_personal += 1
            continue
        if item.buying_group == UNCLASSIFIED:
            unclassified += 1
        kept.append(item)
    return kept, dropped_personal, unclassified
