# Deploying to a Linux server

A runbook for moving the ledger off a desktop onto a host that runs it unattended. Written for an
**Ubuntu 24.04 LTS VM**; a Raspberry Pi on 64-bit Pi OS works the same way, with the differences
called out inline. The README explains how the system works; this explains how to keep it running
somewhere you aren't watching.

Two paths, and you can switch between them freely: both run off the same `config.json` and `data/`
directory (the ledger `data/ledger.sqlite3` and the receipts), and the browser profiles live in
Browser-Use Cloud.

| | **Docker** (recommended) | **venv + cron** |
|---|---|---|
| scheduling | supercronic, inside the container | host crontab |
| Python | pinned 3.12 in the image | the host's (3.12+; 24.04 ships 3.12) |
| upgrade | `git pull && docker compose up -d --build` | `git pull && .venv/bin/pip install -r requirements.txt` |
| health | `docker ps` shows healthy/unhealthy | `logs/.last_run` + your own eyes |
| best for | leaving it alone | debugging over SSH |

---

## What the host does, and how small it can be

**No browser ever runs on this machine.** Chromium runs in Browser-Use Cloud and `scrapers/cdp.py`
connects to it over the network (Playwright is only a CDP *client*); Costco is plain HTTPS. The host
does HTTP requests, HTML parsing and SQLite writes: **1 vCPU and 1–2 GB RAM is ample**, and a few GB
of disk for the image and logs.

It does need a **correct clock**: every date in the ledger comes from the host, and `Order Date` is
part of the upsert key, so a skewed clock writes duplicate rows. `timedatectl` should show NTP
synchronised.

> **On a Raspberry Pi, use the 64-bit OS** (`uname -m` → `aarch64`). `curl_cffi`, which Costco's TLS
> impersonation needs, ships prebuilt wheels for `aarch64` but not reliably for `armv7l`, where the
> source build usually fails.

---

## 0. Prerequisites

```bash
uname -m                 # x86_64 on a VM; aarch64 on a 64-bit Pi
docker --version         # 20.10+
docker compose version   # v2.24+ (the plugin, not the old docker-compose binary)
timedatectl              # confirm the timezone and that NTP is synced
```

**Compose v2.24 is the floor**, because `docker-compose.yml` marks `.env` as `required: false`;
older versions fail every command with "env file not found" when there is no `.env`. On an older
Compose, `touch .env` or delete the `env_file:` block.

Ubuntu 24.04 does not ship Docker. Install it:

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"     # then log out and back in, or `newgrp docker`
```

---

## 1. Get the code and the secrets across

The repo is safe to clone; everything that makes it *work* is gitignored and copied separately.

```bash
git clone https://github.com/ango-dev/BuyingGroupLedger.git ~/BuyingGroupLedger
cd ~/BuyingGroupLedger
```


From the machine that runs it today, copy these across over SSH (`scp` / `rsync`, never email or a
cloud drive) — they hold live credentials and the ledger itself:

| Path | Contains | Required? |
|---|---|---|
| `config.json` | everything: API keys, passwords, profiles, warehouse jigs, cards | **yes** |
| `.state.json` | Costco's rotating refresh token, the dashboard's session secret | if you use Costco |
| `data/` | the ledger (`ledger.sqlite3`), the receipts, the CSV safety copies | if you are moving an existing ledger — a fresh host starts an empty one |

The one-zip way is `python -m scripts.backup` on the old machine and `python -m scripts.backup
--restore <zip>` here: it carries all three (plus `.env`), runs with the system Python (standard
library only), and keeps existing files unless `--force`. By hand:

```powershell
# from the project dir on Windows
scp -r config.json .state.json data you@ledger-vm:~/BuyingGroupLedger/
```

`.env` is optional — purely an override layer; nothing is environment-only. Copy it only if this host
needs a value to differ from the shared config (a different `RUN_INTERVAL_HOURS`, say). Anything set
there wins over `config.json`.

Then lock them down — `config.json` holds every password in plaintext:

```bash
chmod 600 config.json .state.json
```

**Or let the dashboard ask.** Create the two files empty, start the container, and open the
dashboard: a fresh install lands on the setup wizard at `/setup` (restore a backup, password, keys,
profiles, groups, cards, alerts, schedule), each step saving into `config.json` as the Settings page
does ([docs/operations.md](docs/operations.md#first-time-setup-setup)). Published beyond loopback
before it has a password, it first asks for one, with a setup token from its log (step 4a).

```bash
touch config.json .state.json && chmod 600 config.json .state.json   # BEFORE the first `up`
```

The empty files matter: `docker-compose.yml` bind-mounts both, and a host file missing at the first
`docker compose up` becomes an empty *directory* that nothing can write, so every wizard save fails.
If that has happened, the entrypoint's log and preflight say so with the fix: `docker compose down`,
`rmdir` the directory, `touch` the file, `up` again.


> **The `warehouses` and `cards` sections are optional but not no-ops.** Without `warehouses` every
> order tags `Unclassified`, so **nothing is ever submitted to a buying group** (the sync skips
> unclassified rows). Without `cards` every row falls back to `DEFAULT_CASHBACK_RATE` and the profit
> column is wrong but plausible. Preflight (step 3) reports both.

---

## 2. The one thing that breaks on a new host: the MaxOutDeals IP allowlist

**MOD rejects every call from an unregistered IP, whatever the token.** A new host can mean a new
egress IP, and tracking pushes fail from the moment you cut over.

```bash
curl -s https://api.ipify.org; echo      # this host's public IP
```

If the host sits behind the same NAT/router as the machine running it today, the public IP is the
same and there is nothing to do. Otherwise add it under the **firewall tab** in your MOD profile, and
re-check whenever your ISP rotates your address.

BFMR has no allowlist. Costco is unaffected: it connects through the profile's own static ISP proxy,
not the host's IP.

---

## 3. Preflight — run this before anything else

```bash
docker compose run --rm --entrypoint python ledger -m scripts.preflight
```

Or natively: `.venv/bin/python -m scripts.preflight`. It is offline and free. It exists for the
failures that **keep working while doing the wrong thing**:

- **a broken deterministic-path import** — the retailer fails every run with a dossier that looks
  like a selector problem;
- **a bind mount whose host file is missing** — Docker creates an empty *directory*, read as "not
  configured" and unwritable (wizard saves, Costco's token) until you `rmdir` it and `touch` the file;
- **a missing Costco refresh token** — self-heals from the `auth.costco` credentials; without those,
  Costco alerts and skips every run.

Fix every `FAIL` before continuing; a `WARN` is a judgement call. Nothing offline can verify the MOD
allowlist (step 2): the first tracking push tells you, with "Authorization header is not recognized".

---

## 4a. Run it with Docker

```bash
docker compose up -d --build
docker compose logs -f
```

The image builds for the host's architecture and smoke-tests the supercronic binary during the build,
so a wrong-architecture download fails the build instead of crash-looping at 03:00.

Container settings live in `config.json` under `container`:

| `container.*` key | Env override | Default | Notes |
|---|---|---|---|
| `run_interval_hours` | `RUN_INTERVAL_HOURS` | `6` | Rejected and reset to 6 if not 1–23. See below before lowering. |
| `run_on_start` | `RUN_ON_START` | `false` | `true` = also run once at container start. Useful for the first cutover. |
| `timezone` | `TZ` | `UTC` | The cron schedule follows this. |
| `preflight_strict` | `PREFLIGHT_STRICT` | `false` | `true` = refuse to start when preflight fails. |

Compose cannot read `config.json` itself, so `docker/entrypoint.sh` resolves these on every start
(`python -m scripts.container_settings`); an exported variable still wins. Change the interval in
`config.json` (or on the dashboard's Settings page), then:

```bash
docker compose restart        # the entrypoint re-reads the mounted config.json on start
docker compose logs | grep "scheduled every"
```

Use `docker compose up -d` instead if you edited `docker-compose.yml` or set `RUN_INTERVAL_HOURS` in
`.env`, since those are fixed when the container is created.

**The dashboard** runs in the same container, published on the host's loopback
(`127.0.0.1:8765`). To reach it from your LAN or over Tailscale / WireGuard, set `WEB_PUBLISH_HOST`
(`0.0.0.0`, or one interface's IP) in `.env` and re-create with `docker compose up -d`. Compose passes
`WEB_PUBLISH_HOST` into the container, so the dashboard knows it is reachable: **published beyond
loopback with no `web.password`, it serves only a page that sets one**, and that page asks for a
one-time setup token printed to the dashboard's log (`docker compose logs ledger`). Reach it under a
host name (`ledger.local`, a Tailscale name)? Add the name to `web.allowed_hosts`, or the dashboard
answers 400. Never port-forward it to the internet. Details: [docs/operations.md](docs/operations.md#security).

### Choosing an interval

**A third party sets the floor.** MaxOutDeals allows **10 received-items calls per day**, and every
run spends one:

| Interval | Runs/day | MOD receipts used | Headroom |
|---|---|---|---|
| 6h | 4 | 4 of 10 | comfortable |
| 4h | 6 | 6 of 10 | fine |
| **3h** | **8** | **8 of 10** | **2 spare — the practical floor** |
| 2h | 12 | over quota | payout write-back fails daily |

- **A dry run spends one too.** `python -m sync_tracking` without `--apply` still reads payouts, as
  does `scripts.bg_probe`. At 3h there is room for about two of those a day.
- **Nothing local stops you.** `DailyCallBudget` is per-process and cannot see the day's total; MOD's
  server just starts refusing.

Going over affects only the payout / premium / status **write-back**, not tracking submission (the
30/day push limit is comfortable at 8 runs): payouts stop updating until the daily reset, and the run
alerts.

**Preflight runs on every container start and alerts but does not abort**: a container that refuses
to start also stops scraping, and a missed order costs reimbursement money. Set `preflight_strict` to
`true` if you'd rather fail fast.

### Verify the first run

```bash
docker compose exec ledger /usr/local/bin/run_once.sh    # force a run now
docker compose logs --tail=100
docker ps                                                 # STATUS should read (healthy)
```

"Up 3 weeks" proves supercronic is alive, **not** that it ran anything. Every completed run stamps
`logs/.last_run`; once that is older than two intervals the container goes unhealthy and sends the
usual email / Discord alert, once per outage, with an all-clear when a run completes again.

---

## 4b. Or run it with venv + cron

Simpler to poke at over SSH. Ubuntu 24.04 ships Python 3.12, so no extra PPA:

```bash
sudo apt-get update && sudo apt-get install -y python3-venv tmux
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m scripts.preflight

./scripts/install_cron.sh          # every 6h -> 00:00 / 06:00 / 12:00 / 18:00
./scripts/install_cron.sh 4        # every 4h; re-run with any interval to change it
crontab -l                         # confirm
```

`run.sh` is what cron calls. It rotates `logs/cron.log` at 10 MB, always records how a run *ended*,
and stamps the same `logs/.last_run` heartbeat the Docker healthcheck uses. For the dashboard, install
`requirements-web.txt` too and run `python -m web` ([docs/operations.md](docs/operations.md#running-it)).

**Cron runs with a minimal environment**, so if a scheduled run behaves differently from a manual one,
suspect that first. Reproduce it with `env -i ./run.sh`.

### Watching a run live

```bash
tail -f logs/cron.log                # follow the scheduled runs
./run.sh amazon                      # run one retailer in the foreground

tmux new -s ledger                   # then: ./run.sh
#   Ctrl-B then D  -> detach; the run keeps going
tmux attach -t ledger                # reattach later, from any SSH connection
```

A run started in a plain SSH session **dies with the connection**, and a scrape killed halfway can
leave the run lock behind (it self-expires after 3h). tmux survives the disconnect.

With Docker none of this applies: the container isn't tied to your session, so
`docker compose logs -f` is the whole story.

---

## 5. Monitoring

Three independent signals:

**Alerts push to you.** Email and Discord fire on logged-out sessions, failed scrapes, failed syncs
and preflight failures. Confirm they work *from this host* — outbound SMTP on port 587 is often
blocked by hosting networks:

```bash
docker compose run --rm --entrypoint python ledger -m alerts.notifier
```

**The healthcheck notices silence** — the scheduler quietly not running, which produces no error to
alert on. It reports `(unhealthy)` and sends an alert:

```bash
docker inspect --format '{{.State.Health.Status}}' buying-group-ledger
cat logs/.last_run
```

**The audit checks the data.** It opens the ledger through a read-only handle that refuses every
write (the dashboard's Audit page shows the same findings by row):

```bash
docker compose run --rm --entrypoint python ledger -m scripts.audit_ledger
docker compose run --rm --entrypoint python ledger -m scripts.audit_ledger --stale-days 2
```

`--stale-days` catches open orders that stopped being re-scraped: the data-side shadow of a dead
scheduler.

---

## 6. Cutting over from the old machine

Do these in order, or two schedulers submit to the same buying-group accounts from two ledgers that
then disagree:

1. **Stop any scheduler on the old machine first.** On Windows, check with
   `Get-ScheduledTask -TaskName BuyingGroupLedger`; if it exists, `Disable-ScheduledTask -TaskName
   BuyingGroupLedger`. Stop any local container: `docker compose down`. The run lock is a file in
   `logs/`, so it will **not** stop two hosts running at once, and the ledger you copy is only
   current if nothing writes the old one afterwards.
2. Carry the ledger across (step 1: `python -m scripts.backup` there, `--restore` here), then
   snapshot it so you can prove the first run behaved:
   `python -m scripts.audit_ledger --save-snapshot before.json`
3. Bring the server up and force one run.
4. `python -m scripts.audit_ledger --compare before.json` — a healthy cutover updates rows and appends
   only genuinely new orders. Duplicates show as added rows with keys you recognise.

Nothing needs re-authorising: profiles and proxies are cloud-side, and the ledger travels in the
backup. The only host-bound things are the MOD IP allowlist (step 2) and the scheduler itself.

---


## Troubleshooting

| Symptom | Cause |
|---|---|
| `exec format error` on start | Wrong-architecture image. Rebuild on the host; don't copy an image built elsewhere. |
| `exec /usr/local/bin/entrypoint.sh: no such file` | CRLF line endings. `.gitattributes` forces LF on `*.sh`, `Dockerfile` and YAML; clone rather than copying files from Windows by hand. |
| Every retailer alerts "scrape failed — not recorded this run" on every run | A deterministic-path import is broken (the dossier's traceback says `ImportError`). Run preflight. |
| One retailer alerts "scrape failed — not recorded this run" | The page or API changed shape. Open the dossier the alert names (`logs/failures/…/report.md`, or its row on the Activity page): the selector audit says which selector stopped matching, and `page_N.html` is the DOM to fix it against. |
| Buying Group column is all `Unclassified` | `config.json` has no `warehouses` section, or no jig matched the delivery address. Preflight distinguishes these. |
| MOD calls rejected with a valid token | This host's IP isn't allowlisted (step 2), or your ISP rotated it. |
| Container `(unhealthy)` but logs look fine | No run has completed within two intervals. Check the run lock: `cat logs/.run.lock` — it self-expires after 3h. |
| Runs skipped with "another run appears to be in progress" | A stale lock from a killed run. It clears itself after 3h, or `rm logs/.run.lock`. |
| Costco alerts "API auth failed — not recorded this run" every run | Dead refresh token, and the automatic refresh failed. Log the profile into costco.com again (Tools → Log a Profile In), or `python -m scripts.costco_token --label <profile> --grab`; see [docs/retailers.md](docs/retailers.md), "Costco". |
| Dashboard answers 400 | You reached it under a host name it doesn't know: add the name to `web.allowed_hosts` (or set `web.public_url`). |
| Dashboard redirects every page to `/first-password` | It is published beyond loopback with no `web.password`. Enter the setup token from `docker compose logs ledger`. |
| Alerts never arrive | Outbound SMTP (587) blocked by the host network. Test with `python -m alerts.notifier`. |
| Wrong dates on rows | The host clock. `timedatectl` — every date in the ledger comes from the host, and `Order Date` is part of the upsert key. |
