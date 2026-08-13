import logging
import re

from pydantic import BaseModel, Field, field_validator, model_validator

log = logging.getLogger(__name__)

# Matches the old "Shipment N" wording so it can be reduced to a bare "N". The label used to carry the
# word because the column didn't exist yet; under a column already headed "Shipment" it's redundant.
#Kept as a normalizer rather than a one-off migration because the AGENT fallback
# prompts still say "Shipment 1" — an LLM writing the labelled form must not create a second, differently
# keyed row for a shipment the deterministic path already recorded as "1".
_SHIPMENT_PREFIX = re.compile(r"^shipment\s*", re.IGNORECASE)


def shipment_label(number: int) -> str:
    """The canonical Shipment cell value for shipment `number` (1-based): a bare "1", "2", ...

    Every producer goes through this so the upsert key (which includes Shipment) can't drift between
    the deterministic paths, and normalize_shipment covers the agent path.
    """
    return str(number)


def normalize_shipment(value: str) -> str:
    """Reduce any accepted spelling of a shipment label to the bare number: "Shipment 2" -> "2".

    Leaves "" alone (legacy rows written before the column existed) and leaves anything unrecognized
    as-is rather than discarding it — an odd label is still a usable key, whereas dropping it would
    silently merge two shipments onto one row.
    """
    return _SHIPMENT_PREFIX.sub("", (value or "").strip()).strip()

# The status values the ledger understands. load_order_state treats TERMINAL_STATUSES as finished
# (the order drops out of future runs) and anything unrecognized as still-open — so an
# out-of-vocabulary status silently keeps an order open forever. "cancelled" only ever appears via a
# re-check: an order first seen as "ordered" that the order page later shows as cancelled. Brand-new
# orders that are already cancelled are ignored at discovery and never recorded.
#
# "paid" and "return" are the BUYING GROUP's outcomes, not the retailer's (added 2026-08-13; the
# buying-group sync began writing them the same day). No SCRAPER emits them — a retailer has no idea
# whether a group paid you — so sync_tracking.py is their only automatic source, and a hand-import may
# still set them directly. Both are TERMINAL, which is the point: the alternative (an unrecognized
# status) would re-open the order for re-checking on every run forever.
#
# Coverage differs by group, and the gap is deliberate rather than an oversight: BFMR reports both
# outcomes on its tracker, while MOD confirms payment only by listing a package as received and
# publishes NO return signal at all — so a returned MOD package must be set to "return" by hand.
# ledger_sync._STATUS_RANK is what protects that edit: status only ever moves FORWARD, so MOD's
# endless "still received" reports cannot walk it back to "paid".
#
# NOTE "paid" overlaps the Payout Amount column, which records the same fact more precisely. Prefer
# filling Payout Amount when you have it. Setting Status to "paid" on a row that has NOT been
# delivered also discards its shipment state, so only use it on an order that already finished.
STATUSES = ("ordered", "shipped", "delivered", "cancelled", "paid", "return")
TERMINAL_STATUSES = ("delivered", "cancelled", "paid", "return")

# CSV/Sheet column order — keep in sync with output/csv_writer.py and sheets/ledger_sync.py HEADER,
# which is this same list in display-name form, positionally 1:1. tests/test_schema.py pins BOTH.
#
# ORDER IS READING ORDER, chosen by the user (2026-08-12): identity first (when/what/where), then the
# money columns left-to-right in the order you reason about them (cost -> cashback -> payout ->
# profit), then reference/audit columns you rarely scan, parked at the end.
#
# CHANGING THIS ORDER IS A MIGRATION, NOT AN EDIT. Rows are written to the sheet POSITIONALLY from
# column A, so reordering here without rewriting the existing rows silently scrambles every one of
# them. This list was reordered ONCE (2026-08-12) and `scripts/reorder_sheet.py` conformed the live
# sheet to it — it remaps by column NAME, so it also covers any future reorder. Still, the cheap and
# preferred way to add a column stays APPENDING at the end: existing rows just gain a trailing blank
# and no migration is needed.
FIELDNAMES = [
    # --- identity: what was bought, when, and on whose account ---
    "order_date",
    "status",
    "profile_label",
    "retailer",
    "item_name",
    "quantity",
    "order_id",
    "tracking_number",
    # Distinguishes shipments of one order so identical items split across shipments (same SKU in
    # shipment 1 and 2) don't collide on the upsert key. A BARE NUMBER ("1", "2", ...) — the old
    # "Shipment 1" wording was redundant under a column already headed "Shipment". Every retailer
    # numbers from 1, single included; "" only on legacy rows written before this column existed.
    # OrderItem normalizes any "Shipment N" input down to "N" (see _normalize_shipment).
    "shipment",
    "delivery_date",
    # --- money: cost in, cashback + payout back, profit out ---
    "cost_per_item",
    # Every scraper/agent emits the ORDER-LEVEL shipping total, repeated on every shipment row (see
    # OrderItem.shipping below) — sheets.ledger_sync.sync_csv_to_sheet is what turns that into each
    # row's actual cost-weighted SHARE before it lands on the sheet, so this field's value in a CSV
    # and its value in the ledger are deliberately NOT the same number.
    "shipping",
    "total_cost",
    # Derived from card_last4 at run time (main.run_scrape -> config.cards.tag_cards): the friendly
    # card name and the cashback rate that applies to this row. Both blank when card_last4 is blank
    # (a partial re-check), so _merge_row preserves what the first full extraction recorded.
    "card_name",
    "cashback_rate",
    # Filled by the buying-group sync (sync_tracking.py), or by hand. The scrapers always emit these
    # blank, and _merge_row's blank-never-overwrites rule is what keeps a re-scrape from wiping
    # numbers typed into the sheet by hand.
    # `insurance` is filled for BFMR from the negative FEE row on its tracker (its two documented
    # insurance-READ endpoints are documented but NOT DEPLOYED), and written as 0 for MOD, which
    # never charges a premium.
    "insurance",
    "payout_amount",
    "payout_date",
    # DERIVED IN THE SHEET, not here: sheets.ledger_sync writes a live formula into this cell so the
    # number updates the moment insurance/payout are typed in — a Python-computed value would go
    # stale, and a delivered row is never re-scraped to refresh it. Kept in FIELDNAMES (emitted blank)
    # so the column still exists positionally in the CSV and the sheet row.
    "total_profit",
    # Closes the money block: which buying group this row's payout is coming from. DERIVED from
    # delivery_address by config.warehouses.classify_address at run time (in main.run_scrape).
    "buying_group",
    # --- reference / audit: rarely scanned, so parked at the end ---
    "order_url",
    "tracking_url",
    # The raw address buying_group was classified from — kept back here with the other reference data
    # rather than beside its tag, since it's long, wraps badly, and is only consulted when a
    # classification looks wrong.
    "delivery_address",
    "card_last4",
    "last_scraped_at",
    # Has this row's tracking number been accepted by its buying group? A real BOOLEAN, so the
    # column works as a Google Sheets checkbox.
    #
    # A DELIBERATE EXCEPTION to this project's "derive, don't store" rule. Whether a number has been
    # submitted is something the group knows and is re-derived every run, which is why
    # sync_tracking.py does NOT consult this column to decide what to send — a local mirror of remote
    # state drifts the moment a post succeeds and the sheet write doesn't. It exists to be SEEN: an
    # unticked box next to a shipped package is the thing worth noticing. It is a display of state,
    # not a source of truth. Blank on every scraper path, so _merge_row preserves it.
    "tracking_submitted",
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
    shipping: float | None = None  # ORDER-LEVEL total as emitted; ledger_sync reprorates it for the sheet
    total_cost: float | None = None  # computed = quantity * cost_per_item (this row/shipment line)
    card_last4: str = ""
    shipment: str = ""  # bare number: "1" / "2" / ...; "" only on pre-Shipment-column rows
    buying_group: str = ""  # derived from delivery_address; "Unclassified" if no jig matched, "" if blank

    # Derived from card_last4 by config.cards.tag_cards; blank when card_last4 is blank.
    card_name: str = ""
    cashback_rate: float | None = None  # decimal fraction (0.02 = 2%)
    # User-entered / BFMR-filled. Always blank from a scraper — see FIELDNAMES.
    insurance: float | None = None
    payout_date: str = ""
    payout_amount: float | None = None
    # Always blank from here; sheets.ledger_sync writes a live formula into the cell instead.
    total_profit: float | None = None
    # Always blank from a scraper; sync_tracking.py ticks it when a buying group accepts the tracking
    # number. Typed as a string, not a bool, precisely so the scrapers' blank survives _merge_row —
    # a default of False would tick nothing but would overwrite a real True on every re-scrape.
    tracking_submitted: str = ""

    @field_validator("quantity", "cost_per_item", "shipping", "total_cost",
                     "cashback_rate", "insurance", "payout_amount", "total_profit", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        # The agent may send "" (or whitespace) for numbers it skipped — treat as None, not 0.
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @field_validator("delivery_address", mode="before")
    @classmethod
    def _flatten_address(cls, v):
        """Collapse a multi-line address into one comma-separated line.

        The deterministic parsers already comma-join the address block, but the AGENT copies the page
        text verbatim, so an agent-written row could land with real newlines inside the cell — which
        makes the sheet row tall and ragged. Normalizing here rather than in each prompt keeps the two
        paths writing the same shape, and can't drift the way prompt wording does.

        Classification is unaffected either way (config.warehouses.normalize_address already reduces
        every non-alphanumeric run to a single space), so this is purely about how the cell reads.
        """
        if not isinstance(v, str):
            return v
        parts = [" ".join(line.split()).rstrip(",;") for line in v.splitlines()]
        return ", ".join(p for p in parts if p)

    @field_validator("shipment", mode="before")
    @classmethod
    def _normalize_shipment(cls, v):
        """Strip the redundant "Shipment " wording so the cell reads "2", not "Shipment 2".

        Enforced HERE, on every path, rather than only where the deterministic mappings build labels:
        the agent-fallback prompts still describe shipments as "Shipment 1"/"Shipment 2" (the labelled
        form reads unambiguously to an LLM), and Shipment is part of the upsert key — so an agent
        re-check emitting "Shipment 2" for a row the API recorded as "2" would append a duplicate
        instead of updating it.
        """
        return normalize_shipment(v) if isinstance(v, str) else v

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
