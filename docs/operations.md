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
sortable ledger table, one page per order, the failure dossiers with each `report.md` rendered, a
Settings page (with backup and restore), and `/health` as JSON. FastAPI + Jinja2 + htmx, no build step; a light/dark toggle in
the header (remembered per browser; follows the system until you choose). The dependencies are
`requirements-web.txt`, an optional install on a desktop and part of the one Docker image.

**No automatic writes.** Every *read* of the Sheet goes through
`scripts.audit_sheet.open_worksheet_readonly`, the `spreadsheets.readonly` scope, so nothing that
merely displays the ledger can write it. The dashboard never calls a retailer or a buying-group
API, never runs a scrape, and never changes the ledger schema (`FIELDNAMES` / `HEADER` are frozen;
`tests/test_schema.py` enforces them). **The one Sheet write is a cell you edit by hand on the
Orders page**, through `web/ledger_writer.py` and nothing else: `tests/test_web.py` scans every
other file in `web/` for the write scope and the worksheet's write methods, and
`tests/test_web_edit.py` pins what that one file may do. The other `POST`s (Backup, Settings) write
local files only.

**Editing on the Orders page.** The table is the Sheet: every column in the Sheet's order, rows
coloured by the Sheet's own status rules (read from its conditional formats: ordered red, shipped
orange, delivered yellow, paid green, return terracotta, cancelled grey and superseded dark grey
with strikethrough), a frozen header, full page width. Double-click a cell (or press Enter on it)
to edit; Enter saves, Esc cancels. A cell that shows a link (Order Link, Tracking Link, Receipt
Link, a tracking number with its carrier link) cannot be double-clicked into — the click follows
the link — so it carries a ✎ pencil that opens the editor, and a blank link cell shows *add ↗* on
hover and edits on a single click. An edit lands on the Sheet exactly as if typed there: the row is
found **by its key** on a fresh read (the sheet may have re-sorted), the cell must still show what
the page showed or the edit is refused as a conflict, a value goes through the upsert's own
coercion (numbers stay numbers, checkboxes booleans, dates plain text), and a blank clears the cell
while keeping its number format. Not editable: the four key columns (Order ID, Order Date, Item
Name, Shipment: changing one duplicates the row on the next re-check), the two formulas (COGS,
Total Profit) and Last Scraped At. Status must be one of the ledger's words; dates must be
`YYYY-MM-DD`. The snapshot backend is view-only (a CSV has nothing to write to); the `db` backend
writes the Sheet and re-mirrors, so the copy follows.

**Rows: select, add, bulk edit, delete.** Selection is Sheets-style: click a row number to select
that row (it tints blue), shift-click for a range, click the `#` header for every row shown. The
bar under the filters acts on the selection: pick a field and a value and *Apply* to set it on all
of them in one batched write (blank clears), or *Delete selected* to remove them from the sheet
(confirmed first; rows are removed bottom-up so the located numbers stay valid; all-or-nothing).
*Add a row* takes the key columns (Order Date, Order ID, Item Name, Shipment) plus the common ones;
it lands where the sync's own append would (after the last occupied row, blanks sent as `None` so
the column formats survive, the two formula cells stamped), Total Cost is computed from Quantity ×
Cost Per Item, and the row sorts into date order on the next sync. A key that already exists is
refused.

**Filters select all that apply.** Retailer, Profile, Status and Buying group are checkbox
dropdowns: tick any combination; *All* clears the others (and re-ticks itself when the last value
is unticked). The choice travels in the URL as repeated parameters, so links and bookmarks keep it.

**Two views.** The *View* control in the filter bar switches between the sheet-like table and
**cards**: one card per order under the same filters and search, sorted by the *Sort by* / *Order*
controls that appear in the cards view, paginated with a *Per page* choice of 12 / 24 / 48 / 96.
A card shows the order's items one line each (numbered when there are several, with the quantity
and the shipment each is in), the order's totals (quantity, cost, payout with its state, profit)
and, behind *edit rows*, its rows with the same editable cells as the table (double-click, Enter,
Esc), the Status cell coloured as the table's rows are — including Delivery Address, Order Link,
Tracking Link and Receipt Link. A card's *Delete* removes every row of that order, through the same
all-or-nothing delete the table uses. Every confirmation on the page (bulk apply, delete, card
delete) is an in-page dialog rather than the browser's own prompt.

**Receipts by hand.** The add form takes a photo or PDF, and an order's page has an *Upload
receipt* button: the file is stored in the same OCI bucket under the same key the capture uses
(`receipts/<retailer>/<YYYY-MM>/<order id>.<ext>`), and the PAR link becomes Receipt Link — on
every row of the order, since the link is per order. Needs the receipt store configured
(`receipts.oci.bucket` and the PAR prefix); otherwise the page says so and nothing is uploaded.
Accepted: pdf, png, jpg, webp, up to 25 MB.

**While a scheduled run is in progress every write is refused** (the page says so and nothing is
written). The sync caches sheet row numbers from its pre-sync snapshot; a row deleted or appended
underneath it would put its updates on the wrong rows. The signal is the run lock the scheduler
already keeps (`logs/.run.lock`, stale after three hours, the same rule as `main.py`), so the
refusal lasts as long as the run does.

**Open rows** on the overview are `ordered`, `shipped` **or `delivered`** (the buying group has not
paid yet), which is deliberately wider than the scrapers' terminal statuses; a gift-card row is
never open.

**The overview's two stat sections** carry the same eight tiles in the same order — rows and
orders, open rows, spend (Total Cost over rows that carry money), actual return
((Payout − COGS − Insurance) / Total Cost summed over the settled rows: what a dollar spent came
back as after the cashback on shipping and tax, the gift-card and rewards netting, a return's
share, the insurance premium and the buying group's commission; dollars over dollars, so a big
order counts for more than a small one), paid out (settled payouts),
**floating** (Total Cost of the rows the buying group has not paid yet: no settled payout; gift
cards excluded), projected profit (committed, undated payouts) and realized profit (settled rows) —
so the two read side by side, all eight on one line with a few words under each number and the
full definition as the tile's tooltip. Tooltips everywhere on the dashboard are the page's own small panel (`web/static/tooltip.js` takes over every `title`), shown after a short hover or on keyboard focus, never the browser's bubble. Floating is not spend minus paid out: that gap also holds the
settled rows' cost minus their payout, and since the profit here comes from cashback a settled row
is usually paid a little less than it cost. *Lifetime* counts every row of the ledger. *Calendar month* shows one month at a time
(arrows step through the months the ledger spans; the current month by default, `/?month=YYYY-MM`
for another) and counts the rows whose **Order Date** falls in it, nothing else: paid out and
realized profit are the settled rows among them, whenever the payout landed. The cash-basis view
(what was paid in a month, whenever placed) is the Orders page's *Paid in* filter and the tax
report. Every tile is a link that opens the Orders page filtered the same way the
number was counted, and the Orders filter bar shows those filters as editable controls: *Placed in*
and *Paid in* (a month) and *Payout* (any / open / projected / settled / unpaid), so a click-through can be
widened or narrowed without going back. The reconciliation line under Lifetime is what SUM() over
the sheet's Total Profit column gives, so the page can be checked against the sheet at a glance.

**Three backends, one adapter** (`web/ledger_reader.py`), chosen in `config.json`'s `web` section or
by `WEB_LEDGER_SOURCE=snapshot|sheet|db` (the variable table in [configuration.md](configuration.md)
lists every `WEB_*` setting):

| Backend | Reads | For |
|---|---|---|
| `db` | `data/ledger.sqlite3` (see below), refreshed from the read-only Sheet every `web.sheet_cache_ttl_seconds` (300 s), or from `web.snapshot_path` when one is set | the host: instant pages, one Sheet read per interval, the copy survives restarts |
| `sheet` | the live worksheet, read-only scope, cached in memory for the same interval | a desktop look at the live ledger |
| `snapshot` (default) | the newest `data/sheet_backup_*.csv` (every `--apply` script writes one), or `web.snapshot_path` | development, tests, no credentials |

Cells are read **by column name**, so a backup written before a column moved still reads correctly;
`/health` reports `schema_matches: false` (with the missing and extra columns) when a source's header
is not the current order. A CSV backup stores `COGS` and `Total Profit` as formula text, so for those
two columns the page computes the same arithmetic as the sheet formula (`web.ledger_reader.cogs_of`,
pinned against `sheets.ledger_sync._cogs_formula`'s own cell references by a test).

**Projected versus realized.** Since 2026-09-11 a Payout Amount with a blank Payout Date on an open
row is BFMR's *committed* price, not money received ([data model](data-model.md)). The dashboard
reads the `(Payout Date, Status)` pair exactly as the audit does: a payout cell is **settled** when
its date is set or the status is `paid` / `return` (MOD's paid rows carry no date) — a `$0.00`
settlement included, since a return or clawback that paid nothing is a real loss the sheet's Total
Profit shows — and a non-zero amount with neither is **committed**. Projected profit sums the
committed rows, realized profit the settled ones, and a committed cell is tagged `proj.` in every
table. The **Total Profit column sum** tile is what `SUM()` over the sheet's column gives (realized
+ projected, plus anything with a payout cell in neither state), so the page reconciles with the
sheet at a glance; numbers come from the sheet's stored values, not the displayed cents, so the
sums agree to the cent. There is no `Expected Payout` column (parked in
`the design notes`); this is a view over the existing cell.

**Running it locally** (main PC, against a snapshot; nothing to configure):

```bash
.venv/Scripts/pip install -r requirements-web.txt      # once (Linux: .venv/bin/pip)
python -m web                                          # http://127.0.0.1:8765/ over the newest backup
python -m web --snapshot data/sheet_backup_20260910T105451Z.csv
python -m web --source sheet                           # the live Sheet, read-only
python -m web --source db                              # the SQLite copy, refreshed from the Sheet
```

Flags win over `config.json` and the environment. It binds to loopback unless `--host` (or
`web.bind_host` / `WEB_BIND_HOST`) says otherwise — there is no authentication, so anything beyond
localhost or Tailscale is a deliberate choice. `?refresh=1` on any page forces a re-read before the
cache expires (still a read).

**On the host it runs inside the one container.** `docker/entrypoint.sh` starts it beside the
scheduler when `web.enabled` is true (the default), restarts it if it ever exits, and
`docker/healthcheck.sh` probes it — a dead dashboard reports `unhealthy` with a reason that names
the dashboard, never "the scheduler stopped". It is published on the host's **loopback** by default:

```bash
docker compose up -d --build                  # the usual command; the dashboard comes with it
echo "WEB_PUBLISH_HOST=0.0.0.0" >> .env        # or the LAN / Tailscale IP to publish on
docker compose up -d                          # re-create so the new port binding applies
curl -s http://127.0.0.1:8765/health          # on the host: "ok": true, "backend": "db"
```

Set `web.ledger_source` to `db` in the host's `config.json` (the example does). `WEB_ENABLED=false`
(or `web.enabled: false`) makes the container a pure scheduler again.

### The SQLite copy of the ledger (`ledger_db/`)

`data/ledger.sqlite3` is a **mirror** of the Sheet: one `ledger_rows` table whose columns are
`FIELDNAMES` in order, typed from `sheets/ledger_sync.py`'s own field sets, keyed on the upsert key,
plus a `mirror_runs` log. Every mirror replaces the table in one transaction, so the copy is always
"the Sheet as of that read". Two things write it, and both only *read* the Sheet:

```bash
python -m scripts.mirror_sheet_to_db                                   # live Sheet -> DB (read-only scope)
python -m scripts.mirror_sheet_to_db --from-snapshot data/sheet_backup_20260910T105451Z.csv
```

the dashboard's `db` backend, which runs the same mirror on its cache interval, and **the scheduled
run itself, as its last step** (`main.run_db_mirror`, on by default via `database.mirror_after_run`):
after the scrapes, the buying-group sync and the auto-reply, the run reads the Sheet back once and
replaces the copy, so the file always holds what this run wrote. A failure there alerts and never
fails the run. **Nothing on a money path reads the file**: every writer still targets the Sheet
exactly as before, every reader (`load_order_state`, the sync, the audit, the tax report) still
reads the Sheet, and a host running an older version is unaffected by the file's existence. That is
deliberate: this is step one of moving off the Sheet as the database. The cutover (writers and
readers target SQLite, the Sheet becomes an exported view, then goes away) is a separate decision,
tracked in `the design notes`.

### Backup and restore

One zip of everything a `git clone` does not give you — `config.json`, `.state.json`, `.env` and the
whole `data/` directory (the SQLite copy, the CSV sheet backups, the audit snapshots), with a
manifest naming the commit it came from. Logs and failure dossiers are not included. **The archive
holds every live credential**: keep it private (`backups/` is gitignored and never enters an image).

```bash
python -m scripts.backup                       # -> backups/ledger_backup_<UTC stamp>.zip
python -m scripts.backup --list
python -m scripts.backup --restore backups/ledger_backup_20260917T120000Z.zip          # keeps existing files
python -m scripts.backup --restore backups/ledger_backup_20260917T120000Z.zip --force  # overwrites them
```

The script is **standard library only**, so on a new machine the whole move is: `git clone`, then
`python -m scripts.backup --restore <zip>` with the system Python, then the normal setup
(`docker compose up -d --build`, or the venv). The dashboard's Settings page has a **Backup & Restore** panel that offers the same
(`/backup` redirects there):
a *Create a backup now* button (the zip lands in `backups/` and is downloadable from the page), and a
*Restore* upload form that is enabled **only while no `config.json` exists** — on a configured host an
unauthenticated page must not be able to replace the live configuration, so there you restore from
the command line. After a restore, restart the app so the restored `config.json` is read.

### Moving off the Sheet (`ledger.backend`)

The Google Sheet is **deprecated** as the ledger's home. `ledger.backend` (`LEDGER_BACKEND`) says
where the ledger lives:

| Value | What runs where |
|---|---|
| `db` (the default) | `data/ledger.sqlite3` **is** the ledger. The scrapers' upsert, the sort, the buying-group sync (payouts, insurance, the submitted tick), the BFMR auto-reply, the dashboard's editor and the scripts all read and write it, and **nothing touches the Sheet**. |
| `sheet` (deprecated) | As before: every writer targets the Sheet, `data/ledger.sqlite3` is a mirror refreshed at the end of each run. The Settings page shows the Google Sheet settings only in this mode, tagged deprecated. |

How it works: every writer addresses the ledger as a positional grid through a handful of
`gspread.Worksheet` methods, so `ledger_db/worksheet.py` implements that surface over the SQLite
file and `sheets.ledger_sync._get_worksheet()` hands it out instead of a Google worksheet — the
money-path code runs unchanged, and the tests that pin its behaviour run against the adapter too.
COGS and Total Profit are never stored as formula text: the two columns are computed from the row
on every read (the same Python mirrors of the formulas the dashboard already used), so the numbers
are the sheet's numbers, live, for every row. A row with no Order ID is not a ledger row and is
never stored.

**A host coming from `sheet`** starts on `db` at its next start (the default) and needs its file
current first. The end-of-run mirror keeps `data/ledger.sqlite3` at the last run's state, so
between runs it already is; to be sure, or if the last run's mirror failed, take the last copy by
hand before restarting:

```bash
python -m scripts.mirror_sheet_to_db      # only runs while ledger.backend is still `sheet`
docker compose restart                    # or python -m web again on a desktop
```

To stay on the Sheet for now, set `"ledger": {"backend": "sheet"}` in `config.json` (or
`LEDGER_BACKEND=sheet`).

Under `db`: `python -m scripts.mirror_sheet_to_db` and the end-of-run mirror refuse to run (a
mirror from the stale Sheet would overwrite the ledger), the dashboard serves the file directly
whatever `web.ledger_source` says (an explicit `--source` still wins for development), `/health`
reports `ledger_backend`, and `python -m scripts.audit_sheet` exits with a note (its checks are the
Sheet's: formulas, formats, notes, grid; `--from-snapshot` still audits a saved Sheet snapshot).
The Sheet-only maintenance scripts (`reorder_sheet`, `apply_sheet_formats`, the format-related
backfills) are not meant for the database and say so if they hit a method the adapter does not
have. **Nothing of the Sheet code is deleted**: switching back is setting the flag to `sheet`
(the Sheet then lags by whatever was written meanwhile). Deleting the Sheet paths is the user's
call, tracked in the design notes.

### The Settings page

`/settings` edits `config.json` in place. A side index lists the panels; each panel is one
section of the file with its fields in a label / input grid (the variable name and any restart or
*env override* tag under the label, the example file's comment as help under the input). Booleans
are checkboxes; secrets are password fields that show only whether a value is set (blank keeps it,
*clear* blanks it). A key `config.json` omits shows the value the code applies for it, tagged
*default* (so a flag that defaults to on reads as ticked); saving writes it to the file. The
combined-package Gmail address and app password, left blank, use the alerts account at run time,
and the page shows that: the alerts address (or "uses the alerts app password") greyed in the box
as a placeholder with a *falls back* tag, never as a value, so saving cannot copy it into the file;
when both are blank the box is simply empty. The sticky bar at the end of the scalar form says whether there are unsaved
edits and saves them all at once.

**Profiles, warehouses and cards are entry cards.** Each entry is its own card with a form: a
profile's label, Browser-Use id, the retailers it is logged into (chips), its proxy and its
unattended sign-ins (one row per retailer; add one from the blank row, tick *remove* to drop one);
a warehouse's buying group and its jigs (one row each, the last row blank for a new jig); a card's
name, last 4, rate, profile scope and per-retailer rates. *Save* on a card rewrites that one entry
— rebuilt from the form on top of the stored entry, so comment keys and anything the form does
not show survive — validated by the section's model before anything is written; *✕ Remove* asks
in-page and deletes it; the dashed *+ Add* card appends one. Passwords and TOTP seeds are never
rendered: blank keeps them. *Edit … as JSON* under each section is the whole list as text, for
anything the cards do not cover. The service account stays a JSON paste, with the `client_email`
to share the sheet with shown above it. Your `//` comment keys survive, because the write goes
through the same `config.loader.save_config` that `scripts/create_profile.py` uses. A setting
whose variable is exported in the environment is marked *env override*: the file is saved, but
the environment still wins for the running process, as everywhere else.

**When a change takes effect.** Three scopes, and the page tells you which one a save touched:

| Setting | Applies | The page |
|---|---|---|
| everything else | on the next scheduled run — each run is a fresh process that reads the file on start | nothing to do |
| `web.*`, `database.*` | after the dashboard restarts — it reads settings once at start | prompts for *Restart dashboard* (the entrypoint's loop brings it back within seconds; on a desktop, run `python -m web` again) |
| what the entrypoint resolves at container start: `container.*` and `web.enabled` (derived from `scripts/container_settings.py`'s export list, so a new knob there is prompted for automatically) | after `docker compose restart` on the host | prompts with the command; the fields carry a *container restart* tag. Do it between runs: a restart mid-run aborts that run |

In Docker the `config.json` mount is writable for exactly this page (nothing in a scheduled run
writes it).

**The page is derived, not hand-built.** `web/settings_form.py` reads `ENV_TO_CONFIG`, the
`Settings` fields (type, and `repr=False` for secrets) and `config.example.json`'s `"// key"`
comments, so a scalar setting added the documented way appears with the right widget and its help
text; `tests/test_web_settings.py` fails if any variable is missing from the page. A new structured
section, a special widget, or a changed section model must be added to `SECTIONS` by hand
([CLAUDE.md](../CLAUDE.md) states the rule) — and, for it to get entry cards rather than the JSON
editor alone, a display / form-builder pair in `web/settings_form.py` (`display_entries`,
`_BUILDERS`) and a fields macro in `settings.html`. A field added to `ProfileConfig`, `Warehouse`
or `Card` needs the same: the card's form only knows the fields it renders (anything else is kept
as stored, never lost, but not editable until the form shows it).

**No login.** Whoever can open the page can read and change every credential. Keep the dashboard on
loopback or your own network; the LAN publish is opt-in for that reason.

**What the dashboard cannot do, by design:** write the Sheet, edit a row, submit tracking, file
insurance, run a scrape, or add the `Expected Payout` column. For those, the commands in
[CLAUDE.md](../CLAUDE.md)'s cost table remain the way.
