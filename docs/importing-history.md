# Importing History

_Part of the [Buying Group Ledger](../README.md) docs._

> **Only import FINISHED orders**: `delivered`, `paid`, `return`, `cancelled`. The scrapers follow
> open orders on their own, so an imported `ordered` or `shipped` row only risks a duplicate beside
> the scraped one. Terminal rows are never re-read.

| Way | For |
|---|---|
| **Tools › Import** on the dashboard | Most imports: map a CSV's columns, preview, land the complete rows, fix the rest on a staging sheet. See [The web dashboard](operations.md#the-web-dashboard). |
| `scripts/import_history.py` | A spreadsheet whose profit figures you want proven before anything is written. |
| *Add a row* on the Orders page | A handful of rows. |

## `scripts/import_history.py`

Dry run by default:

```bash
python -m scripts.import_history old.csv --rate-add "SUB Rate:SUB" --profile profile-1
python -m scripts.import_history old.csv --apply
```

It maps headers onto the ledger's columns (`--map "Src=Target"` overrides), converts dates to ISO
(a file whose day/month order it cannot prove needs `--date-format mdy|dmy`), derives Cost Per
Item, splits a cell of several tracking numbers into one row per box, and reads `Please fill` /
`#VALUE!` as blank.

**It recomputes every row's profit and refuses the import if any row disagrees with the source's
profit column** (auto-detected, or `--source-profit`) by more than a cent. That proves the rate
mapping (`--rate-add COL:FLAG`: a bonus rate that applies only when a flag column is TRUE) and the
insurance sign before a cell is written.

The preview shows updates vs appends, orders already scraped under other item names (skipped unless
`--allow-existing-orders`) and tracking numbers held by another order, and writes a normalised CSV
under `data/`.

| Row | Treatment |
|---|---|
| Not terminal | Refused (`--allow-open` overrides). |
| No cost | Skipped so a later scrape can fill the order (`--keep-no-cost` overrides). |
| No tracking number | Accepted with a warning. |
| No order number | Given a synthetic one (`<group>-IMPORT-<date>`), so a referral bonus still reaches the tax report. |

`--apply` writes through the same upsert every scrape uses and re-sorts. Bracket it with
`audit_ledger --save-snapshot` / `--compare --strict`.

## Entering Rows by Hand

*Add a row* on the Orders page refuses a key that already exists, so a mistake cannot overwrite a
row. **Dates are `YYYY-MM-DD`**: Order Date is part of the key.

| Column | What to put in it |
|---|---|
| Cost Per Item | Total Cost ÷ Quantity (`total_cost_matches_quantity` warns if it doesn't reconcile) |
| Cashback Rate | The **total** rate earned, as one number (add base and bonus together) |
| Insurance | A **positive** cost; the profit formula subtracts it |
| Shipment | `1`, unless one order has two rows with the **same item name**: number them `1` and `2` by tracking number, or they collide on one key |
| Shipping | `0` if your costs are already all-in |
| Card | Type it: Card is derived from Card Last 4 only during a scrape |
| Total Profit, COGS | Nothing; both are computed from the row |

The Audit's `mandatory_by_stage` check expects a finished row to carry its Order Link, Delivery
Address, Card, Card Last 4, Profile and (from delivered on) a Receipt Link; leave what you don't have
blank and it will be listed. The next run re-sorts the ledger (`python -m scripts.sort_ledger
--apply` does it now); then check the Audit page or `python -m scripts.audit_ledger`.

**What the audit won't catch.** It catches mechanical errors (date spelling, duplicate keys, text in
number columns, negative amounts, unknown statuses) but not semantic ones: **1% where you meant
13.5% passes** `cashback_rate_sane`. Compare the computed Total Profit with your old records on two
or three rows per rate structure; if they agree to the cent, the mapping is right.
