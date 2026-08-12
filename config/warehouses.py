import json
from pathlib import Path

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

WAREHOUSES_FILE = Path(__file__).resolve().parent.parent / "warehouses.json"

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
    """Load the buying-group warehouse/jig config (see warehouses.example.json).

    Returns [] when warehouses.json is absent — then every non-blank address classifies as Unclassified,
    which is the safe default (nothing is silently called personal).
    """
    if not WAREHOUSES_FILE.exists():
        return []
    data = json.loads(WAREHOUSES_FILE.read_text(encoding="utf-8"))
    return [Warehouse.model_validate(entry) for entry in data]


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
        item.buying_group = classify_address(item.delivery_address, warehouses)
        if is_personal(item.buying_group):
            dropped_personal += 1
            continue
        if item.buying_group == UNCLASSIFIED:
            unclassified += 1
        kept.append(item)
    return kept, dropped_personal, unclassified
