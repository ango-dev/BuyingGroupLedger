# Data model — the sheet as a wire format

_Part of the [Buying Group Ledger](../README.md) docs._

Everything on this page follows from rows being written *positionally* into a fixed schema, keyed on one upsert key.

## Columns and the upsert key

One row per line item (its real quantity preserved — a qty-3 line is one row, not three), keyed on
**Order ID + Order Date + Item Name + Shipment** (safe upsert — re-checks update
status/tracking/date/last-scraped **without clobbering** the item name, cost, address, etc.):

Columns follow the order events happen to an order, so the sheet reads forward as a timeline —
what it is → what happened to it → what it cost → what came back → profit, with the
rarely-scanned reference/audit columns parked at the end:

`Order Date · Status · Retailer · Item Name · Shipment · Quantity ·
Order ID · Tracking Number · Tracking Submitted · Delivery Date · Buying Group ·
Cost Per Item · Total Cost · Shipping · Sales Tax · Gift Card · Card · Cashback Rate · COGS ·
Insurance · Payout Amount · Payout Date · Return Qty · Return Date · Total Profit ·
Profile · Order Link · Tracking Link · Receipt Link · Delivery Address · Card Last 4 · Last Scraped At`

> **Changing the column order is a MIGRATION, not an edit**, and it takes two steps.
> `python -m scripts.reorder_sheet --apply` moves the row *values*; `python -m
> scripts.apply_sheet_formats --apply` then puts the presentation back. Both are dry-run by default.
>
> The second step is not optional, and the reason is easy to miss: **this sheet is a Google Sheets
> Table, and a table column's TYPE overrides the cell number format.** Set a cell to PERCENT under a
> CURRENCY column and nothing happens — silently, with the API returning success. Types and formats
> are both bound to a column *position*, so a reorder strands every one of them on its old letter. A
> real instance: a $631 payout rendering as `63100%`, and the checkbox moving off Tracking Submitted
> onto Delivery Address. `apply_sheet_formats` fixes both, derived from `HEADER` by name so it stays
> correct after any future reorder.
>
> **Status is pinned at column B.** The sheet's status colour rules are `=$B2="delivered"` and
> friends, and `reorder_sheet` rewrites values *without moving columns* — so moving Status would
> leave all six rules colouring every row by whatever landed in B. Insert new columns after it.
>
> The date columns are deliberately left **untyped**: `Order Date` is in the upsert key and must stay
> plain ISO text, or Sheets stores a serial and the next re-check duplicates the row.

**Receipt Link** points at the order's captured receipt in object storage — see [Receipt capture](receipts.md). It's per *order*, so every row of a multi-item order carries the same link.

> **Column order is part of the wire format.** Rows are written to the sheet *positionally* from column
> A, so `FIELDNAMES` (models/order.py) and `HEADER` (sheets/ledger_sync.py) define where every value
> lands. Reordering them without rewriting the rows already on the sheet would silently scramble every
> one of them, so `sync_csv_to_sheet` **refuses to write** to a sheet whose header order doesn't match,
> and `python -m scripts.reorder_sheet --apply` is what conforms an existing sheet to a new order.
> **Adding** a column is the cheap case: append it to both lists and existing rows just gain a trailing
> blank — no migration needed.

**Rows are kept newest-first** (Order Date descending, then Order ID, then Shipment ascending — so a
multi-shipment order's rows stay adjacent and in shipment order). New rows are still *written* at the
bottom and the sheet is re-sorted afterwards, which is deliberate: the sync caches each matched row's
number from its pre-sync snapshot and writes updates to that row, so moving rows mid-sync would put
every update on the wrong one. Sorting only runs when a sync actually **appended** — an update rewrites
a row where it already sits and can't change the order — so a routine re-check run skips it. Use
`python -m scripts.sort_ledger` (dry run, then `--apply`) to sort by hand if the order ever drifts.

**Adding a row by hand** works, with two rules. Give it a real **Order ID** — the sort covers the whole
block from row 2 to the last row that has one, and a row without one is invisible to the upsert forever
(every future re-check appends beside it rather than updating it). And enter **Order Date as text** —
typing `2026-08-12` makes Sheets store a real Date, which changes the upsert key and duplicates the row
on its next re-check; type `'2026-08-12`, or format the column as plain text first. Get both right and
the row is indistinguishable from a scraped one: it sorts into place and gets its Total Profit formula
on the next append-triggered sort (`scripts/sort_ledger.py --apply` if you don't want to wait). Don't
hand-write **Total Profit** — it's position-bound and re-stamped on every sort. **Insurance** is yours
to fill in and is never overwritten by anything. **Payout Amount** and **Payout Date** are hand-entered
too, but the buying-group sync fills them in once the group pays (it never blanks a cell it has no
figure for, so a value you typed only changes if the group reports a different one). A note or spacer row belongs *below* the last order, where the sort leaves it alone; put one
inside the block and it gets shuffled in among the orders. `scripts/audit_sheet.py` flags all of this.

**Status** is one of `ordered`, `shipped`, `delivered`, `cancelled`, `paid`, `return`. The first two are
the live lifecycle the scrapers maintain; the other four are **terminal** — the order drops out of
future runs. `paid` and `return` come from the **buying group**, not the retailer (see [Buying groups](buying-groups.md)): BFMR reports both, MOD confirms `paid` by listing a package as received but has no
return signal, so a MOD return is typed in by hand. A status only ever moves forward, so that
hand-typed `return` survives every later run. Anything outside this vocabulary keeps the order **open forever**, so it
gets re-read on every run indefinitely — wasted work on an order that is already finished. `audit_sheet`'s `column_shape` fails an unknown status for exactly that
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
  cost down invisibly; the formula computes the identical number, with the amount now on the
  sheet.) `AMAZON_GIFT_CARD_NETTING_ENABLED=false` leaves the Gift Card cell blank, so COGS uses
  the full sticker cost.
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
- **Insurance**, **Payout Date** and **Payout Amount** are filled by the buying-group sync (see [Buying groups](buying-groups.md)) — or by hand until you enable it. The scrapers always write them blank, and
  the upsert's blank-never-overwrites rule is what stops a re-scrape from wiping what you typed.
- **COGS** and **Total Profit** are **live Google Sheets formulas**, not scraped numbers:

  ```
  COGS         = (Total Cost − Return Qty × Cost Per Item − Gift Card + Shipping + Sales Tax)
                 × (1 − Cashback Rate)
  Total Profit = Payout Amount − COGS − Insurance
  ```

  Blank cells count as 0, so a row with no return, no gift card and no recorded tax is simply
  `(Total Cost + Shipping) × (1 − Cashback Rate)`.

  **COGS exists for end-of-year tax**, and the split is where the tax form wants it. Cashback is
  netted into *cost* rather than counted as income, because a card reward earned on a purchase is a
  purchase-price adjustment, not receipts. **Insurance is deliberately not in COGS** — a buying-group
  premium is an ordinary business expense, a separate line on the form. Expand COGS and you get the
  old single-cell profit formula exactly; the two are the same number, split where it's useful.

  Unlike Total Profit, **COGS does not blank on an unpaid row**: the cost was incurred whether or not
  the group has paid yet, and the year-end cost side has to count it.

  It's a formula so it recalculates the instant you type an Insurance or Payout Amount — a value
  computed at scrape time would go stale immediately, and a `delivered` row is terminal and never
  re-scraped, so it would stay stale forever. The cell reads blank (not `0`) until Payout Amount is
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

Read-only (it goes through the audit's read-only scope). **Two dates drive the year, on a cash
basis:** receipts are Payout Amounts whose *Payout Date* falls in the year; COGS and insurance are
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

**A cancelled order carries no money.** Cost, Shipping, Insurance, Payout and both formula columns
are emptied on any row whose Status is `cancelled` — the order was refunded, so leaving the scraped
cost there makes it look like a real purchase to anything summing the column, and at year end that is
an overstated cost of goods. The row itself stays: what was ordered, from whom, and that it was
cancelled is worth keeping. This is applied on every write, so future cancellations clean themselves.

**Buying Group** classifies each row's `Delivery Address`: which buying group's warehouse the order
shipped to, or `Unclassified` when the address matches no configured warehouse. It's derived at run time
from the `warehouses` section of `config.json` (see [Warehouse and jig config](configuration.md#warehouse-and-jig-config)), so it also sets up the later
buying-group tracking-post step. **Personal orders are dropped entirely** — an address matched to a group
named `Personal` (your own reship/consumer addresses) never reaches the sheet. `Unclassified` is
deliberately *not* treated as personal: a real warehouse you simply haven't configured yet is kept and
counted (in the run log) rather than silently disappearing. A blank address on a partial re-check leaves
the tag untouched.
