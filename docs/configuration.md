# Configuration

_Part of the [Buying Group Ledger](../README.md) docs._

One authored file, an optional override layer, and the two sections (warehouses, cards) that turn addresses and card digits into ledger columns.

## `config.json` and the environment

**`config.json` is the whole setup**: credentials, profiles, warehouse jigs and card cashback rates,
plus the Google service-account key inlined. It is gitignored. The example file documents every key
next to it, so this is the only thing to read.

> **Environment variables override every value in it**, from `.env` or the shell — the name is in
> each key's `// note`. **Nothing is environment-only**, so `.env` is entirely optional; it exists
> purely as the override layer, which is where dev and host-specific values belong:
>
> ```bash
> COSTCO_FORCE_AGENT=true python main.py costco               # exercise the paid agent fallback, once
> BFMR_MIN_INSURANCE_VALUE=999 python -m sync_tracking     # just this run
> LOOKBACK_DAYS=14 python main.py amazon                   # re-scan a wider window
> ```
>
> Put something in the `.env` FILE when it should differ on *this machine* — a dev box pointed at a
> scratch `GOOGLE_SHEET_ID`, or a host that runs on its own `RUN_INTERVAL_HOURS`. A blank value
> (`FOO=`) does **not** override; it falls through to `config.json`, so commenting a line out works
> the way you would expect.
>
> Even the container's own knobs come from the config file. `docker-compose.yml` interpolates its
> variables before any Python runs, so `docker/entrypoint.sh` resolves `container.*`
> (`run_interval_hours`, `run_on_start`, `preflight_strict`, `timezone`) through
> `scripts/container_settings.py` on start — and still lets an exported variable win.

<details>
<summary><b>Every environment variable, and the <code>config.json</code> key it overrides</b> (39 of them)</summary>

The list is generated from `ENV_TO_CONFIG` in [config/settings.py](../config/settings.py), which is the
single place a name is mapped, and `tests/test_config_loader.py` fails if this table drifts from it.
**Booleans (marked †) take `true` / `false`** — in the config file and the environment alike. `1`,
`yes` and `on` still parse, but anything unrecognised is **false**, so a switch that spends money
fails closed on a typo rather than turning itself on.

| Variable | `config.json` key |
|---|---|
| `BROWSER_USE_API_KEY` | `browser_use.api_key` |
| `BROWSER_USE_LLM` | `browser_use.llm` |
| `BROWSER_USE_MAX_COST_USD` | `browser_use.max_cost_usd` |
| `AGENT_FALLBACK_ENABLED` † | `browser_use.agent_fallback_enabled` |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | `google.service_account_file` |
| `GOOGLE_SHEET_ID` | `google.sheet_id` |
| `GOOGLE_SHEET_WORKSHEET_NAME` | `google.worksheet_name` |
| `DISCORD_WEBHOOK_URL` | `alerts.discord_webhook_url` |
| `ALERT_EMAIL_TO` | `alerts.email_to` |
| `GMAIL_ADDRESS` | `alerts.gmail_address` |
| `GMAIL_APP_PASSWORD` | `alerts.gmail_app_password` |
| `AMAZON_GIFT_CARD_NETTING_ENABLED` † | `scraping.amazon_gift_card_netting_enabled` |
| `AMAZON_PROMO_CASHBACK_ENABLED` † | `scraping.amazon_promo_cashback_enabled` |
| `DEFAULT_CASHBACK_RATE` | `scraping.default_cashback_rate` |
| `LOOKBACK_DAYS` | `scraping.lookback_days` |
| `BFMR_API_BASE_URL` | `buying_groups.bfmr.api_base_url` |
| `BFMR_API_KEY` | `buying_groups.bfmr.api_key` |
| `BFMR_API_SECRET` | `buying_groups.bfmr.api_secret` |
| `BFMR_MIN_INSURANCE_VALUE` | `buying_groups.bfmr.min_insurance_value` |
| `MAXOUTDEALS_API_BASE_URL` | `buying_groups.mod.api_base_url` |
| `MAXOUTDEALS_API_KEY` | `buying_groups.mod.api_key` |
| `MAXOUTDEALS_EMAIL` | `buying_groups.mod.email` |
| `MAXOUTDEALS_USER_ID` | `buying_groups.mod.user_id` |
| `BUYING_GROUP_SYNC_ENABLED` † | `buying_groups.sync_enabled` |
| `RECEIPT_CAPTURE_ENABLED` † | `receipts.capture_enabled` |
| `OCI_BUCKET` | `receipts.oci.bucket` |
| `OCI_PAR_URL_PREFIX` | `receipts.oci.par_url_prefix` |
| `DOSSIER_UPLOAD_ENABLED` † | `receipts.dossier_upload_enabled` |
| `OCI_S3_ACCESS_KEY_ID` | `receipts.oci.s3_access_key_id` |
| `OCI_S3_ENDPOINT_URL` | `receipts.oci.s3_endpoint_url` |
| `OCI_S3_REGION` | `receipts.oci.s3_region` |
| `OCI_S3_SECRET_ACCESS_KEY` | `receipts.oci.s3_secret_access_key` |
| `PREFLIGHT_STRICT` † | `container.preflight_strict` |
| `RUN_INTERVAL_HOURS` | `container.run_interval_hours` |
| `RUN_ON_START` † | `container.run_on_start` |
| `TZ` | `container.timezone` |
| `AMAZON_FORCE_AGENT` † | `dev.force_agent.amazon` |
| `AMAZON_BUSINESS_FORCE_AGENT` † | `dev.force_agent.amazon_business` |
| `BESTBUY_FORCE_AGENT` † | `dev.force_agent.bestbuy` |
| `COSTCO_FORCE_AGENT` † | `dev.force_agent.costco` |

</details>

**Already have the old six files?** `python -m scripts.migrate_config` (dry run, secrets masked),
then `--apply`. It folds `.env`, `profiles.json`, `warehouses.json`, `cards.json`,
`service_account.json` and `.costco/*.json` into `config.json` + `.state.json`, and never deletes the
originals — so it is reversible by deleting `config.json`. Delete them yourself once a run has proven
the new file works; preflight warns while they linger, because nothing reads them any more.

**Google Sheet** — create a Google Cloud service account, paste its whole JSON key into
`google.service_account`, and **share the sheet** with that key's `…@…iam.gserviceaccount.com` email
as Editor. Put the sheet ID (from its URL) in `google.sheet_id`. (`GOOGLE_SERVICE_ACCOUNT_FILE` still
points at a standalone file instead, and wins when set.)

**`.state.json`** is the app's own file — currently just Costco's rotating refresh token. You never
edit it, and deleting it only costs a re-run of `scripts.costco_token`. It is separate from
`config.json` precisely so the config you author can stay read-only in Docker.

**Test alerts** before relying on them:
```bash
.venv/bin/python -m alerts.notifier      # Windows: .venv\Scripts\python -m alerts.notifier
```


## Warehouse and jig config

To classify each order by which buying group's warehouse it shipped to, fill the **`warehouses`**
section of `config.json` — `config.example.json` has a commented starting point:

```json
"warehouses": [
  { "buying_group": "BFMR",
    "jigs": [
      { "label": "BFMR-A", "street": "123 Main St", "zip": "10001", "name_contains": "c/o BFMR" },
      { "label": "BFMR-B", "street": "500 Warehouse Blvd", "zip": "07004" }
    ] },
  { "buying_group": "Personal",
    "jigs": [ { "label": "home", "zip": "94103", "name_contains": "Your Name" } ] }
]
```

Each buying group lists one or more **jigs** — the address variants it routes packages through. A jig
matches an order when **every** substring field it sets (`street` / `zip` / `name_contains`, or a generic
`contains: [...]` list) appears in the delivery address after normalization (lowercased, punctuation and
extra spaces removed — so "c/o" vs "c o" and "Ste." vs "Ste" don't matter). The first matching jig (in
file order) wins and its `buying_group` is written to the row.

- List your own reship address under a group literally named **`Personal`** — those orders are **dropped
  from the sheet entirely** (never recorded). List every personal address you use, or the order will fall
  through to `Unclassified` and still show.
- An address matching **no** jig is tagged **`Unclassified`** (kept, not dropped), and the run logs how
  many — a real warehouse you forgot to add stands out instead of silently vanishing. A jig with no match
  fields is rejected (it would match everything).
- No `warehouses` section at all = every non-blank address is `Unclassified` (nothing is guessed).
- Editing the file re-tags **open** orders on the next run (they get re-read); already-delivered rows
  keep their tag. Classification is offline and free — no live run is needed to change it.


## Card and cashback config

Every scraper already captures the last 4 digits of the card an order was charged to. The **`cards`**
section of `config.json` turns those digits into a card name and a cashback rate:

```json
"cards": [
  { "last4": "4321", "name": "Chase Freedom Unlimited", "cashback_rate": 0.015,
    "retailer_rates": { "Amazon": "5%", "Best Buy": "3%" } },
  { "last4": "8765", "name": "Citi Double Cash", "cashback_rate": "2%" },
  { "last4": "1111", "name": "Amex Business Platinum",
    "retailer_rates": { "Amazon Business": "5%" } },
  { "last4": "1111", "name": "Personal Amex Gold", "cashback_rate": "4%", "profile": "profile-1" }
]
```

**Each card gets an overall rate plus optional per-retailer overrides**, because a card's earn rate is
category-dependent in practice. The rate for a row resolves in three tiers, most specific first:

1. the card's **`retailer_rates`** entry for that row's retailer — this card, at this store
2. the card's **`cashback_rate`** — its overall rate everywhere else
3. **`DEFAULT_CASHBACK_RATE`** (`scraping.default_cashback_rate`) — for cards you haven't
   configured at all

Details:

- Rates are **decimal fractions** (`0.015` = 1.5%); `"1.5%"` is accepted and converted, in both
  `cashback_rate` and `retailer_rates`. A bare `2` is **rejected** rather than guessed at — it reads
  equally as 2% or 200%, and picking wrong would misstate every profit number by 100×.
- `retailer_rates` keys are matched loosely: `"Best Buy"`, `"bestbuy"` and `"best-buy"` are the same
  key. A key that names **no** retailer this ledger scrapes logs a warning at load — a typo'd override
  would otherwise never apply and nothing would say so.
- `last4` is matched **normalized**, so it doesn't matter that Amazon says "ending in 4321", Best Buy
  sends `************4321`, and Costco sends `xxxx4321`.
- Two *different* cards can genuinely share a last 4 across accounts. Add an optional **`profile`** (a
  a `profiles` label) to scope an entry; the scoped entry wins over the catch-all, and genuinely
  ambiguous duplicates log a warning rather than one being silently picked. (There's no `retailer`
  scope — one physical card is used at many retailers, and what varies per retailer is the *rate*.)
- A card that's charged but **not configured** gets a blank Card name (so the gap is visible, and a
  name you type by hand survives) and the default rate. No `cards` section at all = every row gets the
  default rate and no name.
- Like the warehouse config, this is offline and free — editing it re-derives the columns for **open**
  orders on the next run. Delivered rows are terminal and keep what they were tagged with.
