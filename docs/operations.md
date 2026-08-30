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
