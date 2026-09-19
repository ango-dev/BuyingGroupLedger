# Data model — the ledger as a wire format

_Part of the [Buying Group Ledger](../README.md) docs._

Everything on this page follows from rows being written *positionally* into a fixed schema, keyed on one upsert key.

## Columns and the upsert key

One row per line item (its real quantity preserved — a qty-3 line is one row, not three), keyed on
**Order ID + Order Date + Item Name + Shipment** (safe upsert — re-checks update
status/tracking/date/last-scraped **without clobbering** the item name, cost, address, etc.):

Columns follow the order events happen to an order, so the ledger reads forward as a timeline —
what it is → what happened to it → what it cost → what came back → profit, with the
rarely-scanned reference/audit columns parked at the end:

`Order Date · Status · Retailer · Item Name · Shipment · Quantity ·
Order ID · Tracking Number · Tracking Submitted · Delivery Date · Buying Group ·
Cost Per Item · Total Cost · Shipping · Sales Tax · Gift Card · Rewards Used · Card · Cashback Rate · COGS ·
Insurance · Expected Payout · Actual Payout · Payout Date · Return Qty · Return Date · Total Profit ·
Profile · Order Link · Tracking Link · Receipt Link · Delivery Address · Card Last 4 · Package ID · Last Scraped At`

**Package ID** (column 33, beside Card Last 4) is the retailer's *own* identity for the physical package a row belongs
to: Amazon's `shipmentId` (read from the card's "Track package" link, or from the "View your item"
link once the track link has expired), Costco's `packageNumber` (the carton's SSCC-style id, not the
carrier label), Best Buy's fulfillment `groupId` (a per-order ordinal, unique only within the order).
It is plain text — Costco ids carry leading zeros — and blank when unknown (an unshipped line, an old
order whose links are gone). The sync matches an incoming row on **(Order ID, Package ID) before the
tracking number**, keeping the row's recorded Shipment number and item name, so a package lands on
its own row however Amazon re-orders its cards; a multi-SKU carton shares one id across its rows and
is told apart by item name. A blank id never blocks a match. `audit_ledger`'s
`package_id_per_shipment` fails if one id ever sits under two Shipment numbers of one order.

**Expected Payout (since 2026-09-18, beside it) is the buying group's COMMITMENT; Actual Payout
(named Payout Amount until 2026-09-18) is the money.** While a BFMR package is **open**, the sync fills Expected Payout with the payout
price BFMR has committed to (`payout_price`/`total_payout`, on its tracker from the moment a
purchase exists — before shipping, before payment), prorated by Total Cost like a real payout.
Actual Payout stays blank until the package **settles**, when the real amount, Payout Date and
`paid` status land; the commitment is left where it is, so the two figures sit side by side and
the dashboard's **Reconciliation** page can list every order paid more or less than promised.
When BFMR **changes** the committed price, Expected Payout is rewritten to the new figure and an
alert names old → new; a settled amount that disagrees with the commitment is alerted once, on
the run that writes it. MOD publishes no price through its API, so its Expected Payout stays
blank and its orders are never "reconciled". Total Profit is blank until Actual Payout is filled
(the formula never sees a commitment); the dashboard shows the *projected* profit (Expected Payout
− COGS − Insurance) on committed rows instead. Anything keyed on "has this been paid?" reads the
**(Payout Date, Status)** pair — the cash-basis tax report keys income on Payout Date, the audit's
straddle line (`cogs_inputs_complete`) counts a dated-or-`paid` payout as settled. *From
2026-09-11 to 2026-09-18 the commitment shared the Actual Payout cell with a blank date;
a one-time migration moved the open rows' commitments into the new column (run, then deleted).*

> **Changing the column order is a MIGRATION, not an edit.** The ledger file does it itself the
> next time it is opened: `ledger_db/store.py` rebuilds the `ledger_rows` table in the new
> `FIELDNAMES` order with every value carried across **by column name** (never dropped), so a
> reorder or a rename in `FIELDNAMES` / `HEADER` costs nothing on the host. A populated table
> holding a column the schema no longer names is **refused loudly** rather than dropped — restore a
> backup or migrate by hand. New columns are appended **last**, to both lists;
> `tests/test_schema.py` pins the pairing. (The Google Sheet, retired 2026-09-18, was the case that
> took two scripts and a format pass; that story is in `the design notes`.)
>
> The date columns are plain ISO text (`YYYY-MM-DD`): `Order Date` is in the upsert key, so a date
> in any other spelling is a different row.

**Receipt Link** points at the order's captured receipt file, served by the dashboard at `/receipts/...` — see [Receipt capture](receipts.md). It's per *order*, so every row of a multi-item order carries the same link.

> **Column order is part of the wire format.** Rows are written *positionally* from column A, so
> `FIELDNAMES` (models/order.py) and `HEADER` (ledger/sync.py) define where every value lands.
> Reordering one without the other would silently scramble every row, so `tests/test_schema.py`
> pins the pairing, `sync_csv_to_ledger` **refuses to write** to a grid whose header order doesn't
> match, and the ledger file's own migration (above) is what conforms an existing table to a new
> order. **Adding** a column is the cheap case: append it last to both lists and existing rows just
> gain a trailing blank.

**Rows are kept newest-first** (Order Date descending, then Order ID, then Shipment ascending — so a
multi-shipment order's rows stay adjacent and in shipment order). New rows are still *written* at the
bottom and the ledger is re-sorted afterwards, which is deliberate: the sync caches each matched row's
number from its pre-sync snapshot and writes updates to that row, so moving rows mid-sync would put
every update on the wrong one. Sorting only runs when a sync actually **appended** — an update rewrites
a row where it already sits and can't change the order — so a routine re-check run skips it. Use
`python -m scripts.sort_ledger` (dry run, then `--apply`) to sort by hand if the order ever drifts.

**Adding a row by hand** is the dashboard's *Add a row* (Orders page; see [Operations](operations.md)),
with two rules. Give it a real **Order ID** — a row without one is not a ledger row and is never
stored, and a key that already exists is refused. And enter **Order Date** as `YYYY-MM-DD` — it is
part of the upsert key, so any other spelling is a different row and the next re-check appends a
duplicate beside it. Get both right and the row is indistinguishable from a scraped one: it sorts
into place on the next append-triggered sort (`scripts/sort_ledger.py --apply` if you don't want to
wait). Don't hand-write **Total Profit** or **COGS** — they are computed from the row on every read,
and a write into them is ignored. **Insurance** is yours to fill in and is never overwritten by
anything. **Actual Payout** and **Payout Date** are hand-entered too, but the buying-group sync fills
them in once the group pays (it never blanks a cell it has no figure for, so a value you typed only
changes if the group reports a different one — and a cell typed on the dashboard is protected from
the run outright until you clear or release it). `scripts/audit_ledger.py` flags all of this.

**Status** is one of `ordered`, `shipped`, `delivered`, `cancelled`, `paid`, `return`, `superseded`. The first two are
the live lifecycle the scrapers maintain; the other five are **terminal** — the order drops out of
future runs. `superseded` is a shipment row whose tracking number Amazon re-issued for the same
delayed package: only `scripts/fix_superseded_shipments.py` writes it, the dead number stays as the
record of what was posted to the buying group, the row is renumbered after the live boxes, every
money cell (Quantity included) is blank, and nothing ever submits, insures, pays or re-scrapes it
(`superseded_rows_carry_no_money` in the audit enforces the blank money). `paid` and `return` come from the **buying group**, not the retailer (see [Buying groups](buying-groups.md)): BFMR reports both, MOD confirms `paid` by listing a package as received but has no
return signal, so a MOD return is typed in by hand. A status only ever moves forward, so that
hand-typed `return` survives every later run. Anything outside this vocabulary keeps the order **open forever**, so it
gets re-read on every run indefinitely — wasted work on an order that is already finished. `audit_ledger`'s `column_shape` fails an unknown status for exactly that
reason.

## How rows are built

**Total Cost is per row** = `Quantity × Cost Per Item` for that shipment line (computed in code,
never trusted from a page), so the column sums to the order total. A `cancelled` order is only ever
recorded via a re-check (an order first seen as `ordered` that the order page later shows cancelled);
brand-new already-cancelled orders are ignored at discovery.

**Multiple shipments per order:** when an order splits across shipments, each shipment gets its own
row(s) with that shipment's own status, tracking number and delivery date. The **Shipment** column is
part of the key so the *same* product in two different shipments stays on two distinct rows instead of
colliding. Its value is a bare number (`1`, `2`) — the column heading already says "Shipment".

**Every retailer numbers shipments** `1`, `2`, … top-to-bottom, a single shipment
included. Amazon starts an order as one shipment and often **splits it into several when it ships**, so
numbering from the start means the original row updates in place (`1`) and the newly-split
shipments are added as new rows. Best Buy uses the same scheme deliberately: the label is part of the
upsert key, so a label that varies between runs (the page's own wording isn't guaranteed to be stable,
or present) would append a duplicate row instead of updating the existing one.

**Re-check routing.** Open orders are re-checked by re-reading the whole order-details page, which
reports every shipment. Amazon can add a shipment (with its own later delivery date) after an order
already looks shipped, so a cached single-page poll would silently miss it.

**Digital items are skipped** on every retailer (gift cards, eBooks, memberships, redemption codes,
etc.) — they're never resold, so they never hit the ledger.


## Profit accounting

**Profit accounting** (Card through Total Profit) turns the ledger into a P&L rather than just a tracker:

- **Card** and **Cashback Rate** are derived automatically from `Card Last 4`, which the scrapers
  already capture, via the `cards` section of `config.json` (see [Card and cashback config](configuration.md#card-and-cashback-config)). Each card has an
  overall rate plus optional **per-retailer overrides**, so a card earning 1.5% generally and 5% at
  Amazon reports the right rate on each row. A card that isn't configured keeps a blank name — so the
  gap stays visible — but still gets your `DEFAULT_CASHBACK_RATE` so profit stays computable. The rate
  is the only cashback column; the dollar amount isn't stored, it's folded into Total Profit.
  On **Amazon**, if the order page advertises a bonus under the payment method ("Earn 5% back … plus an
  extra 1% back"), that extra is **added** to the card's configured rate for that order, so the single
  Cashback Rate cell carries the true total (`AMAZON_PROMO_CASHBACK_ENABLED=false` turns it off).
- **Gift cards earn no cashback**, so when one pays part of an Amazon or Amazon Business order the
  amount lands in the **Gift Card** column (each row gets its cost-weighted share of the order
  total, like Shipping) and the COGS formula subtracts it — the cost basis and the cashback it
  drives reflect card spend only, which raises reported profit by the gift-card amount. Total Cost
  stays the gross number the order page shows. (Until 2026-08-30 the mappings instead scaled the
  cost down invisibly; the formula computes the identical number, with the amount now in the
  ledger.) `AMAZON_GIFT_CARD_NETTING_ENABLED=false` leaves the Gift Card cell blank, so COGS uses
  the full sticker cost.

  **Amazon rewards spent on an order are NOT gift cards** — they get their own **Rewards Used**
  column (appended last, 2026-09-08). Two kinds exist: a spent Prime cash-back balance (the
  `Prime for Young Adults cash back: -$15.98` line in the order summary) and **Amazon points**
  (Business Prime Rewards). A redemption can be partial, so the amount is always read, never
  assumed. On Amazon Business it comes from the **Business Prime Rewards ledger** (one page,
  loaded once at the end of the run whenever some order's payment list names "Amazon point"),
  which lists every redemption by order at 100 points to the dollar and knows it the moment the
  order is placed; the order's related-transactions page is the fallback, and on consumer Amazon
  the only source. If neither prices it the cell stays blank and the run ends with a dossier
  problem, rather than writing a false 0. Best Buy and Costco write a real 0 (no rewards
  programme there yet), and the column defaults to 0 rather than blank everywhere else.

  **Why a separate column, and the year-end rule it encodes (owner, 2026-09-08).** The owner nets
  **every** Amazon reward — Prime for Young Adults cash back **and** Business Prime Rewards — out
  of COGS at year end, from Amazon's own rewards history, outside this ledger. So the ledger must
  record an order paid with rewards at its **full cost**, exactly as if the card had paid all of
  it; netting the redemption here as well would count the same dollars twice (an all-points order
  would show a $0 cost *and* a year-end rewards deduction). What Rewards Used changes is only the
  cashback basis: the card earns nothing on dollars it never paid. The COGS formula below does
  exactly that — the amount leaves the parenthesis and is added straight back.

- **Sales Tax** is its own column, read from the order summary's "Estimated tax to be collected"
  line and prorated the same way. It is usually $0.00 (the resale certificate), but a hand-kept
  order that paid tax records its true cost: the COGS formula adds it inside the cashback netting,
  since the card is charged tax and earns cashback on it.

  **The accounting rule this implies:** a cost is recorded ONCE, where the money actually left your
  pocket. So if you *bought* the gift card, add its purchase as its own row and the two reconcile —
  a $40 card bought at face value and spent on a $100 order totals the same profit as simply paying
  $100 on the card, and a card bought at a discount shows the spread as real profit. If the gift card
  was *given* to you, there is no purchase row and its value is pure profit, which is correct.
  On **Amazon**, that purchase row is now created FOR you when the gift card was bought on a card that
  carries an explicit Amazon rate in `config.json`'s `cards` section — such a card is a reselling card, so the gift card is
  funding inventory. It lands as a `delivered` row with cost and cashback and no tracking. A gift card
  bought on any other card is treated as personal and skipped, as are all other digital items; if you
  need one of those on the ledger, add it by hand.

  Such a row is tagged **`Gift Card`** in the Buying Group column, and that tag is *deliberate
  non-routing* — distinct from the two accidental kinds:

  | Tag | Meaning | Behaviour |
  |---|---|---|
  | `Unclassified` | a warehouse nobody configured | a gap to FIX — **alerts** if the row has tracking |
  | `Personal` | your own address | **dropped**, never reaches the ledger |
  | `Gift Card` | not a resale at all | **kept**, routed nowhere, and silent |

  The distinction earns its keep: a gift card ships with a tracking number like anything else, and
  without it every gift-card row would sit in `sync_tracking`'s unroutable list alerting on every run
  as though a reimbursement were about to be lost — training you to ignore the one alert that means
  exactly that. The tag also **wins over the delivery address**, so a card shipped to your own home
  isn't classified `Personal` and deleted. Hand-entered gift-card rows should carry the same tag.
- **Insurance**, **Payout Date** and **Actual Payout** are filled by the buying-group sync (see [Buying groups](buying-groups.md)) — or by hand until you enable it. The scrapers always write them blank, and
  the upsert's blank-never-overwrites rule is what stops a re-scrape from wiping what you typed.
- **COGS** and **Total Profit** are **computed, never stored** — the ledger derives them from the
  row on every read (`web.ledger_reader.cogs_of` / `profit_of`, the Python form of what were the
  Sheet's two formulas), so they are not scraped numbers:

  ```
  COGS         = (Total Cost − Return Qty × Cost Per Item − Gift Card + Shipping + Sales Tax
                  − Rewards Used) × (1 − Cashback Rate) + Rewards Used
  Total Profit = Actual Payout − COGS − Insurance
  ```

  Blank cells count as 0, so a row with no return, no gift card, no recorded tax and no rewards
  spent is simply `(Total Cost + Shipping) × (1 − Cashback Rate)`.

  **COGS exists for end-of-year tax**, and the split is where the tax form wants it. Cashback is
  netted into *cost* rather than counted as income, because a card reward earned on a purchase is a
  purchase-price adjustment, not receipts. **Insurance is deliberately not in COGS** — a buying-group
  premium is an ordinary business expense, a separate line on the form. Expand COGS and you get the
  old single-cell profit formula exactly; the two are the same number, split where it's useful.

  Unlike Total Profit, **COGS does not blank on an unpaid row**: the cost was incurred whether or not
  the group has paid yet, and the year-end cost side has to count it.

  It is computed on read so it changes the instant you type an Insurance or Actual Payout — a value
  computed at scrape time would go stale immediately, and a `delivered` row is terminal and never
  re-scraped, so it would stay stale forever. The cell reads blank (not `0`) until Actual Payout is
  filled, so un-paid-out rows don't drag a column sum down with fake losses.

  **Shipping, Gift Card and Sales Tax are allocated pro-rata across an order's rows** (`the order
  total × this row's Total Cost ÷ the order's Total Cost`). Every retailer reports these as one
  *order-level* figure and repeats it on every row, so charging it per row would bill a 3-row order
  three times over and make the column's sum wrong. Pro-rata makes each column sum to exactly one
  charge per order.

## Year-end tax report

```bash
python -m scripts.tax_report              # asks for the year
python -m scripts.tax_report 2026 --no-rows
python -m scripts.tax_report 2026 --from-snapshot before.json   # offline, from an audit snapshot
```

Read-only (it opens the ledger through the audit's read-only handle, `open_ledger_readonly`). **Two dates drive the year, on a cash
basis:** receipts are Actual Payouts whose *Payout Date* falls in the year; COGS and insurance are
taken from rows whose *Order Date* does, cancelled rows excluded. A December order paid in January is
therefore a cost in one year and income in the next, and the report's "straddling" block says how
much money sits on each side of the boundary — the number a preparer asks about. Insurance is
reported as its own line (a Schedule C expense), never inside COGS; cashback is shown separately so
the netting is visible. Breakdowns by retailer and by buying group, plus the order list; `--json`
for anything else.

**A partial return is one hand edit on the original row, never a second negative row**. Quantity and Total Cost keep the GROSS bought numbers the scraper wrote; you
type **Return Qty** (and **Return Date**), and the COGS formula nets the returned units out of the
cost basis itself (see the formula above under Profit accounting), so Total Profit follows with no
other edits. The payout nets itself for BFMR (its reported amount is
already net of clawbacks, allocated per order); a MOD return's payout is hand-entered, since MOD has
no return signal. A FULLY returned order keeps status `return` (Return Qty = Quantity nets its COGS
to zero). The `return_columns_consistent` audit check guards the pair, and the single netted row
equals the old two-row bookkeeping to the cent (a pinned test proves the algebra).

**A cancelled or superseded row carries no money.** Cost, Shipping, Insurance, Payout and both formula columns
are emptied on any row whose Status is `cancelled` — the order was refunded, so leaving the scraped
cost there makes it look like a real purchase to anything summing the column, and at year end that is
an overstated cost of goods. The row itself stays: what was ordered, from whom, and that it was
cancelled is worth keeping. This is applied on every write, so future cancellations clean themselves.
A `superseded` row additionally blanks Quantity — it is the multiplier that booked a re-labelled
package's cost twice — and the sync's order-level proration skips both statuses so it never
re-fills them.

**A re-labelled single unit is not a split.** When an existing row's tracking number changes to a
different number, the upsert normally treats it as an undisclosed split (a second box the retailer
surfaces one number at a time for) and appends a Quantity `*` row for you to resolve. But a
Quantity-1 row cannot split — one unit is one box — so on a qty-1 row the change is the carrier
re-issuing the label: the row takes the new number (its Tracking Submitted tick is cleared so the
sync posts it), keeps its cost, and the old number is kept as a `superseded` row. Any retailer.

**Buying Group** classifies each row's `Delivery Address`: which buying group's warehouse the order
shipped to, or `Unclassified` when the address matches no configured warehouse. It's derived at run time
from the `warehouses` section of `config.json` (see [Warehouse and jig config](configuration.md#warehouse-and-jig-config)), so it also sets up the later
buying-group tracking-post step. **Personal orders are dropped entirely** — an address matched to a group
named `Personal` (your own reship/consumer addresses) never reaches the ledger. `Unclassified` is
deliberately *not* treated as personal: a real warehouse you simply haven't configured yet is kept and
counted (in the run log) rather than silently disappearing. A blank address on a partial re-check leaves
the tag untouched.
