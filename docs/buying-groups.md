# Buying groups — posting tracking, reading payouts

_Part of the [Buying Group Ledger](../README.md) docs._

`sync_tracking.py` posts each shipped package's tracking number to the right group and reads the payout back into **Insurance**, **Payout Amount** and **Payout Date**. Dry run by default.

Scraping tells you what you bought. The buying group is who pays you for it — so `sync_tracking.py`
posts each shipped package's tracking number to the right group, and reads their payout back into the
**Insurance**, **Payout Amount** and **Payout Date** columns, which is what makes **Total Profit**
light up (the formula stays blank until Payout Amount is filled).

> ### ⚠️ You still submit BFMR order numbers by hand
>
> **After placing an order against a BFMR reservation, enter its order number in BFMR yourself, right
> away.** This tool does not do it, and is not going to.
>
> That is not an oversight — it is what makes everything below work. BFMR keys on its own
> `reservation → purchase → shipment` chain, and your order number is what turns a *reservation* into
> a *purchase*. Until that happens there is nothing for a tracking number to attach to, so
> `sync_tracking` reports the package as having no purchase rather than guessing which deal you meant.
> A reservation also expires if its order number arrives late, and BFMR cancels a purchase whose
> tracking misses the deadline.
>
> Choosing the reservation automatically would mean matching a deal against a ledger row, and getting
> it wrong books the **wrong deal** — which cannot be undone here, because this tool never cancels
> anything at a buying group. You already know which deal you bought at the moment you buy it; typing
> the number then costs seconds and cannot go wrong.
>
> MaxOutDeals needs none of this — it keys on the tracking number alone, so posting is fully automatic.

**A filing carries the tracking number and nothing else.** BFMR's `insurance/file` also accepts
`address[...]` fields, but those are the **payee** address — where a claim pays out — not the
shipment's destination, so omitting them lets BFMR use the address on your account, which is already
the right answer. `name` and `package_value` are omitted for their own reasons: your account supplies
the name, and BFMR derives the value from the shipment's items, where declaring our own would risk
over-declaring and paying a bigger premium than the box warrants.

Nothing ever files a **jig** as a postal address, either. A jig is a deliberately misspelled variant
(`THIRTEEN SAMMPLE DR1VE`) that a group hands out so each order routes distinctly; it is a routing
token, and the only thing it is matched against is the `warehouses` config that sets the Buying Group
column.

Two groups are supported today, and they work nothing alike:

| | **BFMR** | **MaxOutDeals** |
|---|---|---|
| auth | `API-KEY` + `API-SECRET` headers | bearer token **+ an IP allowlist** |
| keyed on | its own reservation → purchase → shipment ids | the tracking number |
| order number | **you enter it, by hand, at order time** | not used |
| batching | one object per ledger row | one object per package (rows summed) |
| limits | undocumented | **10/day** payouts, **30/day** tracking |

```json
"buying_groups": {
  "bfmr": {
    "// ": "both from Developer Tools in your BFMR account settings",
    "api_key": "...",
    "api_secret": "...",
    "// min_insurance_value": "0 = insure every shipment",
    "min_insurance_value": 0
  },
  "mod": {
    "// ": "MOD also needs the account id + email it wants in the body of every request",
    "api_key": "...",
    "user_id": "...",
    "email": "..."
  }
}
```

(A key whose name starts with `//` is a comment — the loader strips them, so you can annotate your
own `config.json` the way `config.example.json` does.)

⚠️ **MaxOutDeals rejects any call from an unregistered IP**, however valid your token. Add the machine
that runs this under the **firewall tab** in your MOD profile — and again if you move hosts, change
ISP, or containerize it.

```bash
python -m scripts.bg_probe          # read-only recon; answers the open API questions
python -m sync_tracking             # DRY RUN — shows exactly what it would send. Sends nothing.
python -m sync_tracking --apply --limit 1     # one package per group, for the first live test
python -m sync_tracking --void 1Z999...       # undo a BFMR insurance filing
```

**Old orders with no tracking number** (Amazon stops showing them) can be filled from BFMR by order
number: `python -m scripts.backfill_tracking` (dry run; `--apply` writes), then
`python -m sync_tracking --apply --payouts-only --group BFMR`. MOD's report carries no retailer order
number, so MOD rows cannot be joined that way. **`--payouts-only`** reads payouts back and ticks what a
group already holds without submitting anything — the mode to use when the ledger carries orders from
buying-group accounts other than the connected ones.

**Routing is the Buying Group column**, which is already derived from the delivery address (see
"Warehouse / jig config"). A row goes to exactly one group. `Personal` orders never reach the sheet,
and `Unclassified` rows are **skipped and counted** rather than posted to a guess — an unconfigured
warehouse is a real warehouse, and sending someone else's package to the wrong group is worse than
leaving it visible.

**There's no "posted at" column, deliberately.** Whether a number has been submitted is something the
group knows and the sheet doesn't: MOD ignores duplicates by contract, and BFMR has a status
endpoint, so each run asks rather than keeping a local copy that drifts the moment a write fails or
you paste something into their dashboard by hand.

**A payout is split pro-rata** across the rows sharing a tracking number, for the same reason
order-level shipping is — a box holding two items is two rows, and writing the whole payout to each
would book it twice.

**Status advances to `paid` or `return`.** Those are the two outcomes a retailer can never tell you
about, so the buying group is the authority on them:

- **BFMR reports both directly** — its tracker carries `paid` and `returned` per package.
- **MaxOutDeals confirms payment by listing a package in its received-items report**; there is no
  finer signal. ⚠️ **MOD gives no return signal at all**, so a returned MOD package will keep reading
  `paid` until you set its Status to `return` on the sheet **by hand**. That correction is safe: a
  status only ever moves forward, so later runs won't undo it.
- Everything earlier in the journey (`shipped`, `delivered`) stays the retailer's to report — if both
  sources wrote it, they'd overwrite each other every run.

**Payout Amount fills early, with BFMR's committed price** (2026-09-11). BFMR's tracker carries the
payout price it has committed to (`payout_price`/`total_payout`) from the moment a purchase exists —
before shipping, before payment — so the sync writes it the moment your hand-typed order number
links the reservation, prorated by Total Cost across the order's rows, with **no Payout Date**. The
blank date (beside a non-terminal Status) is what says "committed, not settled", and Total Profit
consequently shows the *projected* profit on open BFMR rows. Two alerts come with it: if a later
run finds BFMR **changed** the committed price, the cells are updated to the new figure and the
alert names old → new; and if the **settled** amount disagrees with the commitment, the run that
writes the settlement says so (the paid figure still lands as-is). The settlement — real amount,
Payout Date, `paid` status — always overwrites the commitment, and the commitment pass never
touches a row that is `paid`/`return`, carries a Payout Date, or is being settled in the same run.
MOD's API publishes no price, so MOD cells stay blank until MOD actually pays; a `0` is still never
written — an unpayable figure leaves the cell alone rather than fabricating a loss.

**`Tracking Submitted`** is a checkbox: ticked when the buying group holds that package's tracking
number. Format the column as a checkbox in Sheets and it renders as a tick — the values are real
booleans. It's for reading, not for deciding: what's already been submitted is still re-derived from
the group on every run, so a failed sheet write can't strand a package. An unticked box next to a
shipped row is the thing worth noticing. Boxes are never cleared automatically.

**Insurance** is filled from the group's own premium line. A package that's been paid out and shows
no premium records a real `0`; one still in transit is left blank, since the premium may not be
posted yet. An inferred `0` never overwrites a figure you typed yourself.

**BFMR insurance is filed automatically** when enabled. It never declares a package value (BFMR works
it out from the shipment, so there's no way to over-declare and overpay), never files twice, and only
covers shipments worth at least `BFMR_MIN_INSURANCE_VALUE`. The **premium** is read back into the
Insurance column and the **gross** payout into Payout Amount — rather than netting the two — so the
deduction is visible rather than silently shrinking your payout. MOD never charges a premium, so its
rows record a real `0`.

> **Best Buy sometimes ships several orders in one carton** under a single tracking number. BFMR
> allows a number once, so the second order is rejected and
> [asks you to append B/C/D](https://support.bfmr.com/hc/en-us/articles/50968170907547) until it's
> accepted — their record then reads `529900000009B` where your ledger reads `529900000009`.
>
> You get an **ACTION NEEDED** alert naming the three manual steps: add the tracking by hand (with
> the order number on it), **file the insurance by hand**, and raise a support ticket with proof of
> purchase. The tool won't do any of them for you — until the tracking exists, BFMR has no shipment
> to insure, and an automatic filing would post against nothing while reporting success.
>
> There's nothing to edit on the sheet. The next run finds whichever letter you used, ticks
> Tracking Submitted, and fills in the payout, premium and status as they arrive.

Once you've done a dry run and a one-package live test, set `BUYING_GROUP_SYNC_ENABLED=true` to let the
scheduled run do it too — it's off by default because it spends real money unattended.
