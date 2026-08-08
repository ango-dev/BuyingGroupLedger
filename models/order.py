from pydantic import BaseModel, Field, field_validator

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
    # items split across shipments (e.g. same SKU in "Shipment Two" and "Shipment Three") don't
    # collide on the upsert key. "" for single-shipment / retailers without shipment grouping yet.
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
    shipment: str = ""  # shipment label within the order (e.g. "Shipment Two"); "" if single/unknown

    @field_validator("quantity", "cost_per_item", "shipping", "total_cost", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        # The agent may send "" (or whitespace) for numbers it skipped — treat as None, not 0.
        if isinstance(v, str) and v.strip() == "":
            return None
        return v


class OrderExtractionResult(BaseModel):
    """Structured output schema handed to the Browser-Use agent."""

    logged_out: bool = False
    items: list[OrderItem] = Field(default_factory=list)
