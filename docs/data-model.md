# Data Model: The Ledger as a Wire Format

_Part of the [Buying Group Ledger](../README.md) docs._

Rows are written *positionally* into a fixed schema and keyed on one upsert key. Everything on this
page follows from that.

## Columns and the Upsert Key

One row per line item per shipment (a qty-3 line is one row, not three), keyed on
**Order ID + Order Date + Item Name + Shipment**. The upsert updates a matched row in place, and a
blank value never overwrites a filled cell, so a re-check can't wipe what an earlier run or you wrote.

The 36 columns run in the order things happen to an order: what it is, what happened to it, what it
cost, what came back, then reference columns:

`Order Date · Status · Retailer · Item Name · Shipment · Quantity ·
Order ID · Tracking Number · Tracking Submitted · Delivery Date · Buying Group ·
Cost Per Item · Total Cost · Shipping · Sales Tax · Gift Card · Rewards Used · Card · Cashback Rate · Promo Rate · COGS ·
Insurance · Expected Payout · Actual Payout · Payout Date · Return Qty · Return Date · Total Profit ·
Profile · Order Link · Tracking Link · Receipt Link · Delivery Address · Card Last 4 · Package ID · Last Scraped At`

The date columns are plain ISO text (`YYYY-MM-DD`). `Order Date` is in the upsert key, so a date in
any other spelling is a different row.

**Package ID** is the retailer's own id for the physical package: Amazon's `shipmentId`, Costco's
`packageNumber` (the carton's id, not the carrier label), Best Buy's fulfillment `groupId` (unique
only within the order). It is text (Costco ids carry leading zeros) and blank when unknown. The upsert
matches an incoming row on **(Order ID, Package ID) before the tracking number**, keeping the row's
recorded Shipment and item name, so a package lands on its own row however the page is ordered.
A blank id never blocks a match. The audit's `package_id_per_shipment` fails if one id sits under two Shipment numbers of one order.

**Expected Payout is the buying group's commitment; Actual Payout is the money.** While a BFMR
package is open, the sync writes BFMR's committed payout price into Expected Payout, prorated by
Total Cost across the rows it covers. Actual Payout stays blank until the package **settles**,
when the amount, Payout Date and `paid` status land; the commitment stays beside it, and the
dashboard's Recon page lists orders paid more or less than promised. A changed commitment is
rewritten and alerted (old → new); a settlement that disagrees with it is alerted once. MOD
publishes no price, so its Expected Payout stays blank. Anything asking "has this been paid?"
reads the **(Payout Date, Status)** pair.

**Receipt Link** points at the order's receipt file, served by the dashboard at `/receipts/...`
([Receipt capture](receipts.md)). It is per order, so every row of the order carries the same link.

### Column Order Is a Migration

Rows are written positionally from column A, so `FIELDNAMES` (`models/order.py`) and `HEADER`
(`ledger/sync.py`) decide where every value lands. `tests/test_schema.py` pins the two lists
together, and `sync_csv_to_ledger` **refuses to write** to a ledger whose header order differs.

- **Adding** a column: append it **last** to both lists. Existing rows gain a trailing blank.
- **Moving or renaming** one is a migration, which the ledger file does itself on the next open:
  `ledger_db/store.py` rebuilds the table in the new order, carrying every value across **by column
  name**. A populated table holding a column the schema no longer names is **refused loudly**,
  never dropped. Restore a backup or migrate by hand.

### Sort Order

Rows are kept newest-first: Order Date descending, then Order ID, then Shipment ascending, so an
order's rows stay together. New rows are written at the bottom and the ledger is re-sorted
afterwards, and only when a sync **appended**. The sync writes each update to the row number it
cached before the sync, so rows must not move mid-sync. To sort by hand: `python -m scripts.sort_ledger`
(dry run), then `--apply`.

### Adding a Row by Hand

Use *Add a row* on the dashboard's Orders page ([Operations](operations.md#the-orders-page)):

- Give it a real **Order ID**. A row without one is never stored, and an existing key is refused.
- Enter **Order Date** as `YYYY-MM-DD`, or the next re-check appends a duplicate beside it.
- Don't type **COGS** or **Total Profit**. They are computed from the row on every read, and a
  write into them is ignored.
- **Insurance**, **Actual Payout** and **Payout Date** are written blank by the scrapers. Fill them
  by hand, or let the buying-group sync fill them ([Buying groups](buying-groups.md)); it never
  blanks a cell it has no figure for.

`python -m scripts.audit_ledger` flags a row that breaks these rules.

### Status

| Status | Written by | Terminal? |
|---|---|---|
| `ordered`, `shipped` | the scrapers | no, re-checked every run |
| `delivered` | the scrapers | yes |
| `cancelled` | a re-check of an order first seen live (a brand-new cancelled order is ignored) | yes |
| `paid`, `return` | the buying-group sync (BFMR reports both; MOD confirms `paid` only, so a MOD return is typed by hand) | yes |
| `superseded` | the upsert, or `scripts/fix_superseded_shipments.py` (a re-issued tracking label, below) | yes |

A status only moves forward, so a hand-typed `return` survives later runs. An unknown status keeps
the order open, so it is re-read every run forever. The audit's `column_shape` fails it.

A `superseded` row is a shipment whose tracking number the carrier re-issued for the same package.
The dead number stays as the record of what was posted to the buying group. The row is renumbered
after the live boxes, every money cell (Quantity included) is blank, and nothing submits, insures,
pays or re-scrapes it (`superseded_rows_carry_no_money` enforces this).

## The Ledger File and Hand Edits

`data/ledger.sqlite3` (`database.path`) **is** the ledger. Every writer and reader goes through
`ledger_db/worksheet.py`'s `DbWorksheet`, a worksheet-faced adapter; the audit and the tax report
open it read-only ([Operations](operations.md#the-ledger-file-ledger_db)).

**A cell typed on the dashboard is protected.** The dashboard's writer records it in the file's
`hand_edits` table, and the upsert's merge, the order-level reproration and the buying-group sync's
payout, insurance and Expected Payout writes all leave it alone. Clearing the cell puts back what
the run had written before (or leaves it blank) and releases it. Tools → Ledger Fixes →
*Hand-edited cells* (`python -m scripts.hand_edits`) lists and releases them. Repair scripts run
with `--apply` are not gated.

## How Rows Are Built

- **Total Cost** = `Quantity × Cost Per Item` for the row, computed in code, never read from a page,
  so the column sums to the order total.
- **Shipments.** Every retailer numbers shipments `1`, `2`, … top to bottom, a single shipment
  included, as a bare number. Shipment is in the key, so the same product in two shipments stays
  on two rows. Amazon often splits an order when it ships: shipment `1` updates in place and the new
  shipments append. A stable number (not the page's wording) keeps a re-check from appending
  duplicates.
- **Re-checks** re-read the whole order-details page, because Amazon can add a shipment after an
  order already looks shipped.
- **Digital items are skipped** (gift cards, eBooks, memberships, codes), except a gift card bought
  for reselling (below).

## Profit Accounting

### Card and Cashback

- **Card** and **Cashback Rate** are derived from `Card Last 4` through the `cards` section of
  `config.json` ([Card and cashback config](configuration.md#card-and-cashback-config)): an overall
  rate plus per-retailer rates. An unconfigured card keeps a blank Card (the gap stays visible) and
  gets `scraping.default_cashback_rate` so profit stays computable. The rate is a fraction (`0.02`).
  The dollar amount isn't stored; it's inside COGS.
- A rate cell records **the rate at purchase time**. The one exception is a card with a
  [spend cap](configuration.md#spend-caps): after every sync, rows past shipped (`delivered`,
  `paid`, `return`) are re-rated from the period's spend, skipping hand-edited cells.
- **Amazon promo cashback.** When the order page advertises an extra ("… plus an extra 1% back"),
  it is **added** to the card's rate in Cashback Rate, and **Promo Rate** records the extra on its
  own (blank = no promo), so a spend cap can take it off and put it back. Off:
  `scraping.amazon_promo_cashback_enabled = false`.

### Gift Cards, Rewards and Tax

- **Gift Card**: the part of an Amazon or Amazon Business order paid by gift card. Gift cards earn no
  cashback, so COGS subtracts it and cost and cashback reflect card spend only. Total Cost stays the
  gross figure the order page shows. Off: `scraping.amazon_gift_card_netting_enabled = false` (the
  cell stays blank and COGS uses the full cost).
- **Rewards Used**: Amazon rewards spent on the order, which are **not** gift cards: a Prime cash-back
  balance, or Amazon points (Business Prime Rewards). The amount is always read, never assumed. On
  Amazon Business it comes from the Business Prime Rewards page (100 points = $1), with the order's
  related-transactions page as the fallback (the only source on consumer Amazon). If neither prices
  it, the cell stays blank and the run reports a dossier problem, never a false 0. Best Buy and
  Costco write 0.
  - **The rule it encodes:** the owner nets **every** Amazon reward out of COGS at year end, from
    Amazon's own rewards history, outside the ledger. So the ledger records a rewards-paid order at
    its **full cost**; netting it here too would count the same dollars twice. Rewards Used only
    removes those dollars from the cashback basis, since the card earned nothing on them.
- **Sales Tax**: from the order summary's tax line, usually $0.00 (resale certificate). It is a real
  cost and the card earns cashback on it, so COGS adds it inside the cashback netting.

**A cost is recorded once, where the money left your pocket.** A gift card you *bought* gets its
own row, and the two reconcile: a $40 card bought at face value and spent on a $100 order gives the
same profit as paying $100 by card, and a discounted card shows the spread as profit. A gift card
you were *given* has no purchase row, and its value is profit. On Amazon the purchase row is created
for you when the gift card was bought on a card with an explicit Amazon rate in `cards`: a
`delivered` row with cost and cashback and no tracking. A gift card bought on any other card is
treated as personal and skipped; add it by hand if you need it.

Such a row carries **`Gift Card`** in Buying Group, which means *deliberately routed nowhere*:

| Tag | Meaning | Behaviour |
|---|---|---|
| `Unclassified` | an address no warehouse matches | a gap to fix: **alerts** if the row has tracking |
| `Personal` | your own address | **dropped**, never reaches the ledger |
| `Gift Card` | not a resale | **kept**, routed nowhere, silent |

Without it, every gift card (which ships with tracking) would alert as unroutable on every run. It
also wins over the address, so a card shipped home isn't dropped. Tag hand-entered ones the same.

### COGS and Total Profit

Both are **computed, never stored**: derived from the row on every read (`web.ledger_reader.cogs_of`
/ `profit_of`):

```
COGS         = (Total Cost − Return Qty × Cost Per Item − Gift Card + Shipping + Sales Tax
                − Rewards Used) × (1 − Cashback Rate) + Rewards Used
Total Profit = Actual Payout − COGS − Insurance
```

- Blank cells count as 0, so a plain row is `(Total Cost + Shipping) × (1 − Cashback Rate)`.
- Both read blank on a `cancelled` or `superseded` row, and COGS reads blank with no Total Cost.
- **Total Profit is blank until Actual Payout is filled**, so unpaid rows don't show fake losses.
  The dashboard shows a *projected* profit (Expected Payout − COGS − Insurance) on committed rows.
- **COGS does not wait for a payout**: the cost was incurred, and the year-end cost side counts it.
- **COGS is for year-end tax.** Cashback is netted into cost (a purchase-price adjustment, not
  income). **Insurance is not in COGS**: a buying-group premium is a separate business expense.

**Shipping, Gift Card, Sales Tax and Rewards Used are allocated pro rata** across an order's rows:
`order total × this row's Total Cost ÷ the order's Total Cost`. Retailers report them per order, so
each column then sums to exactly one charge per order.

### Returns, Cancellations and Re-labels

- **A partial return is one hand edit on the original row**, never a second negative row. Quantity
  and Total Cost stay gross; type **Return Qty** and **Return Date**, and COGS nets the returned
  units out (above). BFMR's reported payout is already net of clawbacks; a MOD return's payout is
  typed by hand. A full return is status `return` with Return Qty = Quantity, which nets its COGS
  to zero. The audit's `return_columns_consistent` guards the pair.
- **A cancelled or superseded row carries no money.** Cost, Shipping, Insurance, payouts and the
  computed columns are emptied on every write (a superseded row loses Quantity too), and the
  order-level proration skips both. The row stays as a record.
- **A re-labelled single unit is not a split.** When a row's tracking number changes, the upsert
  normally treats it as an undisclosed split and appends a Quantity `*` row for you to resolve. A
  Quantity-1 row can't split, so there the row takes the new number (Tracking Submitted cleared so
  the sync posts it) and the old number is kept as a `superseded` row.

### Buying Group

Buying Group classifies each row's Delivery Address against the `warehouses` section of
`config.json` ([Warehouse and jig config](configuration.md#warehouse-and-jig-config)): a group's
name, or `Unclassified` when nothing matches. It is the key the buying-group sync routes on. Rows
matched to `Personal` are dropped. `Unclassified` rows are kept and counted in the run log, so an
unconfigured warehouse stays visible. A blank address on a re-check leaves the tag alone.

## Year-End Tax Report

```bash
python -m scripts.tax_report              # asks for the year
python -m scripts.tax_report 2026 --no-rows
python -m scripts.tax_report 2026 --json
python -m scripts.tax_report 2026 --from-snapshot before.json   # offline, from an audit snapshot
```

Read-only (it opens the ledger through `open_ledger_readonly`). **Cash basis, two dates:**

- **Income** = Actual Payout on rows whose **Payout Date** falls in the year.
- **COGS** and **insurance** = rows whose **Order Date** falls in the year; cancelled and superseded
  rows excluded.

A December order paid in January is a cost in one year and income in the next. The "straddling"
block says how much sits on each side of the boundary, and counts paid rows with no Payout Date on
their own line instead of dropping them. Insurance is its own line (a Schedule C expense). Cashback
is shown separately so the netting is visible. A bought gift card counts in COGS on its own line, not
as money owed. It also breaks the year down by retailer and buying group, and lists the orders.

The dashboard's Taxes page lays the same figures out on Schedule C's lines and adds what the ledger
can't know: expenses, program cashback, sign-up bonuses, cashback sites and other income
([Operations](operations.md#the-taxes-page)).
