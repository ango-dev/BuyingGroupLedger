import logging

from pydantic import BaseModel, Field, field_validator

log = logging.getLogger(__name__)

# The only status values the ledger understands. sheets.ledger_sync.load_order_state rolls an order
# up to "delivered" only when EVERY shipment row says "delivered", and treats anything unrecognized
# as still-open — so an out-of-vocabulary status silently keeps an order open forever.
STATUSES = ("ordered", "shipped", "delivered")

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
]


class OrderItem(BaseModel):
    retailer: str
    profile_label: str = ""
    order_id: str
    order_date: str  # YYYY-MM-DD, the date the order was placed
    status: str = "ordered"  # ordered | shipped | delivered (from the tracking page)
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
    total_cost: float | None = None
    card_last4: str = ""
    shipment: str = ""  # "Shipment 1" / "Shipment 2" / ...; "" only on pre-Shipment-column rows

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


class OrderExtractionResult(BaseModel):
    """Structured output schema handed to the Browser-Use agent."""

    logged_out: bool = False
    items: list[OrderItem] = Field(default_factory=list)
