# Deploying to a Raspberry Pi

A runbook for moving the ledger off a desktop and onto a Pi that runs it unattended. The README
explains how the system works; this explains how to make it *keep* running somewhere you aren't
watching.

Two paths are supported, and you can switch between them freely because neither owns any state — the
Google Sheet is the source of truth and the browser profiles live in Browser-Use Cloud.

| | **Docker** (recommended) | **venv + cron** |
|---|---|---|
| scheduling | supercronic, inside the container | host crontab |
| Python | pinned 3.12 in the image | whatever Pi OS ships |
| upgrade | `git pull && docker compose up -d --build` | `git pull && .venv/bin/pip install -r requirements.txt` |
| health | `docker ps` shows healthy/unhealthy | `logs/.last_run` + your own eyes |
| best for | leaving it alone | debugging over SSH |

---

## What the Pi does and doesn't do

Worth being clear about, because it sets the hardware bar very low: **no browser ever runs on the
Pi.** Chromium runs in Browser-Use Cloud, and `scrapers/cdp.py` connects to it over the network —
Playwright is used purely as a CDP *client*. Costco doesn't even do that; it's plain HTTPS.

So the Pi is doing HTTP requests, HTML parsing and Sheets writes. **A Pi 4 with 2 GB is ample**, and
a run is I/O-bound on the network rather than CPU-bound. What the Pi *does* need is a 64-bit OS
(`uname -m` → `aarch64`) and a reliable clock, since every date in the ledger is derived from it.

---

## 0. Prerequisites

```bash
uname -m                 # must print aarch64 (64-bit Raspberry Pi OS)
docker --version         # 20.10+
docker compose version   # v2 (the plugin, not the old docker-compose binary)
timedatectl              # confirm the timezone and that NTP is synced
```

If Docker isn't installed:

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"     # then log out and back in, or `newgrp docker`
```

> **32-bit Pi OS won't work well.** `curl_cffi` (Costco's TLS impersonation) ships prebuilt wheels
> for `aarch64` but not reliably for `armv7l`, so a 32-bit host tries to compile it from source and
> usually fails. Re-flash with the 64-bit image rather than fighting this.

---

## 1. Get the code and the secrets across

The repo is public-safe; the things that make it *work* are all gitignored. Clone the code, then copy
the secrets separately.

```bash
git clone <your-remote> ~/BuyingGroupLedger
cd ~/BuyingGroupLedger
```

From the Windows machine, copy these six paths across. They hold live credentials — use `scp`/`rsync`
over SSH, not email or a cloud drive:

| Path | Contains | Required? |
|---|---|---|
| `.env` | every API key and password | **yes** |
| `service_account.json` | Google service-account key | **yes** |
| `profiles.json` | Browser-Use profile ids, proxy creds, Best Buy password | **yes** |
| `.costco/` | Costco refresh token per profile | if you use Costco |
| `warehouses.json` | buying-group warehouse addresses | strongly recommended |
| `cards.json` | card names + cashback rates | strongly recommended |

```powershell
# from the project dir on Windows
scp .env service_account.json profiles.json warehouses.json cards.json pi@raspberrypi.local:~/BuyingGroupLedger/
scp -r .costco pi@raspberrypi.local:~/BuyingGroupLedger/
```

Then lock them down — `.env` and `profiles.json` contain passwords in plaintext:

```bash
chmod 600 .env profiles.json service_account.json
chmod 700 .costco && chmod 600 .costco/*.json
```

> **`warehouses.json` and `cards.json` are optional but are NOT no-ops if you skip them.** Without
> `warehouses.json` every order tags `Unclassified`, which means **nothing is ever submitted to a
> buying group** — the sync skips unclassified rows rather than guessing. Without `cards.json` every
> row falls back to `DEFAULT_CASHBACK_RATE` and your profit column is wrong but plausible-looking.
> Preflight (step 3) reports both.

---

## 2. The one thing that breaks on a new host: the MaxOutDeals IP allowlist

**MOD rejects every call from an unregistered IP, whatever the token.** Moving from your desktop to
the Pi changes the egress IP, so tracking pushes start failing the moment you cut over.

```bash
curl -s https://api.ipify.org; echo      # the Pi's public IP
```

Add that under the **firewall tab** in your MOD profile. Re-check it whenever your ISP rotates your
address — if you don't have a static IP, this is the thing that will silently break weeks later.

BFMR has no allowlist. Costco routes through the profile's own static ISP proxy (not the host IP), so
it's unaffected — see the note in the design notes about that having been fixed deliberately.

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

The build takes a few minutes on a Pi (mostly `pip install`). The image is multi-arch: it detects
`TARGETARCH` and smoke-tests the supercronic binary during the build, so a wrong-architecture
download fails the build loudly instead of crash-looping at 03:00.

Settings live in `docker-compose.yml`:

| Variable | Default | Notes |
|---|---|---|
| `RUN_INTERVAL_HOURS` | `6` | 4×/day. Rejected and reset to 6 if not 1–23. |
| `RUN_ON_START` | `false` | `true` = also run once at container start. Useful for the first cutover. |
| `TZ` | `America/New_York` | The cron schedule follows this. |
| `PREFLIGHT_STRICT` | `false` | `true` = refuse to start when preflight fails. |

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

Simpler to poke at over SSH, at the cost of inheriting Pi OS's Python.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m scripts.preflight

./scripts/install_cron.sh          # every 6h -> 00:00 / 06:00 / 12:00 / 18:00
./scripts/install_cron.sh 4        # every 4h; re-run with any interval to change it
crontab -l                         # confirm
```

`run.sh` is the entry point cron calls. It rotates `logs/cron.log` at 10 MB (unbounded, it fills an
SD card months later and takes the host down), always records how a run *ended* — including failures,
which used to stop the log mid-file with no explanation — and stamps the same `logs/.last_run`
heartbeat the Docker healthcheck uses.

**Cron runs with a minimal environment**, so if a scheduled run behaves differently from a manual
one, that's the first suspect. Reproduce it with `env -i ./run.sh`.

### Watching a run live

```bash
tail -f logs/cron.log                # follow the scheduled runs
./run.sh amazon                      # run one retailer in the foreground

screen -S ledger ./run.sh            # detach with Ctrl-A D, reattach with `screen -r ledger`
```

`screen` matters over SSH: a run started in a plain SSH session dies with the connection. `screen` (or
`tmux new -s ledger`) survives it. For Docker the equivalent is just `docker compose logs -f`, since
the container isn't tied to your session at all.

---

## 5. Monitoring

Three independent signals, in increasing order of how much you have to do:

**Alerts push to you.** Email + Discord fire on logged-out sessions, failed scrapes, failed syncs and
preflight failures. Confirm they work on the Pi specifically — outbound SMTP is a common thing for a
home network to block:

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

## 6. Watching the Pi from a browser on your main PC

The goal: Claude watches the run **on the Pi**, you read and steer it **from a browser on your main
PC**, and you apply any code changes on the main PC as usual.

> **A plain claude.ai/code web session cannot do this.** Cloud sessions run in an Anthropic-managed
> VM that is isolated from your machine and your network — it clones from GitHub and has no route to
> a Raspberry Pi on your LAN. It would never see `logs/run.log` or the container.
>
> **Remote Control is the feature that does.** Claude Code runs *locally on the Pi* — its filesystem,
> its Docker, its logs — and exposes that same session to claude.ai/code and the Claude mobile app.
> The conversation stays in sync across the Pi's terminal, your browser, and your phone, and it
> reconnects on its own if the network drops.

### Install on the Pi

```bash
# Node 18+ is required; Pi OS's default is usually too old
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
claude --permission-mode plan --remote-control "ledger-pi"
#   Ctrl-B then D    -> detach from tmux; Claude keeps running on the Pi
```

It prints a `claude.ai/code` session URL (and press the indicator for a QR code if you want it on
your phone).

### Connect from your main PC

Open **[claude.ai/code](https://claude.ai/code)** in any browser and pick the `ledger-pi` session.
You are now reading and steering the session that is running on the Pi. Ask it things like:

> Has the scheduled run fired? Check the container health and the last 100 lines of logs/run.log,
> and tell me if anything looks wrong.

To get back into it on the Pi itself: `ssh pi@…` then `tmux attach -t claude`. The browser and the
terminal are the same conversation.

`/loop 30m check the container health and logs/run.log, and tell me only if something changed` makes
it re-check on its own rather than you re-prompting.

### Keeping it advisory: suggestions, not edits

`--permission-mode plan` is the important flag above. In plan mode Claude reads files and runs
commands to explore, but **does not edit source code** — it proposes. You then apply the change on
your main PC, where your editor and git remote already are.

Plan mode stops *edits*, though; it does not by itself stop a shell command that spends money. Add a
Pi-local deny list as the second layer:

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

`.claude/settings.local.json` is **gitignored**, so this restricts the Pi only — your main PC keeps
full edit rights and never inherits it.

Treat the deny list as a seatbelt, not a vault: it matches command strings, so a different spelling
(`docker compose exec ledger python main.py`) isn't covered. The real protection is that this session
exists to *observe*. `CLAUDE.md` in the repo root tells it which commands are free and which spend
money, and it's read automatically at session start.

### What Claude should and shouldn't be responsible for

**Do not make Claude the alarm.** The alerts, the healthcheck and preflight run 24/7 whether or not
any session exists — they are the monitoring of record. Claude is for *interpreting* state and
diagnosing a failure once you know there is one, which is a different job. A monitor that only works
while someone is attached isn't a monitor.

If you do want an unattended second opinion, run it headless from cron rather than leaving a session
open:

```bash
# once a day, write a plain-English verdict to a log
0 9 * * * cd ~/BuyingGroupLedger && claude -p "Run scripts.preflight and scripts.audit_sheet \
  --stale-days 2, read the last 200 lines of logs/run.log, and summarise in 5 lines whether the \
  ledger is healthy. Do not run main.py or sync_tracking." >> logs/claude_digest.log 2>&1
```

Each headless run is a **fresh session with no memory of the previous one**, and it costs tokens per
invocation — so treat it as a daily digest, not a replacement for the alerts.

`CLAUDE.md` in the repo root orients that session automatically — it covers the money-spending
switches, what must never be run casually, and where to look first.

Two things to tell it that it can't infer:

- **A live run costs real money** and, with `BUYING_GROUP_SYNC_ENABLED=1`, files real insurance and
  submits real tracking to third parties. Reading logs, running `pytest`, and running `audit_sheet`
  or `preflight` are all free and safe. `python main.py` is not.
- **`sync_tracking` defaults to a dry run** and only writes with `--apply`. Keep it that way unless
  you mean it.

---

## 7. Cutting over from the Windows machine

Do these in order, or you'll get two schedulers writing the same sheet:

1. **Stop any scheduler on the old machine first.** Check with
   `Get-ScheduledTask -TaskName BuyingGroupLedger`; if it exists, `Disable-ScheduledTask -TaskName
   BuyingGroupLedger` (or `Unregister-ScheduledTask … -Confirm:$false`). Likewise stop any local
   container: `docker compose down`. The run lock is a file in `logs/`, so it is per-machine and will
   **not** stop two hosts scraping the same sheet at once.
2. Snapshot the sheet so you can prove the first Pi run behaved:
   `python -m scripts.audit_sheet --save-snapshot before.json`
3. Bring the Pi up and force one run.
4. `python -m scripts.audit_sheet --compare before.json` — a healthy cutover updates rows and appends
   only genuinely new orders. Duplicates would show as added rows with keys you recognise.

Nothing needs re-authorising: profiles, proxies and the sheet are all cloud-side. The only host-bound
things are the MOD IP allowlist (step 2) and the scheduler itself.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `exec format error` on start | Wrong-architecture image. Rebuild on the Pi — don't copy an image built on an x86 machine. |
| `exec /usr/local/bin/entrypoint.sh: no such file` | CRLF line endings. `.gitattributes` forces LF on `*.sh`; re-clone rather than copying files over from Windows by hand. |
| Every retailer runs the agent; costs jump | A deterministic-path import is broken. Run preflight — this is exactly what it's for. |
| Buying Group column is all `Unclassified` | `warehouses.json` missing, or Docker made it an empty directory. Preflight distinguishes these. |
| MOD calls rejected with a valid token | The Pi's IP isn't allowlisted (step 2), or your ISP rotated it. |
| Container `(unhealthy)` but logs look fine | No run has completed within two intervals. Check the run lock: `cat logs/.run.lock` — it self-expires after 3h. |
| Runs skipped with "another run appears to be in progress" | A stale lock from a killed run. It clears itself after 3h, or `rm logs/.run.lock`. |
| Costco falls back to the agent every run | Missing or expired refresh token. Re-grab it per the README's "Costco API setup". |
| Wrong dates on rows | The Pi's clock. `timedatectl` — every date in the ledger comes from the host. |
