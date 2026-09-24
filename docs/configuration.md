# Configuration

_Part of the [Buying Group Ledger](../README.md) docs._

One authored file, an optional override layer, and the two sections (warehouses, cards) that turn
addresses and card digits into ledger columns. The Settings page edits all of it from the browser.

## `config.json` and the Environment

| File | What it is |
|---|---|
| `config.json` | **The whole setup**: credentials, profiles, warehouse jigs, cards, schedule, where the ledger lives. You author it (or the Settings page does). Gitignored. `config.example.json` documents every key beside it. |
| `.state.json` | The app's own file: Costco's rotating refresh token and a few run records (the setup wizard's, the BFMR auto-reply's). Never edit it; deleting it costs a re-run of `scripts.costco_token`. |
| `.env` | Optional **override layer**. Every value in `config.json` can be overridden by an environment variable, from `.env` or the shell; nothing is environment-only. |

```bash
BFMR_MIN_INSURANCE_VALUE=999 python -m sync_tracking     # just this run
LOOKBACK_DAYS=14 python main.py amazon                   # re-scan a wider window
```

Use `.env` for values that differ on *this machine* (a scratch `LEDGER_DB_PATH`, a host's own
`RUN_INTERVAL_HOURS`). A blank value (`FOO=`) does **not** override.

The container's knobs come from the file too: `docker/entrypoint.sh` resolves `container.*` and
`backups.*` through `scripts/container_settings.py` on start, and an exported variable still wins.

A key whose name starts with `//` is a comment; the loader strips it, and the Settings page shows
`config.example.json`'s comments as help text.

<details>
<summary><b>Every environment variable, and the <code>config.json</code> key it overrides</b></summary>

Generated from `ENV_TO_CONFIG` in [config/settings.py](../config/settings.py), the single place a
name is mapped; `tests/test_config_loader.py` fails if this table drifts from it. **Booleans (†)
take `true` / `false`** in both places. `1`, `yes` and `on` still parse, but anything unrecognised
is **false**, so a switch that spends money fails closed on a typo.

| Variable | `config.json` key |
|---|---|
| `BROWSER_USE_API_KEY` | `browser_use.api_key` |
| `DISCORD_ALERTS_ENABLED` † | `alerts.discord_enabled` |
| `DISCORD_WEBHOOK_URL` | `alerts.discord_webhook_url` |
| `GMAIL_ALERTS_ENABLED` † | `alerts.gmail_enabled` |
| `ALERT_EMAIL_TO` | `alerts.email_to` |
| `CASHBACK_CAP_WARN_PERCENT` | `alerts.cap_warn_percent` |
| `CASHBACK_CAP_WARN_DOLLARS` | `alerts.cap_warn_dollars` |
| `GMAIL_ADDRESS` | `alerts.gmail_address` |
| `GMAIL_APP_PASSWORD` | `alerts.gmail_app_password` |
| `AMAZON_GIFT_CARD_NETTING_ENABLED` † | `scraping.amazon_gift_card_netting_enabled` |
| `AMAZON_PROMO_CASHBACK_ENABLED` † | `scraping.amazon_promo_cashback_enabled` |
| `DEFAULT_CASHBACK_RATE` | `scraping.default_cashback_rate` |
| `LOOKBACK_DAYS` | `scraping.lookback_days` |
| `BFMR_ENABLED` † | `buying_groups.bfmr.enabled` |
| `BFMR_API_BASE_URL` | `buying_groups.bfmr.api_base_url` |
| `BFMR_API_KEY` | `buying_groups.bfmr.api_key` |
| `BFMR_API_SECRET` | `buying_groups.bfmr.api_secret` |
| `BFMR_MIN_INSURANCE_VALUE` | `buying_groups.bfmr.min_insurance_value` |
| `BFMR_COSTCO_TV_ORDER_NUMBER_AS_TRACKING` † | `buying_groups.bfmr.costco_tv_order_number_as_tracking` |
| `BFMR_COSTCO_TV_ITEM_PATTERN` | `buying_groups.bfmr.costco_tv_item_pattern` |
| `BFMR_COMBINED_PACKAGE_AUTOREPLY_ENABLED` † | `buying_groups.bfmr.combined_package_autoreply_enabled` |
| `BFMR_COMBINED_PACKAGE_SENDER_DOMAINS` | `buying_groups.bfmr.combined_package_sender_domains` |
| `BFMR_COMBINED_PACKAGE_REPLY_CC` | `buying_groups.bfmr.combined_package_reply_cc` |
| `BFMR_COMBINED_PACKAGE_GMAIL_ADDRESS` | `buying_groups.bfmr.combined_package_gmail_address` |
| `BFMR_COMBINED_PACKAGE_GMAIL_APP_PASSWORD` | `buying_groups.bfmr.combined_package_gmail_app_password` |
| `MAXOUTDEALS_ENABLED` † | `buying_groups.mod.enabled` |
| `MAXOUTDEALS_API_BASE_URL` | `buying_groups.mod.api_base_url` |
| `MAXOUTDEALS_API_KEY` | `buying_groups.mod.api_key` |
| `MAXOUTDEALS_EMAIL` | `buying_groups.mod.email` |
| `MAXOUTDEALS_USER_ID` | `buying_groups.mod.user_id` |
| `BUYING_GROUP_SYNC_ENABLED` † | `buying_groups.sync_enabled` |
| `RECEIPT_CAPTURE_ENABLED` † | `receipts.capture_enabled` |
| `RECEIPTS_DIR` | `receipts.dir` |
| `PREFLIGHT_STRICT` † | `container.preflight_strict` |
| `RUN_INTERVAL_HOURS` | `container.run_interval_hours` |
| `RUN_ON_START` † | `container.run_on_start` |
| `TZ` | `container.timezone` |
| `WEB_ENABLED` † | `web.enabled` |
| `WEB_LEDGER_SOURCE` | `web.ledger_source` |
| `WEB_SNAPSHOT_PATH` | `web.snapshot_path` |
| `WEB_LEDGER_CACHE_TTL_SECONDS` | `web.ledger_cache_ttl_seconds` |
| `WEB_BIND_HOST` | `web.bind_host` |
| `WEB_PORT` | `web.port` |
| `WEB_TOOL_SESSION_MINUTES` | `web.tool_session_minutes` |
| `WEB_HEARTBEAT_STALE_HOURS` | `web.heartbeat_stale_hours` |
| `WEB_PUBLIC_URL` | `web.public_url` |
| `WEB_ALLOWED_HOSTS` | `web.allowed_hosts` |
| `WEB_PASSWORD` | `web.password` |
| `WEB_SESSION_HOURS` | `web.session_hours` |
| `WEB_REMEMBER_DAYS` | `web.remember_days` |
| `WEB_LOGIN_ATTEMPTS` | `web.login_attempts` |
| `WEB_LOGIN_LOCKOUT_MINUTES` | `web.login_lockout_minutes` |
| `LEDGER_DB_PATH` | `database.path` |
| `BACKUP_ENABLED` † | `backups.enabled` |
| `BACKUP_FREQUENCY` | `backups.frequency` |
| `BACKUP_TIME` | `backups.time` |
| `BACKUP_DAYS` | `backups.days` |
| `BACKUP_KEEP` | `backups.keep` |

</details>

**Coming from the old separate files** (`profiles.json`, `warehouses.json`, `cards.json`,
`.costco/*.json`, a full `.env`)? `python -m scripts.migrate_config` (dry run), then `--apply`,
folds them into `config.json` + `.state.json` without deleting them; preflight warns while they linger.

**The ledger** is `data/ledger.sqlite3` (`database.path`, relative to the repo root; in Docker on
the mounted `data/` volume). It is created on first use. From the Settings page it can be moved
only within `data/`; a path set by hand in `config.json` stays.

**Test alerts** before relying on them:
```bash
.venv/bin/python -m alerts.notifier      # Windows: .venv\Scripts\python -m alerts.notifier
```


## Warehouse and Jig Config

The **`warehouses`** section classifies each order by the buying-group warehouse it shipped to:

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

Each group lists **jigs**, the address variants it routes packages through. A jig matches when
**every** field it sets (`street`, `zip`, `name_contains`, or a `contains: [...]` list) appears in
the normalized delivery address (lowercase, no punctuation or extra spaces). The first match in
file order sets the row's Buying Group. A jig with no match fields is rejected.

| Result | What happens |
|---|---|
| Matches a **`Personal`** jig | The order is **dropped**, never recorded. List every personal address you use. |
| Matches no jig | Tagged **`Unclassified`**, kept, and counted in the run log, so a forgotten warehouse stands out. |
| No `warehouses` section | Every non-blank address is `Unclassified`. |

- **BFMR, MOD and Personal are built in**: the code routes and submits by those names (spellings
  such as `MaxOutDeals` fold to `MOD`; `config/buying_group_names.py`). Settings › Warehouses always
  shows their cards (an empty one is written to the file on its first save), their Buying Group
  name is fixed, and they have no Delete; renaming or removing one is refused there and in the JSON
  editor. Only a warehouse you added has an editable name.
- Edited jigs re-tag **open** orders on the next run. Delivered rows keep their tag unless
  `python -m scripts.retag_buying_groups --apply` re-tags the whole ledger (it also deletes rows
  that now match `Personal`, after saving them to a CSV).


## Card and Cashback Config

Every scraper captures the last 4 of the card an order was charged to. The **`cards`** section
turns those digits into a Card name and a Cashback Rate:

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

A row's rate resolves most specific first:

1. the card's **`retailer_rates`** entry for the row's retailer
2. the card's **`cashback_rate`** (its "everywhere else" rate)
3. **`scraping.default_cashback_rate`**, for a card not configured at all

Details:

- Rates are **decimal fractions** (`0.015` = 1.5%) or percent strings (`"1.5%"`). A bare `2` is
  **rejected**: it reads equally as 2% or 200%. The Settings page shows and writes percents.
- Retailers are spelled one way, by name: `Amazon`, `Amazon Business`, `Best Buy`, `Costco`
  (`models/retailers.py`), in a profile's `retailers` and `auth` keys and a card's `retailer_rates`
  and `caps`. Older spellings (`"bestbuy"`, `"best-buy"`) still load;
  `python -m scripts.standardize_retailers --apply` (also under Tools) rewrites them. A key naming
  no scraped retailer logs a warning at load, so a typo'd override doesn't fail silently.
- `last4` is matched **normalized** ("ending in 4321", `************4321`, `xxxx4321` are all 4321).
- Two cards in different accounts can share a last 4: **`profile`** (a profile label) scopes an
  entry, and the scoped entry wins; an ambiguous duplicate logs a warning.
- An unconfigured card gets a blank Card (a hand-typed name survives) and the default rate.
- Edits re-derive **open** orders on the next run; delivered rows keep their values, except under a
  spend cap (below).

**Card Type** (the menu on Settings › Cards):

| Type | In `config.json` | Behaviour |
|---|---|---|
| Regular | (default) | Its own rates and caps. |
| Virtual | `virtual: true`, `virtual_of: "<its card's last 4>"` | Earns its card's rates and pools spend against its caps; no rates of its own, no sign-up bonus on Taxes. Without `virtual_of` it loads with a warning and uses its own rates. |
| Employee | virtual + `own_bonus: true` | As Virtual, but Taxes asks for its own sign-up bonus (an Amex employee card). |

**Archiving.** `archived: true` (Archive / Restore in Settings › Cards) marks a card no longer in
use. Its old orders keep their card name and rate and stay off the Audit; it folds under Archived,
no virtual number can pick it, and its limits send no warnings. Its virtual numbers follow it.

### Spend Caps

A boosted rate usually runs out (5% on the first $120k of Amazon spend a year, say). **`caps`**
say so:

```json
{ "last4": "5555", "name": "Amazon Business Prime", "cashback_rate": "1%",
  "retailer_rates": { "Amazon": "5%", "Amazon Business": "5%" },
  "caps": [ { "retailers": ["Amazon", "Amazon Business"], "spend_limit": 120000,
              "fallback_rate": "1%", "resets": "calendar-year",
              "outside_spend": [ { "date": "2026-02-03", "amount": 4000, "note": "personal" } ] } ] }
```

| Field | Meaning |
|---|---|
| `retailers` | The retailers sharing ONE allowance. Empty = the catch-all (every retailer without its own cap). One catch-all per card; a retailer in one cap only. A limit named for Amazon covers Amazon Business and back, unless each has its own cap. |
| `spend_limit` | Dollars per period at the boosted rate. Spend is what the card was charged for a row (Total Cost + Shipping + Sales Tax − Gift Card − Rewards Used); a return gives Return Qty × Cost Per Item back in the period of its Return Date. |
| `fallback_rate` | The rate past the limit. Blank = the card's `cashback_rate`, else the global default. The purchase that crosses the line gets the exact blend. |
| `resets` | `calendar-year` (default), `never`, or an `MM-DD` the period starts on. |
| `outside_spend` | A dated log (`{date, amount, note}`) of spend the ledger never sees; a negative amount takes spend back. Each period sums the entries dated inside it. The older per-period shape (`"2026": 4000`) still loads, dated at the period's start. |

**When it applies.** The rate is set at scrape time, with the batch placed among the card's rows for
the period in Order Date order. After every sync, rows **past shipped** (delivered, paid, return)
on a capped card are re-derived and rewritten where a late order or a return moved the line: the
one case where a rate cell is refreshed, so keep a capped card's rates current. A hand-typed rate
cell is never touched. An Amazon promo (Promo Rate) rides on top of the capped rate.

**Warnings.** After every sync each cap alerts once per state per period: *close* when `alerts.cap_warn_percent` of the limit is spent or
`alerts.cap_warn_dollars` or less is left (0 switches one off), *reached* once passed. It is its
own acknowledgeable activity kind, *Spend limit*, with a card on the overview. The Settings page
edits a card's caps as one Rates table (the "everywhere else" row is the card's rate and the
catch-all cap) and shows a Left This Period bar on each capped row.
