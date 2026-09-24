# Operations — scheduling, Docker, the dashboard, moving hosts

_Part of the [Buying Group Ledger](../README.md) docs._

> **[DEPLOY.md](../DEPLOY.md) is the runbook for a dedicated Linux host** (Ubuntu VM, Raspberry Pi):
> container settings, choosing an interval, monitoring and cutover live there. This page is the
> desktop path, the dashboard, and the short version of the rest.

## Automatic running

**Linux (cron):**
```bash
./scripts/install_cron.sh          # every 6h -> 00:00 / 06:00 / 12:00 / 18:00
./scripts/install_cron.sh 4        # every 4h (re-run with any interval to change it)
```

**Windows (Task Scheduler)** — elevated PowerShell:
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_task_windows.ps1 -Hours 6
```

Both call the platform runner (`run.sh` / `run.ps1`), which `cd`s to the project root and appends to
`logs/cron.log`. The two runners behave identically:

- they rotate `logs/cron.log` at 10 MB, so it never fills a small disk;
- they always record how a run *ended*, exit code included;
- they stamp `logs/.last_run`, the heartbeat the container healthcheck also uses. A stale heartbeat
  means the scheduler stopped firing, a failure that is otherwise silent.

Check the heartbeat with `cat logs/.last_run` and the data side with
`python -m scripts.audit_ledger --stale-days 2` (see [Auditing the ledger](diagnostics.md#auditing-the-ledger)).
To watch a run live on Linux: `tail -f logs/cron.log`, or run it inside `tmux new -s ledger` so it
survives a dropped SSH session (detach `Ctrl-B D`, reattach `tmux attach -t ledger`).

**Before trusting a scheduler on a new machine, run `python -m scripts.preflight`.** It is offline and
free, and catches the misconfigurations that keep working while doing the wrong thing (see
[Preflight](diagnostics.md#preflight)).

## Docker

The browser runs in Browser-Use Cloud, so the image is light (no Chromium; Playwright is only a CDP
*client*), and the container **schedules itself** with supercronic. Best for Linux servers; on a
single desktop the venv + Task Scheduler path is simpler.

`config.json` must be in the project directory before the first start (mounted at runtime, never
baked into the image). For Costco, have `.state.json` there too; without a token the first run mints
one through the profile's browser session (it needs an `auth.costco` block), and preflight says so.
On a brand-new host, `touch config.json .state.json` and let the [setup
wizard](#first-time-setup-setup) fill them.

> ⚠️ **Keep the `.state.json` mount writable — never add `:ro`.** Costco rotates its refresh token on
> every refresh and saves the new one back; read-only, Costco fails every run and the rotated token
> is lost.

**A bind mount whose host file is missing becomes an empty directory**, which reads as "not
configured" and cannot be written. The entrypoint and preflight both name it, with the fix:
`docker compose down`, `rmdir` the directory, `touch` the file, `up` again.

```bash
docker compose up -d --build      # build + start; runs every RUN_INTERVAL_HOURS
docker compose logs -f            # watch runs
docker compose down               # stop
```

The schedule is `config.json`'s `container` section (`run_interval_hours`, `run_on_start`,
`timezone`, `preflight_strict`); change it, then `docker compose restart`. **3h is the practical
floor**: every run spends one of MaxOutDeals' 10 daily received-items calls (see "Choosing an
interval" in [DEPLOY.md](../DEPLOY.md)). For several instances, copy the compose service with a
different `config.json` mounted per instance. The image builds for the host's architecture (amd64 or
arm64) and smoke-tests its scheduler binary at build time.

**What runs on its own:**

- **Preflight, on every container start.** It **alerts and continues**, because a container that
  refuses to start also stops scraping; set `preflight_strict` to `true` to fail fast. By hand:
  ```bash
  docker compose run --rm --entrypoint python ledger -m scripts.preflight
  ```
- **A heartbeat healthcheck.** "Up" in `docker ps` proves the scheduler is alive, not that it ran.
  Every completed run stamps `logs/.last_run`; once that is older than two intervals the container
  reports `(unhealthy)` and **sends an alert** (once per outage, with an all-clear on recovery).
- **The dashboard**, beside the scheduler ([below](#in-the-container)).

Container logs are capped at 10 MB × 5.

## Moving to another machine

Profiles and proxies are cloud-side; the ledger is `data/ledger.sqlite3`, beside the receipts under
`data/`. The move:

1. `python -m scripts.backup` on the old machine, copy the zip across.
2. `python -m scripts.backup --restore <zip>` on the new one. It restores `config.json`,
   `.state.json`, `.env` and `data/`, and is standard library only, so the system Python can run it
   before the venv exists.
3. Recreate the venv (`python -m venv .venv && …/pip install -r requirements.txt`) or build the
   image, re-install the scheduler, and run `python -m scripts.preflight`.

Or copy the project **except** `.venv/`, `__pycache__/` and `logs/`, making sure `data/`,
`config.json` and (for Costco) `.state.json` come along. `config.json` holds every credential in
plaintext: move it securely. No re-login is needed.

**Two things do not travel with the files:**

- ⚠️ **MaxOutDeals allowlists by IP.** A new egress IP makes tracking pushes fail however valid the
  token. Add the new host under the firewall tab in your MOD profile;
  `curl -s https://api.ipify.org` shows the IP. BFMR has no allowlist; Costco egresses through the
  profile's own static ISP proxy.
- **Stop the old scheduler before starting the new one.** The run lock is a file in `logs/`, so it
  cannot stop two hosts running at once — both would submit tracking and file insurance against the
  same accounts, into two ledgers that then disagree.

## The web dashboard

The ledger's UI, in `web/`: FastAPI + Jinja2 + htmx, no build step, a light/dark toggle in the header
(remembered per browser; follows the system until you choose). Its dependencies are
`requirements-web.txt`, an optional install on a desktop and part of the one Docker image.

| Page | What it is |
|---|---|
| Overview (`/`) | Needs-attention cards, a Profit & Loss statement, a 12-month chart and four breakdown charts |
| Orders (`/orders`) | The ledger as an editable grid (or cards), one page per order at `/orders/<id>` |
| Activity (`/activity`) | What every run and every dashboard change did; alerts and failure dossiers |
| Audit (`/audit`) | The Orders view over the rows the ledger audit flags |
| Reconciliation (`/recon`) | The Orders view over orders paid more or less than committed |
| Taxes (`/taxes`) | The year on Schedule C, with expenses and income the ledger cannot know |
| Tools (`/tools`) | The useful scripts run from the browser, the importer, *Log a Profile In* |
| Settings (the gear) | `config.json` edited in place, backup and restore, the setup wizard |
| `/health` | JSON for the container healthcheck; not linked |

The header's heartbeat pill turns stale after `web.heartbeat_stale_hours` (0 = twice the run
interval); its tooltip says so. The nav's Activity, Audit and Recon links wear badges: unacknowledged
alerts and dossiers of the last week, the rows a failing audit check flags, the orders paid short or
over. Every tooltip, dropdown, calendar and confirmation is the page's own, not the browser's.

### Running it

On a desktop, nothing to configure:

```bash
.venv/Scripts/pip install -r requirements-web.txt      # once (Linux: .venv/bin/pip)
python -m web                                          # http://127.0.0.1:8765/ over data/ledger.sqlite3
python -m web --source snapshot                        # the newest data/ledger_backup_*.csv, view-only
python -m web --snapshot data/ledger_backup_20260910T105451Z.csv
```

Flags win over `config.json` and the environment. It binds to loopback unless `--host` (or
`web.bind_host` / `WEB_BIND_HOST`) says otherwise. `?refresh=1` on any page re-reads the ledger
before the cache expires.

**Two backends** (`web/ledger_reader.py`), chosen by `web.ledger_source` / `WEB_LEDGER_SOURCE`
(every `WEB_*` setting is in [configuration.md](configuration.md)):

| Backend | Reads | For |
|---|---|---|
| `db` (default) | `data/ledger.sqlite3`, re-read every `web.ledger_cache_ttl_seconds` (300 s) | the host, and any desktop that has the file |
| `snapshot` | the newest `data/ledger_backup_*.csv` (every `--apply` script writes one), or `web.snapshot_path` | offline work and tests; view-only |

Cells are read **by column name**, so a backup written before a column moved still reads; `/health`
reports `schema_matches: false` (with the missing and extra columns) when a source's header is not
the current order.

### In the container

`docker/entrypoint.sh` starts the dashboard beside the scheduler when `web.enabled` is true (the
default) and restarts it if it exits; `docker/healthcheck.sh` probes it, so a dead dashboard reports
`unhealthy` with a reason that names the dashboard. It is published on the host's **loopback** by
default:

```bash
docker compose up -d --build                  # the usual command; the dashboard comes with it
echo "WEB_PUBLISH_HOST=0.0.0.0" >> .env        # or the LAN / Tailscale IP to publish on
docker compose up -d                          # re-create so the new port binding applies
curl -s http://127.0.0.1:8765/health          # on the host: "ok": true, "backend": "db"
```

Publishing wider than loopback with no password set triggers the [first-password
page](#security). `web.enabled: false` (or `WEB_ENABLED=false`) makes the container a pure scheduler.

### Security

Dashboard access is access to every credential in `config.json` — a backup download holds them all.

- **Never port-forward the dashboard to the internet.** Reach it over your LAN, Tailscale or
  WireGuard.
- **Sign-in.** Set `web.password` (Settings → Dashboard → Sign-in) and every page except the login
  page, the static assets and `/health` asks for it. Blank means no sign-in. *Remember me* ticked
  keeps the sign-in for `web.remember_days` (730); unticked, `web.session_hours` (6).
  `web.login_attempts` (5) wrong passwords in a row from one address lock it out for
  `web.login_lockout_minutes` (15; 0 = never), and a lockout is recorded as an alert. The cookie is
  signed with `web.session_secret`, made on first use and kept in `.state.json`: a restart keeps
  everyone signed in, changing the password signs everyone out. *Sign out* is in the header. These
  settings are read at dashboard start: Save, then *Restart dashboard*.
- **First password.** The dashboard knows where it is reachable: in Docker, the compose file's
  `WEB_PUBLISH_HOST` (default `127.0.0.1`); run by hand, the `--host` it binds. Reachable beyond
  loopback with `web.password` blank, every page redirects to `/first-password`. That page sets the
  password and wants the one-time **setup token** the dashboard prints to its log on start
  (`docker compose logs ledger`, or the terminal running `python -m web`), so only someone who can
  read the host's logs can claim it. Setting it signs that browser in and restarts the dashboard.
- **Host check** (`web/guard.py`). A request is answered only under an IP address, `localhost`, the
  host of `web.public_url`, or a name in **`web.allowed_hosts`** / `WEB_ALLOWED_HOSTS` (Settings →
  Advanced; comma-separated, `.example.com` = any subdomain; read at dashboard start). Anything else
  gets a 400, which stops DNS rebinding. Reach it as `ledger.local` or by a Tailscale name? List that
  name. `/health` is exempt.
- **Cross-site writes refused.** A form post or save the browser marks as coming from another site
  (`Sec-Fetch-Site`, or `Origin` ≠ `Host` when that is absent) gets a 403, so another website cannot
  submit a form to your dashboard. Behind a reverse proxy that rewrites `Host`, put the public name in
  `web.public_url` or `web.allowed_hosts`.
- **Receipts** are served at `/receipts/` only as pdf, png, jpg, jpeg or webp.

### What writes, and what doesn't

Every *read* goes through `web/ledger_reader.py`, which has no write path, so nothing that merely
displays the ledger can write it. The dashboard never calls a retailer or buying-group API on its
own, never scrapes on its own (Tools → *Run once* is you pressing the button) and never changes the
ledger's columns. **The ledger writes are the ones you make by hand** — a cell edited, a row added,
rows deleted, an import — through `web/ledger_writer.py` (`LedgerCellWriter`) and nothing else:
`tests/test_web.py` scans every other file in `web/` for the worksheet's write methods. The other
`POST`s (Settings, backups, receipts, tax inputs) write local files only.

**While a run is in progress every ledger write is refused** (the page says so). The sync caches row
numbers from its pre-sync snapshot, so a row deleted or appended underneath it would misplace its
updates. The signal is the run lock (`logs/.run.lock`, stale after three hours, as in `main.py`).

### The Orders page

The table is the ledger: every column in `HEADER` order, rows coloured by status (ordered red,
shipped orange, delivered yellow, paid green, return terracotta, cancelled grey, superseded dark grey
struck through), newest first. *Export CSV* downloads the rows in view.

**It edits like a spreadsheet.** The **hint** pill (a light bulb) on the count line has the full list;
right-click any cell, header or row number for every action with its key.

| Action | How |
|---|---|
| Select | click a cell; Ctrl-click adds or removes one; shift-click or drag for a range; click a header for its column; Ctrl+A for every row |
| Sort | the arrow at a header's right: ascending, descending, clear |
| Edit | double-click, Enter or start typing; Enter saves (and fills a selected range); Esc cancels; Delete clears |
| Copy / paste | Ctrl+C copies tab-separated; Ctrl+V pastes one value into every selected cell, or a block from the top-left |
| Undo | Ctrl+Z / Ctrl+Y (or Ctrl+Shift+Z), for this page load's cell writes |
| Today | Ctrl+; in a date cell or field |

Date cells open a calendar. Status, Retailer, Buying Group, Card Name, Card Last 4 and Profile offer
the column's previous answers, most used first; typing narrows them, and Card Name and Card Last 4
narrow each other. Tracking Submitted is a checkbox. A link cell selects on click and opens on
Ctrl-click, middle-click or double-click; its ✎ pencil opens the editor.

**Each write is one key-located, conflict-checked cell write**: the row is found by its key on a
fresh read, the cell must still hold what the page showed (or the edit is refused as a conflict), and
the value goes through the upsert's own coercion. Not editable: the key columns (Order ID, Order Date,
Item Name, Shipment), the computed COGS and Total Profit, and Last Scraped At. Dates are `YYYY-MM-DD`.

**A cell you edit is protected** while the count line's *keep my edits from runs* switch is on (the
default on Orders; off on Audit and Recon, where an edit usually fixes a finding the run should then
own). The run's upsert, its order-level reproration and the buying-group sync all keep a protected
cell (`ledger_db/hand_edits`); it shows a coloured left edge. Ctrl+Shift+H toggles the mark on the
selection. Clearing a protected cell puts back what the run had written before and releases it. Tools
→ Ledger Fixes → *Hand-edited cells* lists and releases them. Repair scripts are not gated.

**Rows.** Click a row number to select the row; Ctrl-click, shift-click or drag down the numbers for
more; the `#` header selects every row shown. Delete or Backspace removes the selected rows after one
confirmation (all-or-nothing). *Add a row* takes the key columns plus the common ones; Total Cost is
Quantity × Cost Per Item, and an existing key is refused.

**Filters.** Retailer, Profile, Status, Buying Group and Card are multi-select dropdowns; *Placed in*
/ *Paid in* take a month; *Payout* is any / open / projected / settled / unpaid. Filters travel in the
URL. Orders, Audit and Recon each remember their own view, page size and filters; each Reset clears
only its own. *View* switches to **cards**: one card per order, with its items, totals and, behind
*edit rows*, its editable rows; a card's *Delete* removes the whole order.

**Narrow windows and touch.** The header's tabs fold into a pages dropdown when they do not fit, and
under 760px forms stack and the Expenses and Activity tables stack each row's cells (the Orders grid
keeps its sheet). By touch: tap selects, a second tap edits, a long press opens the menu (with Select
all and a Paste box), the corner handle extends a range.

**Receipts by hand.** The Receipt Link cell's ⤒ button, the add form and the order page's *Upload
receipt* take a pdf, png, jpg, jpeg or webp up to 25 MB, stored under `receipts.dir` with the
capture's key (`<retailer>/<YYYY-MM>/<order id>.<ext>`); the `/receipts/...` link lands on every row
of the order. Receipt capture must be on. Deleting an order's rows deletes its receipt once nothing
links to it.

**The order page** (`/orders/<id>`) shows the order's facts, its money and, per shipment, its rows in
the same editable cells.

### The overview

- **Needs-attention cards**, only when something needs a hand: unacknowledged alerts and failure
  dossiers from the last seven days, spend limits near or reached, failing and warning audit checks,
  short- and over-paid orders, each linking to where it is dealt with. *Acknowledge all* drops a
  card and the nav badge until something newer arrives, and is recorded on Activity.
- **Profit & Loss statement**: Lifetime, the year and a month (arrows step; `/?month=YYYY-MM`) as
  columns; Net profit (realized profit + other income − expenses), Realized profit, Other income,
  Expenses, Projected profit, Weighted cashback rate, Floating, Paid out, Spend, Rows / orders and
  Open rows as rows. Income and expenses come from the Taxes page. Every figure links to the rows it
  counts; hover for its definition.
- **Charts**: 12 months of realized profit, other income and expenses (click a month to select it),
  and rows by status, retailer and buying group, plus the open rows. Every slice is a filter link.

**Open** rows are `ordered`, `shipped` or `delivered` (never a gift card). **Floating** is the Total
Cost of rows with no settled payout. The **Weighted cashback rate** is (Payout − COGS − Insurance) /
Total Cost over settled rows. A month counts rows by **Order Date**; for what was paid in a month use
the *Paid in* filter or the Taxes page.

**Projected versus realized.** A payout is **settled** when its Payout Date is set or the status is
`paid` / `return` (a `$0.00` settlement included); a non-zero amount with neither is **committed**.
Projected profit sums committed rows over their Expected Payout, realized profit the settled ones; a
committed cell is tagged `proj.`, and an open row's delivery date `est.` (the retailer's estimate).
See the [data model](data-model.md).

### The Activity page

The app's own account of what it did, newest first, from `logs/activity.jsonl`
(`diagnostics/activity.py`), recorded at the source rather than reconstructed from `run.log`:

- per run: what each retailer's scrape found, what the ledger write updated or added (split boxes,
  ignored tracking numbers, key conflicts), what the buying-group sync submitted, insured and read
  back, the emails the BFMR auto-reply sent;
- every alert, with its message, and every failure dossier — a dossier row opens its report in place
  and downloads the whole dossier as a zip (`/failures` redirects here);
- every dashboard change: cell edits (before and after), rows added or deleted, receipts, backups,
  settings saved (paths only, never a value), tool runs, sign-in lockouts.

Filter by *Type* (hidden types remembered per browser), *Last* (24 hours to everything; 7 days by
default), text, or one run (click its stamp). Each unacknowledged alert or dossier has its own
*Acknowledge* button; the overview's link opens Activity on the unacknowledged ones only. *Export CSV*
at the right.

### The Audit and Reconciliation pages

Both are the **Orders view** — the same filter bar, table or cards, sort, search and cell editing —
over a subset of rows, with a **Finding** column (or block) beside each. Neither writes anything on
its own; an edit there is an ordinary cell write. Both have *Export CSV*.

- **Audit** (`/audit`, `web/audit_view.py`): every check of `scripts.audit_ledger`, run against the
  ledger the dashboard serves through the CLI's own read-only path, then every row a check named,
  mapped to its order. The lead shows Fail / Warning / Pass / Flagged tiles (each filters the rows)
  and the checks with how many rows each flagged; the **Check** dropdown narrows the rows. A detail
  line that names no row stays under its check. The report is rebuilt whenever the rows change.
- **Reconciliation** (`/recon`, `web/recon_view.py`): every order the buying group paid more or less
  than it committed — Actual Payout against Expected Payout, compared as order totals over the same
  settled rows (two cents plus a cent per row of tolerance), the biggest gap first, with short-paid /
  over-paid / net totals in the lead. A row with no commitment is not compared. Fix a figure by
  editing the cell; take a real shortfall up with the group.

### The Taxes page

`/taxes?year=YYYY` (`web/tax_inputs.py`) lays the year out on Schedule C's lines from the cash-basis
report `scripts/tax_report` computes (Part I lines 1, 4, 6, 7; Part II lines 15, 27a, 28, 31; Part
III lines 36 and 42, card cashback netted from cost on its own row), and holds what the ledger cannot
know:

| Panel | Holds | Line |
|---|---|---|
| Expenses | Business spending beyond the ledger's purchases, card annual fees included. Each needs a date in the year, amount, who paid (profile or account email) and a receipt (a file kept under `data/expenses/`, or a link). A grid like Orders; the receipt cell's ⤒ uploads a replacement; deleting rows deletes their files | 27a |
| Program Cashback | Prime, Prime Business and Costco Executive cashback, per retailer login | 6 |
| Cards | The sign-up bonus per card on the year's orders (virtual cards left out) | 6 |
| Cashback Sites | Portal payouts, the usual portals plus any you add | 6 |
| Other Income | Any other income, and notes for the preparer | 6 |

Cashback and bonuses are dated logs (entries behind a total), each counted in its own month on the
overview. *Open year* opens any year; *Close year* deletes a year's inputs and receipts; *Download
year* is one zip of the Schedule C lines, the orders, the expenses with receipts, the income entries,
the notes and the order receipts. Inputs live in `data/tax_inputs.json`. A summary for a preparer, not
tax advice; nothing here writes the ledger.

### The Tools page

Runs the scripts worth running from a browser, exactly as on the command line (`python -m
scripts.<name> <args>`, the arguments its `--help` declares), output streaming into the page, each
run an Activity event. The header's Tools menu and the page's *All Tools* directory group them:

| Group | Tools |
|---|---|
| Import | *Run importer* ([below](#importing-order-history-tools--import--run-importer)) |
| Run | **Run once** (`python -m main [retailer]`): a real run — scrape, upsert, and the buying-group sync that submits tracking and files insurance. Confirmed in-page, refused while a run holds the lock, and stamps the heartbeat when it ends |
| Accounts | *Log a Profile In*, the Costco refresh token |
| Checks | Preflight, the buying-group probe, the receipt audit, the receipt files check |
| Ledger Fixes | The backfills (tracking numbers, receipts, gift cards, Amazon promo cashback, Promo Rate), retag buying groups, fix a superseded shipment, sort the ledger, hand-edited cells |
| Config | Standardize retailer names |

A tool that writes asks first, runs dry by default where the script can, and is refused during a
run; one that spends (a cloud browser session, a buying-group call) says so. A secret field (the
Costco refresh token) is a password box, masked (`••••••`) in the activity log, the job log and the
job page. Recon probes and one-off migrations stay on the command line.

**Log a Profile In** embeds a live Browser-Use session on the profile, through its proxy (with an
*open in a new tab* link for a browser that refuses the frame). Log into the retailers, then *Close &
save*: closing the session saves the cookies. An abandoned session closes after
`web.tool_session_minutes` (60).

### Importing order history (Tools → Import → Run importer)

Upload a CSV of past orders with any columns, or fill in the template (the ledger's own header). The
map screen pairs each column with a ledger column (pre-filled from the header names), asks which way
round slash dates are, and takes a profile for rows that name none. The preview says what will happen
to every row; nothing is written until you run it. Duplicate column names and oversize cells are
refused at upload. A cell with several tracking numbers becomes one row per box; if Quantity cannot
cover the boxes, the per-box money cells are left blank for you.

**What lands**: a row that carries everything the Audit page's `mandatory_by_stage` check asks of its
status (the same function decides both). It goes through the Orders page's add-row path: refused
during a run, skipped when its key is already on the ledger, every cell kept as a hand edit.

**What waits** goes to the **staging sheet**, an Orders-style grid: rows with a gap (missing cells
outlined), open rows (*ordered* / *shipped* — the scrapers own those), and near-duplicates of an
order or tracking number the ledger holds. *Import anyway* lifts a near-duplicate hold (*undo* puts
it back); a row of an order this import landed is never held. Fix cells in place, then *Import the
complete rows*. A key cannot be edited onto one the ledger or the sheet already holds. The sheet lives
in `data/imports/<stamp>/`, so backups carry it; the Tools menu counts its rows. One import at a time;
*Download the staging CSV* gives the waiting rows to fix in a spreadsheet.

`python -m scripts.import_history` remains for the command line; it is stricter (it refuses open rows
and reconciles a profit column before writing).

### First-time setup (/setup)

A fresh install — no `config.json` (or an empty one), or no profile, and no finished setup on record —
lands on the wizard from any page (sign-in, restore, `/settings`, `/health` and the static assets stay
reachable). Its ten steps use the Settings page's own fields, each saving only its own: restore a
backup (a restored `config.json` ends the wizard), the dashboard password (signs the browser in),
the Browser-Use key, profiles, buying groups and their API keys, cards, alerts, schedule and backups,
the importer, and Done, which lists what is set and offers any restart needed.

Done needs a profile. A step's chip ticks when the step has a value; a refused save keeps what you
typed; a duplicate profile, buying group or card is refused. The record is `setup` in `.state.json`;
an install that already has profiles is stamped complete and never interrupted. *Run the setup
wizard again* on Settings walks the same steps with a *Keep* on each.

### The Settings page

`/settings` edits `config.json` in place, one panel per section, each field with its config key, any
restart or *env override* tag, and the example file's comment as help. Secrets are password fields
that show only whether a value is set (blank keeps it, *clear* blanks it). A key the file omits shows
the code's default, tagged *default*. `//` comment keys survive every save. A setting exported in the
environment still wins over the file.

- **Alerts**: the spend-limit warnings, then Discord and Gmail, each with its own switch.
- **Buying groups**: the sync switch, then BFMR and MOD as dropdowns with their own enable switches
  (`buying_groups.bfmr.enabled` / `buying_groups.mod.enabled`, default on; the sync skips a group
  switched off). The BFMR combined-package auto-reply has its own Gmail account, separate from alerts.
- **Dashboard**: the sign-in settings ([Security](#security)).
- **Backups**: the schedule, and the **Backup & Restore** panel ([below](#backup-and-restore)).
- **Advanced**, folded at the foot behind a warning: the ledger path, the dashboard's ledger source,
  snapshot path and cache TTL, `web.enabled`, bind address and port, `web.allowed_hosts`, the receipts
  directory, strict preflight and the buying groups' API hosts. The receipts folder (`receipts.dir`)
  and the ledger file (`database.path`) can move only within `data/` from the page; a path set by
  hand in `config.json` stays.

**Profiles, warehouses and cards are entry cards**, each with *Save* (validated by the section's
model; keys the form does not show survive), *✕ Delete* and *+ Add*; *Edit … as JSON* shows a whole
list as text. Passwords and TOTP seeds are never rendered.

- **Profile**: label, Browser-Use id, retailers, proxy (with an on/off switch that keeps the details),
  unattended sign-ins.
- **Warehouse**: buying group and address jigs. BFMR, MOD and Personal are built in: always shown,
  name fixed, no Delete.
- **Card**: name, last 4, Card Type (Regular / Virtual / Employee), profile scope, and a Rates table
  (retailers sharing a rate, optionally with a spend limit and a Left This Period bar). *Archive*
  keeps a retired card for its old orders but folds it away, stops offering it to a virtual number
  and stops its spend-limit warnings; *Restore* brings it back.

**When a change takes effect.** The bar at the bottom has *Save settings*, *Restart dashboard* and
*Restart container*, and says which restart your edits need:

| Setting | Applies |
|---|---|
| most settings | at the next run (each run reads the file on start) |
| `web.*`, `database.*` | after *Restart dashboard* (on a desktop, run `python -m web` again) |
| `container.*`, `web.enabled`, `backups.*` | after *Restart container* (refused while a run is in progress), or `docker compose restart` |

In Docker the `config.json` mount is writable for this page alone; no scheduled run writes it.

**The page is derived** from `ENV_TO_CONFIG`, the `Settings` fields and `config.example.json`'s
`"// key"` comments (`web/settings_form.py`), so a scalar setting added the documented way appears by
itself, and `tests/test_web_settings.py` fails if one is missing. A new structured section, special
widget or changed section model goes in `SECTIONS` by hand; a new field on `ProfileConfig`,
`Warehouse` or `Card` is kept as stored but is not editable until the card's form shows it.

## The ledger file (`ledger_db/`)

`data/ledger.sqlite3` (`database.path` / `LEDGER_DB_PATH`) **is** the ledger: one `ledger_rows`
table whose columns are `FIELDNAMES` in order, keyed on the upsert key, plus the `hand_edits` table of
cells the dashboard protected. Every writer (the scrapers' upsert, the sort, the buying-group sync,
the BFMR auto-reply, the dashboard's editor, the repair scripts) and every reader goes through
`ledger_db/worksheet.py:DbWorksheet`, a worksheet-faced adapter (`get_all_values` / `get_values` /
`update` / `batch_update` / `sort` / `delete_rows`) that `ledger.sync._get_worksheet()` hands out. The
audit and the tax report open it through `scripts.audit_ledger.open_ledger_readonly`, a read-only
handle that refuses every write.

COGS and Total Profit are never stored: they are computed from the row on every read
(`web.ledger_reader.cogs_of` / `profit_of`, pinned against `ledger.sync._cogs_formula` by a test),
and a write into them is ignored. A row with no Order ID is never stored. The file **migrates its own
table on open**: when `FIELDNAMES` changes, `ledger_db/store.py` rebuilds `ledger_rows` carrying every
value across by column *name*, and refuses loudly (restore a backup or migrate by hand) if a
populated table holds a column the schema no longer knows.

## Backup and restore

One zip of everything a `git clone` does not give you: `config.json`, `.state.json`, `.env` and the
whole `data/` directory (ledger, receipts, expenses, tax inputs, import sheets, CSV safety copies),
with a manifest naming the commit. Logs and dossiers are left out. **The archive holds every live
credential**: keep it private (`backups/` is gitignored and never enters an image).

```bash
python -m scripts.backup                       # -> backups/ledger_backup_<UTC stamp>.zip
python -m scripts.backup --list
python -m scripts.backup --restore backups/ledger_backup_20260917T120000Z.zip          # keeps existing files
python -m scripts.backup --restore backups/ledger_backup_20260917T120000Z.zip --force  # overwrites them
```

The ledger is copied through SQLite's backup API, so a backup taken mid-run is still consistent. The
script is **standard library only**: on a new machine, `git clone`, then `python -m scripts.backup
--restore <zip>` with the system Python, then the normal setup.

**Scheduled backups.** The container's cron follows `config.json`'s `backups` section: `enabled`,
`frequency` (`daily` / `weekly` / `monthly`), `time` (HH:MM in `container.timezone`), `days` (weekly
`mon,thu`, monthly `1,15`; blank = Sunday / the 1st) and `keep` (older zips beyond this are deleted
after every backup; `0` keeps all). Edit them under Settings → *Backups*; they apply at the next
container start. Each backup is recorded on Activity, and a failure **alerts**. On a native install,
add this line to your crontab:
`$(python -m scripts.backup --print-cron) cd /path/to/repo && .venv/bin/python -m scripts.backup --scheduled`.
The zips stay on the same machine: copy `backups/` elsewhere (rsync over WireGuard, a second disk,
object storage).

**From the dashboard**, Settings → **Backup & Restore** (`/backup` redirects there): *Create a backup
now* (into `backups/`, a bind mount that survives a rebuild; each zip downloadable and deletable) and
*Restore*, an upload that keeps existing files unless *overwrite* is ticked. A restored `config.json`
prompts for a container restart. Restoring from the page is only as safe as the dashboard's access,
so set a password ([Security](#security)).

