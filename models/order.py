import logging

from pydantic import BaseModel, Field, field_validator, model_validator

log = logging.getLogger(__name__)

# The status values the ledger understands. load_order_state treats delivered and cancelled as
# TERMINAL (the order drops out of future runs) and anything unrecognized as still-open — so an
# out-of-vocabulary status silently keeps an order open forever. "cancelled" only ever appears via a
# re-check: an order first seen as "ordered" that the order page later shows as cancelled. Brand-new
# orders that are already cancelled are ignored at discovery and never recorded.
STATUSES = ("ordered", "shipped", "delivered", "cancelled")
TERMINAL_STATUSES = ("delivered", "cancelled")

# CSV/Sheet column order — keep in sync with output/csv_writer.py and sheets/ledger_sync.py
FIELDNAMES = [
    "retailer",
    "profile_label",
    "order_id",
    "order_date",
    "status",
    "order_url",
    "tracking_number",
    "tracking_url",
    "delivery_date",
    "delivery_address",
    "item_name",
    "quantity",
    "cost_per_item",
    "shipping",
    "total_cost",
    "card_last4",
    "last_scraped_at",
    # Appended LAST on purpose: a mid-list insert would misalign existing sheet rows (the sync
    # writes rows positionally from column A). Distinguishes shipments of one order so identical
    # items split across shipments (e.g. same SKU in "Shipment 1" and "Shipment 2") don't collide
    # on the upsert key. Every retailer numbers shipments "Shipment N" from 1, single included;
    # "" only on legacy rows written before this column existed.
    "shipment",
    # Appended LAST (after shipment) on purpose, same positional-write reasoning: older sheets gain a
    # trailing empty cell and no existing row shifts. Derived from delivery_address by
    # config.warehouses.classify_address at run time (in main.run_scrape): the buying group whose
    # warehouse/jig this shipment went to, "Personal" if configured, "Unclassified" if it matched no
    # jig, or "" when the address is blank (a partial re-check) so _merge_row preserves the earlier tag.
    "buying_group",
]


class OrderItem(BaseModel):
    retailer: str
    profile_label: str = ""
    order_id: str
    order_date: str  # YYYY-MM-DD, the date the order was placed
    status: str = "ordered"  # ordered | shipped | delivered | cancelled
    order_url: str = ""  # direct URL to the order details page (for fast re-visits)
    tracking_number: str = ""
    tracking_url: str = ""  # direct URL to the tracking page (for fast re-visits)
    delivery_date: str = ""  # est. arrival date if shipped, actual date if delivered, else ""
    delivery_address: str = ""
    item_name: str
    # Numeric fields are optional/None so an omitted value (e.g. on a tracking-only re-check)
    # serializes blank in the CSV and never clobbers an already-recorded number in the sheet.
    quantity: int | None = None
    cost_per_item: float | None = None
    shipping: float | None = None
    total_cost: float | None = None  # computed = quantity * cost_per_item (this row/shipment line)
    card_last4: str = ""
    shipment: str = ""  # "Shipment 1" / "Shipment 2" / ...; "" only on pre-Shipment-column rows
    buying_group: str = ""  # derived from delivery_address; "Unclassified" if no jig matched, "" if blank

    @field_validator("quantity", "cost_per_item", "shipping", "total_cost", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        # The agent may send "" (or whitespace) for numbers it skipped — treat as None, not 0.
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @field_validator("status", mode="before")
    @classmethod
    def _normalize_status(cls, v):
        """Coerce the agent's status into the known vocabulary — never reject the row.

        Rejecting would raise out of OrderExtractionResult.model_validate_json and kill the whole
        profile's run for that cycle over one odd word, losing every other order in the batch. A
        mislabeled order costs far less than a missed one, so an unrecognized value falls back to
        "ordered" (the safe end: the order stays open and keeps getting re-checked) and logs a
        warning, which is the signal that the prompt needs tightening.
        """
        if not isinstance(v, str):
            return v
        cleaned = v.strip().lower()
        if cleaned in STATUSES:
            return cleaned
        if cleaned == "":
            return "ordered"
        log.warning("Unrecognized status %r from agent; treating as 'ordered'.", v)
        return "ordered"

    @model_validator(mode="after")
    def _shipped_requires_tracking(self):
        """A row may only be 'shipped' if it actually carries a tracking number.

        'shipped' is the claim that a package is in transit; with no tracking number there is no
        proof of that, and nothing downstream (buying-group posting) can act on it. Amazon's agent
        judges a shipment 'shipped' from the order-details "Arriving <date>" text but deliberately
        leaves tracking_number blank — the number lives on the tracking page, read separately by the
        cheaper CDP pass — which would otherwise land a 'shipped' row with no number. Downgrade to
        'ordered' (the safe open state) until a number is present; the CDP re-check then promotes it
        back to 'shipped' once it reads the number. delivered/cancelled are terminal and untouched.
        Best Buy already enforces this via prompt wording; doing it here makes the rule uniform and
        deterministic across every retailer and every path (agent, CDP) that builds an OrderItem.
        """
        if self.status == "shipped" and not self.tracking_number.strip():
            self.status = "ordered"
        return self

    @model_validator(mode="after")
    def _compute_total_cost(self):
        """total_cost is the line total for THIS shipment row = quantity * cost_per_item.

        Computed here rather than trusted from the agent, which used to report the ORDER grand total
        on every row (so the column couldn't be summed). Left untouched when either factor is absent
        — e.g. a tracking-only re-check sends both blank, and _merge_row then preserves whatever was
        already recorded. Shipping is a separate column and is not folded in.
        """
        if self.quantity is not None and self.cost_per_item is not None:
            self.total_cost = round(self.quantity * self.cost_per_item, 2)
        return self


class OrderExtractionResult(BaseModel):
    """Structured output schema handed to the Browser-Use agent."""

    logged_out: bool = False
    items: list[OrderItem] = Field(default_factory=list)
