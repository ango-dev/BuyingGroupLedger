# Buying Groups: Posting Tracking, Reading Payouts

_Part of the [Buying Group Ledger](../README.md) docs._

`sync_tracking.py` posts each shipped package's tracking number to its buying group, files BFMR
insurance, and reads the group's figures back into **Expected Payout**, **Insurance**, **Actual
Payout**, **Payout Date** and **Status**. Total Profit stays blank until Actual Payout is filled.
It is a **dry run by default**.

> ### You still submit BFMR order numbers by hand
>
> **After ordering against a BFMR reservation, enter the order number in BFMR yourself, right
> away.** Your order number turns a reservation into a purchase; until then a tracking number has
> nothing to attach to and the sync reports "no purchase". The tool does not pick the reservation
> for you: a wrong guess books the **wrong deal**, and it never cancels anything at a group. (A
> reservation also expires if the number arrives late.) MaxOutDeals keys on the tracking number
> alone and needs none of this.

| | **BFMR** | **MaxOutDeals (MOD)** |
|---|---|---|
| Auth | `API-KEY` + `API-SECRET` headers | bearer token **+ an IP allowlist** |
| Keyed on | its reservation → purchase → shipment ids | the tracking number |
| Order number | **you enter it at order time** | not used |
| Batching | one object per ledger row | one object per package (rows summed) |
| Limits | undocumented | **30 tracking calls/day**, **10 payout reads/day** |
| Payout price before settlement | yes (Expected Payout) | no |
| Return signal | yes | **none** |
| Insurance | filed by the sync (a premium per shipment) | none, Insurance records `0` |

## Setup

In `config.json` (or Settings › Buying Groups; the setup wizard asks for the keys too):

```json
"buying_groups": {
  "sync_enabled": false,
  "bfmr": { "enabled": true, "api_key": "...", "api_secret": "...", "min_insurance_value": 0 },
  "mod":  { "enabled": true, "api_key": "...", "user_id": "...", "email": "..." }
}
```

- BFMR's key and secret are under Developer Tools in its account settings; MOD also wants the
  account id and email.
- **MOD rejects any call from an unregistered IP**, however valid the token. Add the host under the
  **firewall tab** in your MOD profile (again after moving hosts or ISPs); the push alerts if not.
- **`buying_groups.sync_enabled`** lets the scheduled run do all this unattended. Off by default
  because it spends real money; turn it on after a dry run and a one-package live test.
- **Each group has its own switch**, `buying_groups.bfmr.enabled` / `buying_groups.mod.enabled`
  (`BFMR_ENABLED` / `MAXOUTDEALS_ENABLED`, default on). A group switched off is skipped entirely
  while the other runs; an explicit `--group` overrides it.

```bash
python -m scripts.bg_probe                     # read-only recon of both APIs
python -m sync_tracking                        # DRY RUN: prints what it would send
python -m sync_tracking --apply --limit 1      # one package per group, for a first live test
python -m sync_tracking --apply --group BFMR   # one group only
python -m sync_tracking --apply --payouts-only # read payouts and tick what is held; submit nothing
python -m sync_tracking --void 1Z999AA10000000001 --apply   # cancel a BFMR insurance filing
```

`--payouts-only` suits a ledger holding orders from group accounts other than the connected ones.
Old orders with no tracking number can be filled from BFMR by order number with
`python -m scripts.backfill_tracking` (dry run; `--apply` writes); MOD's report has no order number.

## Routing

The **Buying Group** column (set by the [warehouse jigs](configuration.md#warehouse-and-jig-config))
sends a row to exactly one group. `Gift Card` rows are unrouted on purpose; **`Unclassified` rows
are skipped**, never posted to a guess. A shipped row whose group is not set up is listed on
Activity every run and alerted once per group name.

## What the Sync Writes

**Tracking Submitted** is ticked when the group holds the package's number. It is for reading:
what to send is asked of the group every run (MOD ignores duplicates; BFMR lists what it holds), so
a failed ledger write cannot strand a package; a paid package is not re-sent. An unticked box on a
shipped row is worth a look. Only a carrier re-label clears it.

**Expected Payout** is BFMR's committed price, written as soon as your hand-typed order number links
the reservation, before shipping. It is prorated by Total Cost across the order's rows; once BFMR
holds a tracking number for a purchase, the commitment is scoped to that shipment's rows. If BFMR
changes the price, the cells follow and an alert names old → new. MOD publishes no price (blank,
never `0`).

**Actual Payout, Payout Date and `paid`** come from the settlement: BFMR's tracker, or a package
appearing in MOD's received-items report. A payout per package is **split pro-rata by Total Cost**
across the rows sharing its tracking number, so a two-item box is not paid twice. The settlement
never touches Expected Payout; if the two disagree the run says so, and the order stays on the
dashboard's **Reconciliation** page.

**Status** moves forward only, to `paid` or `return`, the two outcomes a retailer never reports.
BFMR reports both. **MOD reports no returns**: set a returned MOD row's Status to `return` by hand
on the dashboard; later runs will not undo it. `shipped` and `delivered` stay the retailer's to
report.

**Insurance** is the group's premium: a paid package with none records `0`, one in transit stays
blank. A cell typed on the dashboard is never overwritten by the sync.

## BFMR Insurance

With `--apply` (or the scheduled sync), BFMR insurance is filed automatically for each shipment
worth at least `buying_groups.bfmr.min_insurance_value` (0 = every shipment), never twice. A filing
carries **only the tracking number**: BFMR derives the value from the shipment's items (so nothing
is over-declared) and uses the name and payee address on your account. The **premium** goes to
Insurance and the **gross** payout to Actual Payout, so the deduction stays visible.

## Special Cases

**Best Buy combined cartons.** Best Buy sometimes ships several orders under one tracking number.
BFMR allows a number once and
[asks you to append a letter](https://support.bfmr.com/hc/en-us/articles/50968170907547); the sync
does that itself, trying `B`, `C`, … until BFMR accepts one, confirms it by re-reading the tracker,
and files insurance under that spelling. The ledger keeps the bare number. Only if no letter works
do you get an **Action needed** alert: add the number to the purchase in My Tracker by hand and
raise a BFMR ticket with proof of purchase. A box attaches only to the purchases its quantity can
hold; one that fits none is an Action needed alert, never dropped.

**The combined-package auto-reply.** BFMR then emails for the carton's serial numbers and the
Best Buy receipt PDF. `python -m respond_bfmr` (dry run; `--apply` sends) matches each email to the
ledger, fetches the serials live from Best Buy (a paid browser session, `--apply` only) and replies
with the receipts from `data/receipts/`. Any gap blocks that reply and raises one alert.
`buying_groups.bfmr.combined_package_autoreply_enabled` (off by default) lets the scheduled run do
it; turn it on after a supervised `--apply --limit 1`. It reads and sends as **its own Gmail
account** (`combined_package_gmail_address` / `_app_password`), never the alerts one, even when it is
the same mailbox; either blank with the switch on fails preflight.

**Costco TVs.** BFMR takes a Costco TV's order number as its tracking number. With
`costco_tv_order_number_as_tracking` on (default), a Costco row routed to BFMR whose item matches
`costco_tv_item_pattern` (default `\bTV\b`) carries its order number as Tracking Number from
`ordered` on, and keeps it when Costco reports a carrier number; Status still follows Costco.

**Cancelled orders.** When the retailer cancels an order BFMR still holds, the run alerts; it never
cancels anything at a group, because that would give up the reservation. A partial cancellation
alerts only while BFMR holds more units than are still coming.
