import logging
import re

from pydantic import BaseModel, PrivateAttr, field_validator, model_validator

log = logging.getLogger(__name__)

# Matches the old "Shipment N" wording so it can be reduced to a bare "N". The label used to carry the
# word because the column didn't exist yet; under a column already headed "Shipment" it's redundant.
#Kept as a normalizer rather than a one-off migration because imports and
# hand-entered rows still arrive in the labelled form — writing it must not create a second,
# differently keyed row for a shipment already recorded as "1".
_SHIPMENT_PREFIX = re.compile(r"^shipment\s*", re.IGNORECASE)


def shipment_label(number: int) -> str:
    """The canonical Shipment cell value for shipment `number` (1-based): a bare "1", "2", ...

    Every producer goes through this so the upsert key (which includes Shipment) can't drift
    between the deterministic paths; normalize_shipment covers everything else (imports, hand edits).
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
# NOTE "paid" overlaps the Actual Payout column, which records the same fact more precisely. Prefer
# filling Actual Payout when you have it. Setting Status to "paid" on a row that has NOT been
# delivered also discards its shipment state, so only use it on an order that already finished.
#
# "superseded" (2026-09-09, the design notes) is a shipment row whose tracking number Amazon RE-ISSUED
# for the same delayed package: the dead label was really posted to a buying group, so the row is
# kept as the record of that instead of being deleted. Only scripts/fix_superseded_shipments.py
# ever writes it. It is TERMINAL for the usual reason (a non-terminal status re-opens the order
# forever), RETIRED (never a merge target for a future box, never submitted, insured or handed a
# payout — sync_tracking and ledger_sync's upsert both skip it) and MONEY-FREE (every amount cell
# blank, Quantity included, so the re-labelled package's cost can never be counted twice; the audit's
# superseded_rows_carry_no_money check enforces it).
STATUSES = ("ordered", "shipped", "delivered", "cancelled", "paid", "return", "superseded")
TERMINAL_STATUSES = ("delivered", "cancelled", "paid", "return", "superseded")
# Never a merge target and never touched by the buying-group sync: the row is a closed record.
RETIRED_STATUSES = ("superseded",)
# Every amount cell blank -- see ledger/sync._BLANK_FIELDS_BY_STATUS for which cells, since
# the two differ (a cancelled row keeps Quantity as "how many were ordered"; a superseded row does
# not, because Quantity is the multiplier that double-counted the re-labelled package).
MONEY_FREE_STATUSES = ("cancelled", "superseded")

# WHAT A CAPTURE MUST READ (2026-09-19). A retailer page or payload that stopped yielding a cell it
# used to yield is a SHAPE CHANGE -- the thing this app breaks on -- and it must leave a failure
# dossier, never a quiet blank. Two tiers:
#   IDENTITY fields are the upsert key (Order ID + Order Date + Item Name). A blank one cannot be
#   recorded at all, so each mapping RAISES its shape error: nothing recorded that run, dossier +
#   alert, the next run retries.
#   MANDATORY fields are recorded blank (a blank never overwrites, and the next re-read of an open
#   order may fill the cell) but every blank is a dossier PROBLEM reported by the retailer client
#   with the page or payload attached -- see diagnostics.report_unreadable_rows and the FIELD_SOURCES
#   table each mapping declares (which selector / JSON path the cell is read from).
# A cancelled or superseded row carries no money and is exempt; a gift-card row has no package, so
# it needs no address; a Quantity of "*" is the undisclosed-split marker, deliberate, not a gap.
# scripts/audit_ledger.py's mandatory_by_stage checks the same cells on the ledger afterwards;
# tests/test_capture_mandatory.py pins that every field here is on its lists too.
CAPTURE_IDENTITY_FIELDS = ("order_id", "order_date", "item_name")
CAPTURE_MANDATORY_FIELDS = ("quantity", "cost_per_item", "delivery_address", "card_last4")
CAPTURE_FIELD_LABELS = {
    "order_id": "Order ID", "order_date": "Order Date", "item_name": "Item Name",
    "quantity": "Quantity", "cost_per_item": "Cost Per Item",
    "delivery_address": "Delivery Address", "card_last4": "Card Last 4",
}


def unreadable_fields(row) -> list[str]:
    """The CAPTURE_MANDATORY_FIELDS this row could not read (field names, in that order), or []
    when the row is complete or exempt (money-free status, or a gift-card row's address)."""
    from config.warehouses import is_deliberately_unrouted  # local: config imports models

    if (row.status or "").lower() in MONEY_FREE_STATUSES:
        return []
    gift_card = is_deliberately_unrouted(row.buying_group or "")
    gaps = []
    for field in CAPTURE_MANDATORY_FIELDS:
        if field == "delivery_address" and gift_card:
            continue
        value = getattr(row, field, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            gaps.append(field)
    return gaps

# CSV/ledger column order — keep in sync with output/csv_writer.py and ledger/sync.py HEADER,
# which is this same list in display-name form, positionally 1:1. tests/test_schema.py pins BOTH.
#
# ORDER IS READING ORDER, chosen by the user (2026-08-12): identity first (when/what/where), then the
# money columns left-to-right in the order you reason about them (cost -> cashback -> payout ->
# profit), then reference/audit columns you rarely scan, parked at the end.
#
# CHANGING THIS ORDER IS A MIGRATION, NOT AN EDIT. Rows are written to the ledger POSITIONALLY from
# column A, so reordering here without rewriting the existing rows silently scrambles every one of
# them. The ledger file migrates its table by column NAME on open (ledger_db/store.py), which
# covers a reorder; the formula letters in tests/test_profit_formula.py are pinned. Still, the cheap and
# preferred way to add a column stays APPENDING at the end: existing rows just gain a trailing blank
# and no migration is needed.
FIELDNAMES = [
    # --- identity: what it is ---
    "order_date",
    "status",
    "retailer",
    "item_name",
    # Distinguishes shipments of one order so identical items split across shipments (same SKU in
    # shipment 1 and 2) don't collide on the upsert key. A BARE NUMBER ("1", "2", ...) — the old
    # "Shipment 1" wording was redundant under a column already headed "Shipment". Every retailer
    # numbers from 1, single included; "" only on legacy rows written before this column existed.
    # OrderItem normalizes any "Shipment N" input down to "N" (see _normalize_shipment).
    "shipment",
    "quantity",
    # --- what happened to it: the shipment's own story, in the order it happens ---
    # Opens the shipment block: the order's own identifier, immediately before the shipment's
    # (tracking_number) and whether that shipment was handed over (tracking_submitted). Also part
    # of the upsert key -- see the module note above.
    "order_id",
    "tracking_number",
    # Has this row's tracking number been accepted by its buying group? A real BOOLEAN, so the
    # column works as a checkbox.
    #
    # A DELIBERATE EXCEPTION to this project's "derive, don't store" rule. Whether a number has been
    # submitted is something the group knows and is re-derived every run, which is why
    # sync_tracking.py does NOT consult this column to decide what to send — a local mirror of remote
    # state drifts the moment a post succeeds and the ledger write doesn't. It exists to be SEEN: an
    # unticked box next to a shipped package is the thing worth noticing. It is a display of state,
    # not a source of truth. Blank on every scraper path, so _merge_row preserves it.
    "tracking_submitted",
    "delivery_date",
    # Closes the shipment story: which buying group this package went to. It is also the routing
    # key sync_tracking.py submits on, which is why it sits with the tracking columns rather than
    # with the payout it eventually produces. DERIVED from delivery_address by
    # config.warehouses.classify_address at run time (in main.run_scrape).
    "buying_group",
    # --- money: what it cost -> what the card gave back -> COGS -> what came back -> profit ---
    "cost_per_item",
    "total_cost",
    # Every scraper emits the ORDER-LEVEL shipping total, repeated on every shipment row (see
    # OrderItem.shipping below) — ledger.sync.sync_csv_to_ledger is what turns that into each
    # row's actual cost-weighted SHARE before it lands on the ledger, so this field's value in a CSV
    # and its value in the ledger are deliberately NOT the same number.
    "shipping",
    # Both are the ORDER-LEVEL total repeated on every row by the mappings (the same contract as
    # `shipping` above); ledger_sync reprorates each into the row's cost-weighted share, and the COGS
    # formula reads the shares: sales tax is ADDED (a real acquisition cost; usually 0 under the
    # resale certificate, but hand-kept orders pay it), gift card is SUBTRACTED (a tender the card
    # never spent, so it earns no cashback and isn't our cost — the gift-card purchase has its own
    # row). They sit here, beside shipping, because all three are the same kind of number: an
    # order-level adjustment to what this row actually cost.
    "sales_tax",
    "gift_card",
    # Amazon rewards SPENT on the order — a Prime cash-back balance or Amazon points — as an
    # order-level total like `gift_card` above, and placed beside it for that reason. Deliberately
    # NOT a gift card: the user nets every Amazon reward out of COGS at year end, outside the ledger,
    # so the ledger must keep the order's FULL cost (as if the card paid it all) or the reward is
    # counted twice. What this column changes is only the cashback basis: the card earns nothing on
    # dollars it never paid.
    "rewards_used",
    # Derived from card_last4 at run time (main.run_scrape -> config.cards.tag_cards): the friendly
    # card name and the cashback rate that applies to this row. Both blank when card_last4 is blank
    # (a partial re-check), so _merge_row preserves what the first full extraction recorded.
    "card_name",
    "cashback_rate",
    # Added 2026-09-20 BESIDE Cashback Rate -- a migration, not an append: the ledger file
    # rebuilds its table by column name on open (ledger_db/store.py) and the formula letters follow
    # HEADER. The Amazon promo the order page advertises ("... plus an extra 1% back ...") on its
    # own, as the mapping read it; Cashback Rate stays the TOTAL the card earns (card rate, capped,
    # plus this). Kept apart so a spend cap's recompute (ledger/cashback_caps.py) can take it off,
    # judge the remainder against the cap and put it back. Blank = no promo.
    "promo_rate",
    # DERIVED IN THE LEDGER like total_profit below, and for the same reason. Cost of Goods Sold for
    # this row, net of the card rebate:
    #
    #     COGS = (total_cost + this row's SHARE of shipping) * (1 - cashback_rate)
    #
    # Cashback is netted into COST here rather than counted as income, because card rewards earned on
    # a purchase are a purchase-price adjustment, not receipts — which is the characterisation a
    # Schedule C wants. `insurance` is deliberately NOT in here: a buying-group premium is an ordinary
    # business expense, not part of the cost of the goods, and folding it in would misreport both.
    #
    # UNLIKE total_profit this is populated whenever total_cost exists — it must NOT blank out on a
    # row that hasn't paid out yet, because the cost was incurred regardless and the year-end cost
    # side has to count it.
    "cogs",
    # Filled by the buying-group sync (sync_tracking.py), or by hand. The scrapers always emit these
    # blank, and _merge_row's blank-never-overwrites rule is what keeps a re-scrape from wiping
    # numbers typed into the ledger by hand.
    # `insurance` is filled for BFMR from the negative FEE row on its tracker (its two documented
    # insurance-READ endpoints are documented but NOT DEPLOYED), and written as 0 for MOD, which
    # never charges a premium.
    "insurance",
    # The buying group's COMMITTED payout for this row (BFMR's tracker price, prorated by Total
    # Cost), written by sync_tracking the moment the purchase links the order and left alone once
    # the row settles -- beside the ACTUAL payout so the two read together and the dashboard's
    # Reconciliation page can compare them. Column added 2026-09-18 (last), moved here the same
    # day: the store migrates its table by name on open. Always blank from a
    # scraper; excluded from both formulas.
    "expected_payout",
    "payout_amount",
    "payout_date",
    # --- returns: a PARTIAL return is ONE hand edit on the original
    # row, never a second negative row. Quantity / Total Cost keep the GROSS bought values the
    # scraper wrote; the COGS formula reads return_quantity and nets the returned units out of the
    # cost basis itself (see ledger.sync._cogs_formula), which sums to exactly what the old
    # two-row bookkeeping did. A fully-returned order keeps status `return`.
    "return_quantity",
    "return_date",
    # DERIVED IN THE LEDGER, not here: the adapter computes this cell from the row on every read, so
    # the number is current the moment insurance/payout are typed in — a scrape-time value would go
    # stale, and a delivered row is never re-scraped to refresh it. Kept in FIELDNAMES (emitted blank)
    # so the column still exists positionally in the CSV and the row.
    "total_profit",
    # --- reference / audit: rarely scanned, so parked at the end ---
    # Which browser profile scraped the row. A scraper detail, never read while reconciling, so it
    # sits with the reference columns rather than taking a place near the front.
    "profile_label",
    "order_url",
    "tracking_url",
    # A link to this ORDER's captured receipt in object storage (receipts/). One document per order,
    # so every row of a multi-item / multi-shipment order carries the same link — the receipt covers
    # the whole order, and duplicating the link is what makes it reachable from whichever row you
    # happen to be looking at.
    #
    # Filled by receipts.capture.attach_receipts during the scrape and blank when receipt capture is
    # unconfigured, so _merge_row's blank-never-overwrites rule keeps a link already on the ledger.
    "receipt_url",
    # The raw address buying_group was classified from — kept back here with the other reference data
    # rather than beside its tag, since it's long, wraps badly, and is only consulted when a
    # classification looks wrong.
    "delivery_address",
    "card_last4",
    # Added 2026-09-09 (history §1f follow-up), moved beside Card Last 4 on 2026-09-10. The retailer's OWN identity for the physical
    # package this row is part of — Amazon's `shipmentId` (from the card's track link, or the
    # /your-orders/pop link once the track link has expired), Costco's `packageNumber` (the carton's
    # SSCC-style id, not the carrier label), Best Buy's `fulfillmentGroups[].groupId` (a per-order
    # ordinal, unique only within the order). Text, never a number: Costco ids carry leading zeros.
    # ledger_sync matches on (Order ID, Package ID) BEFORE the tracking number, so a package lands on
    # its own row regardless of where its card sits on the page (Shipment is a DOM ordinal there).
    # Blank means unknown (an unshipped line, an old order whose links expired) and never blocks a
    # match.
    "package_id",
    "last_scraped_at",
]


class OrderItem(BaseModel):
    retailer: str
    profile_label: str = ""
    order_id: str
    order_date: str  # YYYY-MM-DD, the date the order was placed
    status: str = "ordered"  # one of STATUSES above
    order_url: str = ""  # direct URL to the order details page (for fast re-visits)
    tracking_number: str = ""
    tracking_url: str = ""  # direct URL to the tracking page (for fast re-visits)
    delivery_date: str = ""  # est. arrival date if shipped, actual date if delivered, else ""
    delivery_address: str = ""
    item_name: str
    # Numeric fields are optional/None so an omitted value (e.g. on a tracking-only re-check)
    # serializes blank in the CSV and never clobbers an already-recorded number in the ledger.
    quantity: int | None = None
    cost_per_item: float | None = None
    shipping: float | None = None  # ORDER-LEVEL total as emitted; ledger_sync reprorates it for the ledger
    total_cost: float | None = None  # computed = quantity * cost_per_item (this row/shipment line)
    card_last4: str = ""
    shipment: str = ""  # bare number: "1" / "2" / ...; "" only on pre-Shipment-column rows
    buying_group: str = ""  # derived from delivery_address; "Unclassified" if no jig matched, "" if blank

    # Derived from card_last4 by config.cards.tag_cards; blank when card_last4 is blank.
    card_name: str = ""
    cashback_rate: float | None = None  # decimal fraction (0.02 = 2%)
    # Amazon only: the per-order promo the order page advertises, as the mapping read it (see
    # FIELDNAMES); config.cards.add_promos adds it on top of the (capped) card rate.
    promo_rate: float | None = None
    # User-entered / BFMR-filled. Always blank from a scraper — see FIELDNAMES.
    insurance: float | None = None
    payout_date: str = ""
    payout_amount: float | None = None
    # The group's committed payout (see FIELDNAMES). Filled by sync_tracking, never by a scraper.
    expected_payout: float | None = None
    # Always blank from here; the adapter derives the cell instead.
    # Both are DERIVED IN THE LEDGER (computed columns) and always emitted blank from here — they
    # exist on the model only so FIELDNAMES can name real fields and the columns hold their
    # position in the CSV. See ledger.sync._cogs_formula / _profit_formula.
    cogs: float | None = None
    total_profit: float | None = None
    # Always blank from a scraper; sync_tracking.py ticks it when a buying group accepts the tracking
    # number. Typed as a string, not a bool, precisely so the scrapers' blank survives _merge_row —
    # a default of False would tick nothing but would overwrite a real True on every re-scrape.
    tracking_submitted: str = ""
    # Set by receipts.capture.attach_receipts after the rows are built (it needs the order id and
    # date this carries), so every scraper emits it blank and _merge_row preserves an existing link.
    receipt_url: str = ""
    # Hand-entered (or import-derived) return record; scrapers emit both blank. See FIELDNAMES.
    return_quantity: int | None = None
    return_date: str = ""
    # ORDER-LEVEL totals repeated on every row, like `shipping`; ledger_sync reprorates both into
    # per-row cost-weighted shares and the COGS formula nets them. See FIELDNAMES.
    gift_card: float | None = None
    sales_tax: float | None = None
    # ORDER-LEVEL like the two above; Amazon only (cash-back balance + points). See FIELDNAMES.
    # DEFAULTS TO A REAL 0: a row that never named rewards spent none, and the
    # ledger should read 0 rather than blank. Every scraper still sets it explicitly; only the Amazon
    # mappings ever emit None, and only when the amount could not be read (a blank never
    # overwrites, so the cell stays whatever it was until a run can price it).
    rewards_used: float | None = 0.0
    # The retailer's own per-package identity (see FIELDNAMES). Plain text — "00009999990206101794"
    # must round-trip with its zeros — and blank when the mapping has none for this row.
    package_id: str = ""

    # The block's "Sold by" merchant, read by the Amazon mappings so two same-titled lines in ONE
    # shipment (two sellers, two prices -- 114-9990029-9990029, 2026-09-08) can be told apart.
    # Mapping-internal like the promo rate: it feeds the Item Name suffix, never a column.
    _seller: str = PrivateAttr(default="")

    @field_validator("quantity", "cost_per_item", "shipping", "total_cost",
                     "cashback_rate", "promo_rate", "insurance", "payout_amount", "cogs", "total_profit",
                     "return_quantity", "gift_card", "sales_tax", "rewards_used",
                     "expected_payout", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        # A scraper may send "" (or whitespace) for numbers it skipped — treat as None, not 0.
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @field_validator("delivery_address", mode="before")
    @classmethod
    def _flatten_address(cls, v):
        """Collapse a multi-line address into one comma-separated line.

        The deterministic parsers already comma-join the address block, but the AGENT copies the page
        text verbatim, so an agent-written row could land with real newlines inside the cell — which
        makes the row tall and ragged. Normalizing here rather than in each prompt keeps the two
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

        Enforced HERE, on every path, rather than only where the deterministic mappings build
        labels: Shipment is part of the upsert key, and imports / hand-entered rows still arrive in
        the labelled form — a path emitting "Shipment 2" for a row recorded as "2" would append a duplicate
        instead of updating it.
        """
        return normalize_shipment(v) if isinstance(v, str) else v

    @field_validator("status", mode="before")
    @classmethod
    def _normalize_status(cls, v):
        """Coerce a scraped status into the known vocabulary — never reject the row.

        Rejecting would kill the whole profile's run for that cycle over one odd word, losing every
        other order in the batch. A mislabeled order costs far less than a missed one, so an
        unrecognized value falls back to "ordered" (the safe end: the order stays open and keeps
        getting re-checked) and logs a warning, which is the signal that a mapping needs tightening.
        """
        if not isinstance(v, str):
            return v
        cleaned = v.strip().lower()
        if cleaned in STATUSES:
            return cleaned
        if cleaned == "":
            return "ordered"
        log.warning("Unrecognized status %r; treating as 'ordered'.", v)
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
        already recorded.

        SHIPPING IS A SEPARATE COLUMN AND IS NOT FOLDED IN — settled 2026-08-13, and not a free
        change if you're tempted. This number is a money-path input, not just a
        display value: it gates and values real BFMR insurance filings (buying_groups/bfmr.py), is
        the `amount` declared to MOD (unrevisable once sent), weights how sync_tracking splits a
        payout across an order's rows, AND is the weight ledger_sync._reprorate_order_level divides the
        order's shipping total by — so folding that shipping share back in here is circular. The two
        are combined only inside the Total Profit formula, which subtracts Shipping on its own.
        """
        if self.quantity is not None and self.cost_per_item is not None:
            self.total_cost = round(self.quantity * self.cost_per_item, 2)
        return self

