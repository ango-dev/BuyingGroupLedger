# Importing history by hand

_Part of the [Buying Group Ledger](../README.md) docs._

Pasting finished orders straight into the sheet, and what the audit will and won't catch afterwards.

> **Only ever hand-import FINISHED orders** — `delivered`, `paid`, `return`, `cancelled`. Never import
> `ordered` or `shipped` rows. A single run already keeps those current: the scrapers discover open
> orders and re-check them to delivery on their own, so importing them by hand duplicates work the
> ledger does for free, and any detail you get slightly wrong (a re-worded item name, a different
> shipment number) becomes a duplicate row the next run appends beside yours. Terminal rows are the
> safe class precisely because nothing will ever re-read them — they're history, and no scraper will
> fight you over them.


## Importing a spreadsheet with `scripts/import_history.py`

For more than a handful of rows, use the importer instead of pasting. It is **dry-run by default**:

```bash
python -m scripts.import_history old.csv --rate-add "SUB Rate:SUB" --profile profile-alpha
python -m scripts.import_history old.csv --apply
```

It maps the source headers onto the ledger's columns (aliases plus `--map "Src=Target"`), converts
dates to ISO (refusing a file whose day/month order it cannot prove — `--date-format mdy|dmy`),
derives Cost Per Item, splits a cell holding several tracking numbers into one row per box, numbers
shipments per order, reads placeholders such as `Please fill` / `#VALUE!` as blank, and then does the
one thing pasting cannot: **it recomputes every source row's own profit figure from the mapped
inputs and refuses the import if any row disagrees by more than a cent.** That is what proves a
rate mapping (a sign-up-bonus rate that only applies when a flag column is TRUE, hence `--rate-add
COL:FLAG`) and an insurance sign before a cell is written. It then previews against the live sheet —
update vs append, orders the scrapers already recorded under other item names (skipped; the scraped
rows are authoritative), tracking numbers already held under another order — and writes a
normalised CSV under `data/`. Rows that are not terminal are refused (`--allow-open`); rows with no
cost are skipped so a later scrape can still fill that order (`--keep-no-cost`); rows with no
tracking number are accepted with a warning; rows with no order number get a synthetic one
(`BFMR-IMPORT-<date>`) so a referral bonus still reaches the tax report. `--apply` writes through
the same upsert every scrape uses and re-sorts; bracket it with `audit_sheet --save-snapshot` /
`--compare --strict`.

## Bulk-importing history by hand

Pasting a batch of finished orders straight into the sheet is fine, and in one way safer than routing
them through `sync_csv_to_sheet`: a paste doesn't go through the upsert, so a mistake can't silently
overwrite an existing row. Errors just sit there as rows, and the audit names them.

**Before you paste — format `Order Date`, `Delivery Date` and `Payout Date` as Plain text.** This is the
one hard-to-undo step, because a Date-typed `Order Date` changes the row's upsert key. Convert to
`YYYY-MM-DD` while you're there.

Then, per row:

| Column | What to put in it |
|---|---|
| `Cost Per Item` | `Total Cost ÷ Quantity` — `total_cost_matches_quantity` fails if it doesn't reconcile |
| `Cashback Rate` | the **total** rate earned, as one number. If your source tracks base and bonus rates in separate columns, add them together |
| `Insurance` | a **positive** cost. The profit formula subtracts it |
| `Shipment` | `1`, unless one order has two rows with the **same item name** — then number them by tracking number, `1` and `2`, or they collide on one upsert key |
| `Shipping` | `0` if your costs are already all-in |
| `Total Profit` | nothing — it's a formula, re-stamped on every sort |

Leave `Profile`, `Order Link`, `Tracking Link`, `Delivery Address`, `Card Last 4` and `Last Scraped At`
blank if you don't have them; none of it is read for a terminal row. Fill `Card` and `Cashback Rate`
directly, since `Card` is normally *derived* from `Card Last 4` and that only happens during a scrape.

Finish with `python -m scripts.sort_ledger --apply`, then `python -m scripts.audit_sheet`.

**What the audit will and won't catch.** It's a strong net for *mechanical* errors — Date-typed cells,
duplicate keys, `Cost Per Item` not reconciling, text in numeric columns, embedded newlines, blank
Order IDs, unknown statuses, non-integer Shipment. All FAIL-level, all named with a row number.

It is blind to *semantic* ones. `cashback_rate_sane` only checks that a rate is plausible, so **1%
where you meant 13.5% passes silently**, as does a negative Insurance. So verify those two by
arithmetic instead: after pasting, compare the sheet's computed `Total Profit` against the profit your
old records show, on two or three rows chosen to cover each rate structure you use. If those agree to
the cent, the rate and sign mapping is right everywhere. It takes two minutes and it's the only check
that proves the numbers rather than the shapes.
