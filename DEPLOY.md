# Deploying to a Linux server

A runbook for moving the ledger off a desktop and onto a host that runs it unattended. Written for an
**Ubuntu 24.04 LTS VM** (the reference target); a Raspberry Pi on 64-bit Pi OS works the same way,
with the few differences called out inline.

The README explains how the system works. This explains how to make it *keep* running somewhere you
aren't watching.

Two paths are supported, and you can switch between them freely because neither owns any state — the
Google Sheet is the source of truth and the browser profiles live in Browser-Use Cloud.

| | **Docker** (recommended) | **venv + cron** |
|---|---|---|
| scheduling | supercronic, inside the container | host crontab |
| Python | pinned 3.12 in the image | the host's (24.04 ships 3.12, so this is fine) |
| upgrade | `git pull && docker compose up -d --build` | `git pull && .venv/bin/pip install -r requirements.txt` |
| health | `docker ps` shows healthy/unhealthy | `logs/.last_run` + your own eyes |
| best for | leaving it alone | debugging over SSH |

---

## What the host does, and how small it can be

Worth being clear about, because it sets the hardware bar very low: **no browser ever runs on this
machine.** Chromium runs in Browser-Use Cloud, and `scrapers/cdp.py` connects to it over the network
— Playwright is used purely as a CDP *client*. Costco doesn't even do that; it's plain HTTPS.

So the host does HTTP requests, HTML parsing and Sheets writes. **1 vCPU and 1–2 GB RAM is ample**,
and a run is I/O-bound on the network rather than CPU-bound. Disk is a few GB for the image plus
logs.

What it *does* need is a correct clock — every date in the ledger comes from the host, and `Order
Date` is part of the upsert key, so a skewed clock writes duplicate rows. `timedatectl` should show
NTP synchronised.

> **On a Raspberry Pi, use the 64-bit OS** (`uname -m` → `aarch64`). `curl_cffi`, which Costco's TLS
> impersonation depends on, ships prebuilt wheels for `aarch64` but not reliably for `armv7l`, so a
> 32-bit host tries to compile it from source and usually fails. Not a concern on an x86 VM.

---

## 0. Prerequisites

```bash
uname -m                 # x86_64 on a VM; aarch64 on a 64-bit Pi
docker --version         # 20.10+
docker compose version   # v2 (the plugin, not the old docker-compose binary)
timedatectl              # confirm the timezone and that NTP is synced
```

Ubuntu 24.04 ships neither Docker nor a recent Node by default. Install Docker:

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"     # then log out and back in, or `newgrp docker`
```

---

## 1. Get the code and the secrets across

The repo is safe to clone; the things that make it *work* are all gitignored. Clone the code, then
copy the secrets separately.

```bash
# the `internal` remote in .git/config is the LAN Gitea — use that from inside the network
git clone http://<gitea-host>:3000/pi/BuyingGroupLedger.git ~/BuyingGroupLedger
cd ~/BuyingGroupLedger
```

From the machine that currently runs it, copy these two files across. They hold live credentials —
use `scp`/`rsync` over SSH, not email or a cloud drive:

| Path | Contains | Required? |
|---|---|---|
| `config.json` | everything: API keys, passwords, profiles, warehouse jigs, cards, and the inlined Google service-account key | **yes** |
| `.state.json` | Costco's rotating refresh token | if you use Costco |

```powershell
# from the project dir on Windows
scp config.json .state.json you@ledger-vm:~/BuyingGroupLedger/
```

`.env` is OPTIONAL — it is only the override layer now. Copy it only if this host needs a value to
differ from the shared config, and note `RUN_INTERVAL_HOURS` can ONLY live there: docker-compose
reads it itself, not through Python.

Then lock them down — `config.json` holds every password in plaintext:

```bash
chmod 600 config.json .state.json
```

> **`config.json`'s `warehouses` and `config.json`'s `cards` are optional but are NOT no-ops if you skip them.** Without
> `config.json`'s `warehouses` every order tags `Unclassified`, which means **nothing is ever submitted to a
> buying group** — the sync skips unclassified rows rather than guessing. Without `config.json`'s `cards` every
> row falls back to `DEFAULT_CASHBACK_RATE` and your profit column is wrong but plausible-looking.
> Preflight (step 3) reports both.

---

## 2. The one thing that breaks on a new host: the MaxOutDeals IP allowlist

**MOD rejects every call from an unregistered IP, whatever the token.** Moving hosts can change the
egress IP, so tracking pushes start failing the moment you cut over.

```bash
curl -s https://api.ipify.org; echo      # this host's public IP
```

Compare it to the IP you registered for the machine running it today. **If the VM sits behind the
same NAT/router as that machine, the public IP is identical and there is nothing to do** — worth
checking before you go editing anything. If it differs, add it under the **firewall tab** in your MOD
profile. Re-check whenever your ISP rotates your address.

BFMR has no allowlist. Costco routes through the profile's own static ISP proxy (not the host IP), so
it's unaffected — see the design notes about that having been fixed deliberately.

---

## 3. Preflight — run this before anything else

```bash
docker compose run --rm --entrypoint python ledger -m scripts.preflight
```

Or natively, if you've built the venv: `.venv/bin/python -m scripts.preflight`.

It's offline and free — no Browser-Use run, no Sheets call, no network at all. It exists because the
failures that matter on an unattended host are the ones that **keep working while doing the wrong
thing**, and so never raise:

- **a deterministic-path import that broke** — `scrape()` catches `ImportError` and degrades to the
  paid Browser-Use agent, so a missing dependency reads as "fine" and just bills you forever;
- **a bind mount whose host file is missing** — Docker creates an empty *directory* there, and the
  config loaders correctly read that as "not configured";
- **a missing Costco refresh token** — falls back to the agent, works, costs money.

Fix every `FAIL` before continuing. A `WARN` is a judgement call; the MOD allowlist one always shows
because nothing on this machine can verify it for you.

---

## 4a. Run it with Docker

```bash
docker compose up -d --build
docker compose logs -f
```

The image builds for the host's own architecture and smoke-tests the supercronic binary during the
build, so a wrong-architecture download fails the build loudly instead of crash-looping at 03:00.

Settings live in `docker-compose.yml`:

| Variable | Default | Notes |
|---|---|---|
| `RUN_INTERVAL_HOURS` | `3` | 8×/day. Rejected and reset to 6 if not 1–23. See below before lowering. |
| `RUN_ON_START` | `false` | `true` = also run once at container start. Useful for the first cutover. |
| `TZ` | `America/New_York` | The cron schedule follows this. |
| `PREFLIGHT_STRICT` | `false` | `true` = refuse to start when preflight fails. |

Change the interval by editing `docker-compose.yml` and recreating the container:

```bash
docker compose up -d          # re-reads the compose file; `docker compose restart` does NOT
docker compose logs | grep "scheduled every"
```

`docker compose restart` reuses the container's existing environment, so it will silently keep the
old schedule — always use `up -d`.

### Choosing an interval

**A third party sets the floor, not this code.** MaxOutDeals allows **10 received-items calls per
day**, and every run spends exactly one, so:

| Interval | Runs/day | MOD receipts used | Headroom |
|---|---|---|---|
| 6h | 4 | 4 of 10 | comfortable |
| 4h | 6 | 6 of 10 | fine |
| **3h** | **8** | **8 of 10** | **2 spare — the practical floor** |
| 2h | 12 | over quota | payout write-back fails daily |

Two things make that tighter than it looks:

- **A dry run spends one too.** `fetch_payouts` is a non-mutating read, and `dry_run` only suppresses
  *mutating* calls — so `python -m sync_tracking` with no `--apply` still costs a receipts call, as
  does `scripts.bg_probe`. At 3h you have room for about two of those a day.
- **Nothing local stops you.** `DailyCallBudget` is deliberately per-process, so it bounds one run's
  behaviour and cannot see the day's total across scheduled runs. MOD's server is the only real
  authority, and it just starts refusing.

The failure mode is contained: going over affects only the payout/premium/status **write-back**, not
tracking submission (the 30/day push limit stays comfortable at 8 runs). Payouts simply stop updating
until the daily reset, and the run alerts rather than failing silently. Still, if you want to run
more often than 3h, raise the interval back up and let the retailers' own delivery signal carry the
status — it's free, and it's already what the scrapers read.

**Preflight runs on every container start and alerts but does not abort.** That's deliberate and it
matches the project's "reliability beats cost" rule: a container that refuses to start also stops
scraping, and a missed order costs more than a wasted agent run. Set `PREFLIGHT_STRICT=true` if you'd
rather fail fast.

### Verify the first run

```bash
docker compose exec ledger /usr/local/bin/run_once.sh    # force a run now
docker compose logs --tail=100
docker ps                                                 # STATUS should read (healthy)
```

`docker ps` showing "Up 3 weeks" proves supercronic is alive, **not** that it ever ran anything. The
healthcheck closes that gap: every completed run stamps `logs/.last_run`, and the container goes
unhealthy once that's older than two intervals.

---

## 4b. Or run it with venv + cron

Simpler to poke at over SSH. Ubuntu 24.04 ships Python 3.12, so this needs no deadsnakes PPA:

```bash
sudo apt-get update && sudo apt-get install -y python3-venv tmux
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m scripts.preflight

./scripts/install_cron.sh          # every 6h -> 00:00 / 06:00 / 12:00 / 18:00
./scripts/install_cron.sh 4        # every 4h; re-run with any interval to change it
crontab -l                         # confirm
```

`run.sh` is the entry point cron calls. It rotates `logs/cron.log` at 10 MB (unbounded, it eventually
fills the disk and takes the host down), always records how a run *ended* — including failures, which
used to stop the log mid-file with no explanation — and stamps the same `logs/.last_run` heartbeat
the Docker healthcheck uses.

**Cron runs with a minimal environment**, so if a scheduled run behaves differently from a manual
one, that's the first suspect. Reproduce it with `env -i ./run.sh`.

### Watching a run live

```bash
tail -f logs/cron.log                # follow the scheduled runs
./run.sh amazon                      # run one retailer in the foreground

tmux new -s ledger                   # then: ./run.sh
#   Ctrl-B then D  -> detach; the run keeps going
tmux attach -t ledger                # reattach later, from any SSH connection
```

`tmux` matters over SSH: a run started in a plain SSH session **dies with the connection**, and a
scrape killed halfway can leave the run lock behind (it self-expires after 3h). tmux survives the
disconnect. It's also what §6 uses for the Claude session, so it's one tool rather than two.

For Docker none of this applies — the container isn't tied to your session at all, so
`docker compose logs -f` is the whole story.

---

## 5. Monitoring

Three independent signals, in increasing order of how much you have to do:

**Alerts push to you.** Email + Discord fire on logged-out sessions, failed scrapes, failed syncs and
preflight failures. Confirm they work *from this host* — outbound SMTP on port 587 is a common thing
for a hosting network or a VM's egress rules to block:

```bash
docker compose run --rm --entrypoint python ledger -m alerts.notifier
```

**The healthcheck notices silence.** The failure nobody catches is the scheduler quietly not running:
no error, no log line, nothing to alert on. `docker ps` reporting `(unhealthy)` is that signal.

```bash
docker inspect --format '{{.State.Health.Status}}' buying-group-ledger
cat logs/.last_run
```

**The audit checks the data.** This is the only one that looks at the ledger itself, and it writes
nothing ever — it authenticates read-only:

```bash
docker compose run --rm --entrypoint python ledger -m scripts.audit_sheet
docker compose run --rm --entrypoint python ledger -m scripts.audit_sheet --stale-days 2
```

`--stale-days` catches open orders that stopped being re-scraped, which is the data-side shadow of
the same "scheduler died" failure.

---

## 6. Watching it from a browser on your main PC

The goal: Claude watches the run **on the server**, you read and steer it **from a browser on your
main PC**, and you apply any code changes on the main PC as usual.

> **A plain claude.ai/code web session cannot do this.** Cloud sessions run in an Anthropic-managed
> VM that is isolated from your machine and your network — it clones from GitHub and has no route to
> a host on your LAN. It would never see `logs/run.log` or the container.
>
> **Remote Control is the feature that does.** Claude Code runs *locally on the server* — its
> filesystem, its Docker, its logs — and exposes that same session to claude.ai/code and the Claude
> mobile app. The conversation stays in sync across the server's terminal, your browser, and your
> phone, and it reconnects on its own if the network drops.

### Install

```bash
# Node 18+ is required; 24.04's default is too old for Claude Code
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs tmux

npm install -g @anthropic-ai/claude-code

cd ~/BuyingGroupLedger
claude            # sign in with /login, and accept the workspace-trust prompt once
```

Both of those one-time steps matter: Remote Control needs a claude.ai login (not an API key), and it
must be started from a trusted project directory.

### Start the watcher

Start it inside `tmux`, because Claude Code is a foreground process — without tmux it dies when the
SSH connection drops, taking the Remote Control session with it.

```bash
tmux new -s claude
cd ~/BuyingGroupLedger
claude --permission-mode plan --remote-control "ledger"
#   Ctrl-B then D    -> detach from tmux; Claude keeps running on the server
```

It prints a `claude.ai/code` session URL (and offers a QR code if you want it on your phone).

### Connect from your main PC

Open **[claude.ai/code](https://claude.ai/code)** in any browser and pick the `ledger` session. You
are now reading and steering the session running on the server. Ask it things like:

> Has the scheduled run fired? Check the container health and the last 100 lines of logs/run.log,
> and tell me if anything looks wrong.

To get back into it on the server itself: `ssh you@ledger-vm` then `tmux attach -t claude`. The
browser and the terminal are the same conversation.

`/loop 30m check the container health and logs/run.log, and tell me only if something changed` makes
it re-check on its own rather than you re-prompting.

### Keeping it advisory: suggestions, not edits

`--permission-mode plan` is the important flag above. In plan mode Claude reads files and runs
commands to explore, but **does not edit source code** — it proposes. You then apply the change on
your main PC, where your editor and git remote already are.

Plan mode stops *edits*, though; it does not by itself stop a shell command that spends money. Add a
host-local deny list as the second layer:

```bash
mkdir -p ~/BuyingGroupLedger/.claude
cat > ~/BuyingGroupLedger/.claude/settings.local.json <<'JSON'
{
  "permissions": {
    "deny": [
      "Edit", "Write", "NotebookEdit",
      "Bash(python main.py:*)",
      "Bash(python -m sync_tracking:*)",
      "Bash(python -m scripts.sort_ledger:*)",
      "Bash(python -m scripts.reorder_sheet:*)",
      "Bash(python -m scripts.backfill_profit_columns:*)"
    ]
  }
}
JSON
```

`.claude/settings.local.json` is **gitignored**, so this restricts this host only — your main PC
keeps full edit rights and never inherits it.

### What the VM boundary actually contains

Running Claude in a dedicated VM is the right instinct, but be clear about what it buys, because the
gap is where surprises live:

- **It does contain** filesystem and process blast radius. A bad command wrecks a VM you can rebuild,
  not your daily driver, and rolling back is a snapshot restore.
- **It does not contain the credentials.** This VM holds `.env`, `config.json`'s `profiles` and
  `service_account.json` — live keys to your Google Sheet, both buying-group accounts, and Browser-Use.
  Anything running here can spend money and write to the ledger regardless of the VM boundary.
- **It does not contain the network.** By default the VM reaches your LAN (including the Gitea host)
  and the internet. Restrict egress at the hypervisor or with `ufw` if you want that narrowed.

So treat the VM as limiting *damage to the host*, and the plan mode + deny list above as limiting
*damage to the accounts*. They're different problems and you want both. Take a snapshot once it's
working — that's the cheapest rollback you'll ever have.

`CLAUDE.md` in the repo root orients that session automatically — it covers the money-spending
switches, what must never be run casually, and where to look first.

Two things to tell it that it can't infer:

- **A live run costs real money** and, with `BUYING_GROUP_SYNC_ENABLED=1`, files real insurance and
  submits real tracking to third parties. Reading logs, running `pytest`, and running `audit_sheet`
  or `preflight` are all free and safe. `python main.py` is not.
- **`sync_tracking` defaults to a dry run** and only writes with `--apply`. Keep it that way unless
  you mean it.

### If Remote Control isn't available

It's in research preview. If the flag doesn't work on your account, the fallback is the same session
over SSH: `ssh you@ledger-vm` then `tmux attach -t claude`. Same conversation, viewed from a terminal
instead of a browser.

---

## 7. Cutting over from the old machine

Do these in order, or you'll get two schedulers writing the same sheet:

1. **Stop any scheduler on the old machine first.** On Windows, check with
   `Get-ScheduledTask -TaskName BuyingGroupLedger`; if it exists, `Disable-ScheduledTask -TaskName
   BuyingGroupLedger`. Likewise stop any local container: `docker compose down`. The run lock is a
   file in `logs/`, so it is per-machine and will **not** stop two hosts scraping the same sheet.
2. Snapshot the sheet so you can prove the first run behaved:
   `python -m scripts.audit_sheet --save-snapshot before.json`
3. Bring the server up and force one run.
4. `python -m scripts.audit_sheet --compare before.json` — a healthy cutover updates rows and appends
   only genuinely new orders. Duplicates would show as added rows with keys you recognise.

Nothing needs re-authorising: profiles, proxies and the sheet are all cloud-side. The only host-bound
things are the MOD IP allowlist (step 2) and the scheduler itself.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `exec format error` on start | Wrong-architecture image. Rebuild on the host — don't copy an image built on a different architecture. |
| `exec /usr/local/bin/entrypoint.sh: no such file` | CRLF line endings. `.gitattributes` forces LF on `*.sh`, `Dockerfile` and YAML; clone rather than copying files over from Windows by hand. |
| Every retailer runs the agent; costs jump | A deterministic-path import is broken. Run preflight — this is exactly what it's for. |
| Buying Group column is all `Unclassified` | `config.json`'s `warehouses` missing, or Docker made it an empty directory. Preflight distinguishes these. |
| MOD calls rejected with a valid token | This host's IP isn't allowlisted (step 2), or your ISP rotated it. |
| Container `(unhealthy)` but logs look fine | No run has completed within two intervals. Check the run lock: `cat logs/.run.lock` — it self-expires after 3h. |
| Runs skipped with "another run appears to be in progress" | A stale lock from a killed run. It clears itself after 3h, or `rm logs/.run.lock`. |
| Costco falls back to the agent every run | Missing or expired refresh token. Re-grab it per the README's "Costco API setup". |
| Alerts never arrive | Outbound SMTP (587) blocked by the host network. Test with `python -m alerts.notifier`. |
| Wrong dates on rows | The host clock. `timedatectl` — every date in the ledger comes from the host, and `Order Date` is part of the upsert key. |
