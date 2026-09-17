# Operations — scheduling, Docker, moving hosts

_Part of the [Buying Group Ledger](../README.md) docs._

> **[DEPLOY.md](../DEPLOY.md) is the runbook for a dedicated Linux host** (Ubuntu VM, Raspberry Pi): container settings, choosing an interval, monitoring and cutover live there and are not repeated here. This page is the desktop path and the short version of the rest.

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
`logs/cron.log`. To watch a run live on Linux: `tail -f logs/cron.log`, or run it inside
`tmux new -s ledger` so it survives your SSH session dropping (detach with `Ctrl-B D`, reattach with
`tmux attach -t ledger`).

**Both runners behave identically**, so a host is diagnosable the same way whichever platform it's
on. They rotate `logs/cron.log` at 10 MB — left unbounded it fills a small disk months later, long
after anyone is watching — and always record how a run *ended*, including its exit code. They also
stamp `logs/.last_run`, the same heartbeat the container healthcheck uses: if that file is stale, the
scheduler has stopped firing, which is otherwise a completely silent failure. Check it with
`cat logs/.last_run`, and cross-check the data side with
`python -m scripts.audit_sheet --stale-days 2` (see [Auditing the sheet](diagnostics.md#auditing-the-sheet)).

**Before trusting either scheduler on a new machine, run `python -m scripts.preflight`** — it's
offline and free, and it catches the misconfigurations that keep working while doing the wrong thing
(see [Preflight](diagnostics.md#preflight)). For a dedicated Linux host — an Ubuntu VM or a Raspberry Pi — follow
**[DEPLOY.md](../DEPLOY.md)**.

## Docker

Because the browser runs in Browser-Use Cloud, the image is lightweight (no Chromium — the bundled
Playwright is only a CDP *client*). The container **self-schedules** via supercronic, so no host cron
is needed. Best for Linux servers, cloud, or running several isolated instances; for a single desktop
the venv + Task Scheduler path is simpler.

Prereqs on the host: `config.json` present in the project dir (mounted at runtime and excluded
from the image via `.dockerignore` — secrets are never baked in). If you use Costco, also have `.state.json` present so the container starts with a
token; without one, the first run has to mint it through the profile's browser session (which needs
an `auth.costco` block), and preflight says so. (Not using Costco? Drop the
`./.state.json` volume line from `docker-compose.yml`.)

> ⚠️ **The `.state.json` mount must stay writable — do not add `:ro` to it.** It looks like it should be
> read-only, since it holds nothing but secrets, but Costco **rotates its refresh token** on every
> refresh and the client saves the new one back. Mounted read-only that write raises
> `OSError: Read-only file system`, Costco fails on every run, and the
> rotated token is discarded — which can strand the stored one and force a manual re-grab. Preflight
> now probes the directory for writability rather than only checking the file exists.

Warehouses and cards used to be two more bind mounts; they are sections of `config.json` now, so
there is nothing extra to mount. That removes a real trap — **a bind mount whose host file is missing
makes Docker create an empty directory in its place**, which reads as "present but empty" rather than
as an error. Both sections are still optional, and the run works without them: every address tags
`Unclassified` and every card falls back to `DEFAULT_CASHBACK_RATE`.

```bash
docker compose up -d --build      # build + start; runs every RUN_INTERVAL_HOURS
docker compose logs -f            # watch runs
docker compose down               # stop
```

Adjust the schedule in `config.json` under `container` (`run_interval_hours`, `run_on_start`,
`timezone`, `preflight_strict`), then `docker compose restart` — the entrypoint re-reads the mounted
config on every start. **3h is the practical floor**: every run spends one of MaxOutDeals' 10 daily
received-items calls, so running more often makes payout write-back start failing (see "Choosing an interval" in [DEPLOY.md](../DEPLOY.md)). To run **multiple instances**, copy the compose service with a
different `config.json` mounted per instance (e.g. one per proxy pool).

The image builds for the host's own architecture (amd64 and arm64 both work — see
**[DEPLOY.md](../DEPLOY.md)** for the server runbook), and smoke-tests its scheduler binary during the
build so a wrong-architecture image fails loudly at build time rather than crash-looping later.

**Two things run automatically that are worth knowing about:**

- **Preflight, on every container start.** `scripts/preflight.py` checks the things that otherwise
  fail *silently* — a deterministic-path dependency whose absence would fail three retailers on every
  run without raising, a bind mount whose missing host file became an empty directory, a missing
  Costco token. It **alerts and continues** rather than aborting, because a container that refuses to
  start also stops scraping; set `PREFLIGHT_STRICT: "true"` to fail fast instead. Run it by hand any
  time — it's offline and free:
  ```bash
  docker compose run --rm --entrypoint python ledger -m scripts.preflight
  ```
- **A heartbeat + healthcheck.** `docker ps` reporting "Up 3 weeks" proves the scheduler process is
  alive, not that it ever ran anything — a wedged lock or a failing job leaves the container happily
  "Up" while the ledger goes stale. Every completed run stamps `logs/.last_run`, and the container
  reports `(unhealthy)` once that's older than two intervals — and **sends an alert** (email +
  Discord, once per outage, with an all-clear when a run completes again), so a dead scheduler no
  longer depends on someone looking at `docker ps`.

Container logs are capped (10 MB × 5) rather than left to Docker's unbounded default, which otherwise
fills a small disk months later.


## Moving to another machine

Everything except the local environment is cloud-side (profiles, sheet, proxies), so migration is just:
copy the project **except** `.venv/`, `__pycache__/`, `data/`, `logs/`; be sure to bring the two
gitignored files — `config.json` and, if you use Costco, `.state.json`. `config.json` holds every live
credential in plaintext, so move it securely; then recreate the venv (`python -m venv .venv && …/pip install -r requirements.txt`) and
re-install the scheduler on the new host. No re-login or re-sharing needed.

**Two things do not travel with the files:**

- ⚠️ **MaxOutDeals allowlists by IP.** A new machine has a new egress IP, so tracking pushes start
  failing however valid the token. Add the new host under the firewall tab in your MOD profile
  (`curl -s https://api.ipify.org` tells you what to add). BFMR has no allowlist, and Costco egresses
  through the profile's own static ISP proxy, so neither is affected.
- **Stop the old scheduler before starting the new one.** The overlap lock is a file in `logs/`, so it
  is per-machine and will not stop two hosts scraping the same sheet at once.

Then run `python -m scripts.preflight` on the new host before trusting it. Moving to a dedicated
Linux host has its own runbook: **[DEPLOY.md](../DEPLOY.md)**.

## The web dashboard (read-only)

A local web page over the ledger, in `web/`: an overview (open rows by status and buying group,
projected versus realized profit, the COGS input gaps, the scheduler heartbeat), a filterable and
sortable ledger table, one page per order, the failure dossiers with each `report.md` rendered, and
`/health` as JSON. FastAPI + Jinja2 + htmx, no build step; the dependencies are the optional extra
`requirements-web.txt`, which the scheduler image never installs.

**The read-only guarantee.** The dashboard cannot write the Sheet: its live backend opens the
worksheet through `scripts.audit_sheet.open_worksheet_readonly`, the `spreadsheets.readonly` scope,
so Google refuses a write before any code could attempt one. It never calls a retailer or a
buying-group API, never runs a scrape, and never changes the ledger schema (`FIELDNAMES` / `HEADER`
are frozen; `tests/test_schema.py` enforces them). Every route is a `GET`; every other method is
refused; `tests/test_web.py` pins all of it, including that no write method of the worksheet is ever
named in `web/`. In Docker, `config.json`, `data/` and `logs/` are mounted `:ro` as well.

**Two backends, one adapter** (`web/ledger_reader.py`), chosen in `config.json`'s `web` section or
by `WEB_LEDGER_SOURCE=snapshot|sheet` (the variable table in [configuration.md](configuration.md)
lists the five `WEB_*` settings):

| Backend | Reads | For |
|---|---|---|
| `snapshot` (default) | the newest `data/sheet_backup_*.csv` (every `--apply` script writes one), or `web.snapshot_path` | development, tests, a look at yesterday's ledger with no credentials |
| `sheet` | the live worksheet, read-only scope, cached in memory for `web.sheet_cache_ttl_seconds` (300 s) | the deployed dashboard |

Cells are read **by column name**, so a backup written before a column moved still reads correctly;
`/health` reports `schema_matches: false` (with the missing and extra columns) when a source's header
is not the current order. A CSV backup stores `COGS` and `Total Profit` as formula text, so for those
two columns the page computes the same arithmetic as the sheet formula (`web.ledger_reader.cogs_of`,
pinned against `sheets.ledger_sync._cogs_formula`'s own cell references by a test).

**Projected versus realized.** Since 2026-09-11 a Payout Amount with a blank Payout Date on an open
row is BFMR's *committed* price, not money received ([data model](data-model.md)). The dashboard
reads the `(Payout Date, Status)` pair exactly as the audit does: a payout is **settled** when its
date is set or the status is `paid` / `return` (MOD's paid rows carry no date), and **committed**
otherwise. Projected profit sums the committed rows, realized profit the settled ones, and a
committed cell is tagged `proj.` in every table. There is no `Expected Payout` column (parked in
`the design notes`); this is a view over the existing cell.

**Running it locally** (main PC, against a snapshot; nothing to configure):

```bash
.venv/Scripts/pip install -r requirements-web.txt      # once (Linux: .venv/bin/pip)
python -m web                                          # http://127.0.0.1:8765/ over the newest backup
python -m web --snapshot data/sheet_backup_20260910T105451Z.csv
python -m web --source sheet                           # the live Sheet, read-only
```

Flags win over `config.json` and the environment. It binds to loopback unless `--host` (or
`web.bind_host` / `WEB_BIND_HOST`) says otherwise — there is no authentication in phase 1, so anything
beyond localhost or Tailscale is a deliberate choice. `?refresh=1` on any page forces the sheet
backend to re-read before its cache expires (still a read).

**Running it on the host** (Docker; a separate image and a compose *profile*, so a plain
`docker compose up -d` starts exactly what it always did and the scheduler is untouched):

```bash
docker compose --profile web up -d --build   # builds web/Dockerfile, publishes 127.0.0.1:8765
docker compose --profile web logs -f web
docker compose --profile web down            # stops both; `docker compose stop web` stops just the dashboard
```

Set `web.ledger_source` to `sheet` in the host's `config.json` for the live ledger (the snapshot
backend only sees `data/` backups, which the scheduled run does not write). To reach it over
Tailscale, put `WEB_PUBLISH_HOST=<the host's Tailscale IP>` in `.env`; the default publishes on the
host's loopback only. `logs/` is the same volume the scheduler writes, so the heartbeat and the
failure dossiers on the page are the host's own.

**What it cannot do (phase 1, by design):** write anything, edit a row, submit tracking, file
insurance, run a scrape, or add the `Expected Payout` column. For those, the commands in
[CLAUDE.md](../CLAUDE.md)'s cost table remain the way.
