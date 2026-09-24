# Getting Started

_Part of the [Buying Group Ledger](../README.md) docs._

A first install, step by step. Pick **Docker** (path A, the easy way, and what a small always-on host
like a Raspberry Pi runs) or a **native venv** (path B, for a desktop or development). Both end with
the same safe first run.

## What You Need

| Thing | Why |
|---|---|
| A **Browser-Use Cloud** API key, on a **paid tier** | Retailer sign-ins and page reads run in its cloud browser; custom proxies need a paid plan. No browser runs on your machine. |
| **One static ISP proxy per profile** | Each profile is one browser identity behind its own proxy. Amazon and Amazon Business must be separate profiles. |
| Your **retailer accounts** (Amazon, Amazon Business, Best Buy, Costco) | With 2-step verification on through an **authenticator app**, so the app's seed lets sign-in run unattended ([Profiles and Sign-In](profiles-and-auth.md)). |
| Optional: **buying-group API access** | A BFMR API key and secret, and/or a MaxOutDeals token (MaxOutDeals allowlists your server's IP address). |
| Optional: **alerts** | A Gmail address with an app password, and/or a Discord webhook. |
| **Python 3.12+** or **Docker** | Docker needs Compose v2.24+. |

It is **US-only**: amazon.com, bestbuy.com, costco.com, and a Schedule C tax report.

## Path A: Docker

```bash
git clone https://github.com/ango-dev/BuyingGroupLedger.git
cd BuyingGroupLedger
touch config.json .state.json
chmod 600 config.json .state.json
docker compose up -d --build
```

Create both files **before** the first `up`. A bind mount whose host file is missing becomes an empty
directory inside the container, which nothing can write, so the wizard could not save; preflight and
the container's start-up log name that case with its fix. Empty files read as a fresh install.

Open **http://127.0.0.1:8765**. A fresh install lands on the setup wizard.

### The Setup Wizard

Each step saves into `config.json` through the Settings page's own fields:

1. **Restore a backup** (optional): upload a backup zip to bring an existing install across; a
   restored `config.json` ends the wizard.
2. **Dashboard password**: set one. The browser is signed in at once.
3. **Browser-Use key**.
4. **Profiles**: a label (such as `profile-1`), its proxy and its retailers. At least one profile
   is required to finish.
5. **Buying groups**: the warehouses (the delivery addresses and jigs that decide an order's Buying
   Group) and the BFMR / MaxOutDeals keys.
6. **Cards**: last 4 digits, name and cashback rates.
7. **Alerts**: Discord and/or Gmail.
8. **Schedule and backups**: hours between runs, timezone, and when backups are made and how many
   are kept.
9. **Import history** (optional): a pointer to Tools → Import for orders you already have
   ([Importing History](importing-history.md)).
10. **Done**: lists what is set and offers any restart a changed setting needs.

Then log each profile in to its retailers: **Tools → Accounts → Log a Profile In** opens the
profile's live cloud browser in the page. Sign in, then close the session so the profile saves its
cookies. *Run the setup wizard again* on the Settings page walks the same steps with a Keep on each.

### Reaching the Dashboard From Another Machine

The compose file publishes the dashboard on `127.0.0.1` only. To reach it from your LAN or a VPN,
put `WEB_PUBLISH_HOST=0.0.0.0` (or the interface's IP) in `.env` and run `docker compose up -d`.

If the dashboard is reachable beyond loopback and its **Password** (`web.password`) is still blank, every page
sends you to a page that sets one, and that page asks for a one-time **setup token** printed to the
dashboard's log:

```bash
docker compose logs ledger | grep "Setup token"
```

Requests under a host name the dashboard does not know are refused. An IP address, `localhost` and
the host of **Public URL** (`web.public_url`) always work; add any other name, such as
`ledger.local`, to **Allowed hosts** (`web.allowed_hosts`, Settings → Advanced). **Never
port-forward the dashboard to the internet**; see [SECURITY.md](../SECURITY.md).

[DEPLOY.md](../DEPLOY.md) is the full runbook for a dedicated host.

## Path B: Native Venv

```bash
git clone https://github.com/ango-dev/BuyingGroupLedger.git
cd BuyingGroupLedger
python -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-web.txt -r requirements-dev.txt
```

On Windows use `.venv\Scripts\pip` and `.venv\Scripts\python`; the examples below use the Linux
spelling.

Then either configure by hand:

```bash
cp config.example.json config.json     # every key has a "// key" comment beside it
chmod 600 config.json
```

or start the dashboard and use the same wizard as path A:

```bash
.venv/bin/python -m web                # http://127.0.0.1:8765
```

`python -m web --host 0.0.0.0` binds it wider; with no password set it prints the setup token to
that terminal. Log a profile in from the command line (the profile's `proxy` must already be in
`config.json`; `profile_id` is filled in for you):

```bash
.venv/bin/python -m scripts.create_profile --label profile-1
```

It prints a live browser URL; sign in to the retailers there, press Enter, and close the browser
window. Details, and unattended re-sign-in with an `auth` block, are in
[Profiles and Sign-In](profiles-and-auth.md).

## Costco: The Refresh Token

Costco's path reads its API with a stored refresh token rather than a browser. Save one once, on the
profile that owns the membership: **Tools → Accounts → Costco refresh token** on the dashboard, or

```bash
.venv/bin/python -m scripts.costco_token --label profile-1 --grab
```

The token lands in `.state.json`, which must stay writable because Costco rotates it on every use.
See [Retailers → Costco](retailers.md#costco).

## A Safe First Run

**1. Check the setup offline.** Free, no network:

```bash
.venv/bin/python -m scripts.preflight
# Docker: docker compose run --rm --entrypoint python ledger -m scripts.preflight
```

Fix anything it fails.

**2. Scrape narrowly.** One retailer, today and yesterday only:

```bash
LOOKBACK_DAYS=1 .venv/bin/python main.py costco
# Docker: docker compose run --rm -e LOOKBACK_DAYS=1 --entrypoint python ledger main.py costco
```

`main.py` takes `amazon`, `amazon-business`, `bestbuy` or `costco`, or nothing for all of them. On
Windows PowerShell set the variable first: `$env:LOOKBACK_DAYS = "1"`. A failure records nothing
and writes a failure dossier under `logs/failures/`; read its `report.md` first
([Diagnostics](diagnostics.md)).

**3. Leave the buying-group sync off until you have dry-run it.** It is off by default
(**Sync enabled** under Settings → Buying groups, `buying_groups.sync_enabled`). When on, every run **submits tracking numbers and files real BFMR
insurance, unattended**, and a submission cannot be taken back. Before turning it on:

```bash
.venv/bin/python -m sync_tracking      # dry run: prints what it would send, sends nothing
```

Then a one-package live test (`python -m sync_tracking --apply --limit 1`) before the switch. BFMR
and MaxOutDeals each have their own switch under it. See [Buying Groups](buying-groups.md), including
the BFMR order numbers you still enter by hand.

## Checking the Results

- **Orders** on the dashboard shows the rows the run wrote; cells can be edited there, and an edited
  cell is protected from later runs.
- **Audit** shows every failing or warning check by row. The same checks run from the command line,
  read-only:

  ```bash
  .venv/bin/python -m scripts.audit_ledger
  ```

- **Activity** lists what each run did: rows written, packages submitted, alerts, dossiers.

## Scheduling

- **Docker**: the container schedules itself. Set the interval and timezone on the wizard's
  *Schedule and backups* step or Settings; the healthcheck and `logs/.last_run` show that it is
  still running.
- **Linux**: `./scripts/install_cron.sh` (every 6 hours; `./scripts/install_cron.sh 4` for 4).
- **Windows**: `scripts\install_task_windows.ps1 -Hours 6` from an elevated PowerShell.

See [Operations](operations.md).

## Backups

`config.json`, `.state.json`, `.env` and `data/` (the ledger and receipts) are what a backup holds:

```bash
.venv/bin/python -m scripts.backup                   # a zip into backups/
.venv/bin/python -m scripts.backup --list
.venv/bin/python -m scripts.backup --restore backups/<zip>
```

The container also makes them on the schedule you set, keeping the newest few; Settings → Backup &
Restore lists, downloads and restores them. A backup holds every credential, so keep `backups/`
private, and copy it off the host now and then.

## Where to Go Next

- [Architecture](architecture.md): the run loop and the cost model.
- [Configuration](configuration.md): every setting, warehouse jigs, card rates.
- [Data Model](data-model.md): the columns and the profit accounting.
- [Receipt Capture](receipts.md): proof-of-purchase PDFs.
- [Operations](operations.md): the dashboard's pages, scheduling, moving hosts.
- [SECURITY.md](../SECURITY.md): secrets and exposing the dashboard.
