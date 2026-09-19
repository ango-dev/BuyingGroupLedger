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
`python -m scripts.audit_ledger --stale-days 2` (see [Auditing the ledger](diagnostics.md#auditing-the-ledger)).

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

Profiles and proxies are cloud-side; the ledger is not — it is `data/ledger.sqlite3`, beside the
receipts under `data/`. So migration is: `python -m scripts.backup` on the old machine, copy the
zip, `python -m scripts.backup --restore <zip>` on the new one (`config.json`, `.state.json`, `.env`
and `data/` in one; standard library only, so the system Python does it before the venv exists) —
or copy the project **except** `.venv/`, `__pycache__/`, `logs/`, making sure `data/` and the two
gitignored files (`config.json` and, if you use Costco, `.state.json`) come with it. `config.json`
holds every live credential in plaintext, so move it securely; then recreate the venv (`python -m venv .venv && …/pip install -r requirements.txt`) and
re-install the scheduler on the new host. No re-login needed.

**Two things do not travel with the files:**

- ⚠️ **MaxOutDeals allowlists by IP.** A new machine has a new egress IP, so tracking pushes start
  failing however valid the token. Add the new host under the firewall tab in your MOD profile
  (`curl -s https://api.ipify.org` tells you what to add). BFMR has no allowlist, and Costco egresses
  through the profile's own static ISP proxy, so neither is affected.
- **Stop the old scheduler before starting the new one.** The overlap lock is a file in `logs/`, so it
  is per-machine and will not stop two hosts running at once — each would submit tracking and file
  insurance against the same buying-group accounts, into two ledgers that then disagree.

Then run `python -m scripts.preflight` on the new host before trusting it. Moving to a dedicated
Linux host has its own runbook: **[DEPLOY.md](../DEPLOY.md)**.

## The web dashboard

The ledger's UI: a local web page over `data/ledger.sqlite3`, in `web/`: an overview (open rows by status and buying group,
projected versus realized profit; the scheduler heartbeat is the pill in the header, stale after
`web.heartbeat_stale_hours` -- its tooltip says so -- and the COGS input gaps are the Audit page's
`cogs_inputs_complete` finding), a filterable and
sortable ledger table, one page per order, an Activity page (what every run
and every dashboard change did, with the failure dossiers' reports rendered in place), an **Audit**
page and a **Reconciliation** page (both the Orders view over the affected rows — see below), a
**Taxes** page (the year on Schedule C, with the year's expense list, program cashback, card
sign-up bonuses, cashback-site payouts and anything else you enter for it — see below), a Settings
page (with backup and restore), a Tools menu, and `/health` as JSON for the container's
healthcheck. FastAPI + Jinja2 + htmx, no build step; a light/dark toggle in
the header (remembered per browser; follows the system until you choose). The dependencies are
`requirements-web.txt`, an optional install on a desktop and part of the one Docker image.

**No automatic writes.** Every *read* of the ledger goes through `web/ledger_reader.py`, which has
no write path, so nothing that merely displays the ledger can write it. The dashboard never calls
a retailer or a buying-group API on its own, never runs a scrape on its own (Tools → *Run once* is
you pressing the button), and never changes the ledger schema (`FIELDNAMES` / `HEADER` are frozen;
`tests/test_schema.py` enforces them). **The ledger writes are the ones you make by hand on the
Orders page** — a cell edited, a row added, rows deleted — through `web/ledger_writer.py`
(`LedgerCellWriter`) and nothing else: `tests/test_web.py` scans every other file in `web/` for
the worksheet's write methods, and `tests/test_web_edit.py` pins what that one file may do. The
other `POST`s (Backup, Settings, receipts, tax inputs) write local files only.

**Editing on the Orders page.** The table is the ledger: every column in `HEADER` order, rows
coloured by status (ordered red, shipped orange, delivered yellow, paid green, return terracotta,
cancelled grey and superseded dark grey with strikethrough), a frozen header, full page width. It
edits like a spreadsheet: click a
cell to select it, Ctrl-click to add one to the selection (or take a selected one out), shift-click
or drag for a range; double-click, Enter or just start typing to
edit (the keystroke replaces the value); Enter saves, and fills every cell of a selected range;
Esc cancels; Delete clears the selection; Ctrl+C copies the selection as tab-separated values
(it pastes into Sheets or Excel) and Ctrl+V pastes a value into every selected cell or a block
cell by cell from the top-left; the arrow keys move, with Shift they extend; Ctrl+Z undoes the
last accepted write and Ctrl+Y (or Ctrl+Shift+Z) redoes it -- a range fill, a paste or Ctrl+; is one
step, the old value goes back through the same conflict check, and the stacks are the page load's
(added and deleted rows are not undone this way). Clicking the only selected row again deselects it. A date cell opens the page's own calendar (so
does every date field on a form, and the Placed in / Paid in filters open a month grid; the month
name and the year in the calendar's head are buttons, so any month or year is a click away; Ctrl+;
puts today's date into a date cell or field, as in Sheets); Status, Retailer, Buying Group, Card Name, Card Last 4 and Profile
open the column's every previous answer (most used first, the current one marked), typing narrows
the list, and a new answer typed in is a previous answer from then on; Card Name and Card Last 4
narrow each other by the pairings the ledger and the settings' cards know. Tracking Submitted is a
checkbox: a click, Space or Enter toggles it. The Receipt Link cell carries an upload button
(⤒): pick a file and the link lands on every row of the order, the table re-rendering in place.
Every write is one conflict-checked cell write, run one after another. **A cell you edit is protected from then
on** while the count line's *keep my edits from runs* switch is on (it is, on Orders; off on Audit
and Recon, where an edit usually fixes a finding the run should then own -- switched off, a write
is a correction the runs may overwrite, and it releases any protection the cell had): the scheduled
run's upsert, its order-level reproration and the buying-group sync all keep a hand-typed value
(`ledger_db/hand_edits`, a table in the ledger file that the dashboard's writer fills; the cell
shows a coloured left edge and says so in its tooltip). Clearing the cell puts back what the run had
written before you typed (a cell that was blank before is left blank) and releases it; deleting the row clears its marks; Tools
→ Ledger Fixes → *Hand-edited cells* lists them and can release an order's cells (or one field)
when you want the run to take over again. The repair scripts (backfills, retag, the profit-column
refresh) are your own explicit rewrites and are not gated. A cell that shows a link (Order Link, Tracking Link, Receipt
Link, a tracking number with its carrier link) cannot be double-clicked into — the click follows
the link — so it carries a ✎ pencil that opens the editor, and a blank link cell shows *add ↗* on
hover and edits on a single click. An edit lands in the ledger as one key-located, conflict-checked
cell write: the row is found **by its key** on a fresh read (the ledger may have re-sorted), the
cell must still hold what the page showed or the edit is refused as a conflict, a value goes
through the upsert's own coercion (numbers stay numbers, checkboxes booleans, dates plain text),
and a blank clears the cell. Not editable: the four key columns (Order ID, Order Date, Item Name,
Shipment: changing one duplicates the row on the next re-check), the two computed columns (COGS,
Total Profit — derived from the row on every read) and Last Scraped At. Status must be one of the
ledger's words; dates must be `YYYY-MM-DD`. The snapshot backend is view-only (a CSV has nothing
to write to).

**Rows: select, add, delete.** Selection is Sheets-style: click a row number to select that row
and nothing else (it tints blue; a plain click on a cell likewise drops the row selection), Ctrl-click
to add a row (or take one out), drag down the numbers or shift-click for a range, click the `#`
header for every row shown (or, with anything selected, to clear); Esc clears. The counter sits on the table's count line, and the how-it-works hints for cells, rows and edits
are a small table behind the **hint** pill (a light bulb) at the line's right end (hover or focus it). With rows
selected, the Delete or Backspace key removes them after ONE confirmation that names the count (rows are
removed bottom-up so the located numbers stay valid; all-or-nothing); there is no bar or button. Setting one value on many rows is the grid's range
fill: select the cells, type the value, Enter — the field/value bar that did this is gone
(2026-09-18).
*Add a row* takes the key columns (Order Date, Order ID, Item Name, Shipment) plus the common ones,
each field in the page's own design (the calendar, the previous answers with the same Card Name /
Card Last 4 narrowing as the table, the drop zone);
it lands where the sync's own append would (after the last occupied row), Total Cost is computed
from Quantity × Cost Per Item, and the row sorts into date order on the next sync. A key that
already exists is refused.

**Filters select all that apply.** Retailer, Profile, Status, Buying group and Card (by Card
Last 4, shown with the card's name; *(blank)* for rows without one) are checkbox dropdowns: tick
any combination; *All* clears the others (and re-ticks itself when the last value
is unticked). The choice travels in the URL as repeated parameters, so links and bookmarks keep it.

**Two views.** The *View* control in the filter bar switches between the spreadsheet-like table and
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
receipt* button: the file is stored under `receipts.dir` beside the ledger under the same key the
capture uses (`<retailer>/<YYYY-MM>/<order id>.<ext>`), and the dashboard-relative link
(`/receipts/...`) becomes Receipt Link — on every row of the order, since the link is per order.
Needs receipt capture on; otherwise the page says so and nothing is stored. Accepted: pdf, png,
jpg, webp, up to 25 MB.

**While a scheduled run is in progress every write is refused** (the page says so and nothing is
written). The sync caches ledger row numbers from its pre-sync snapshot; a row deleted or appended
underneath it would put its updates on the wrong rows. The signal is the run lock the scheduler
already keeps (`logs/.run.lock`, stale after three hours, the same rule as `main.py`), so the
refusal lasts as long as the run does.

**The Tools page** runs the scripts worth running from a browser, each exactly as it runs on the
command line (`python -m scripts.<name> <args>`, the arguments its own `--help` declares): **Run
once** (`python -m main [retailer]` — a real run exactly as the schedule does it, scrape, upsert
and the buying-group sync that submits tracking and files insurance; confirmed in-page, refused
while a run holds the lock, holding that lock itself so no scheduled run overlaps it, and stamping
the heartbeat when it ends exactly as the cron wrapper does), the preflight check, the tax report, the buying-group probe, the receipt checks, the Costco token
tool, the backfills and ledger fixes (sort, retag, superseded shipments, hand-edited cells, the
one-off migrations), and the ledger audit — grouped as *Run*, *Accounts*, *Checks* and *Ledger
Fixes*. A tool that writes the ledger asks in-page first, runs dry by default where the script
has a dry run, and is refused while a scheduled run holds the run lock; one that spends (a cloud
browser session, a buying-group call) says so on its card. Output streams into the card; every run
is an Activity event. **Log a profile in** (under *Accounts* in the Tools menu) is the interactive one: it opens a live Browser-Use
session on the profile through its proxy and embeds it in the page (an *open in a new tab* link
is there for a browser that refuses the frame); log into the retailers, then *Close & save* —
closing the session is what saves the cookies, and the profile id is written to config.json. Leave
the page and the session stays open; it is closed for you after `web.tool_session_minutes`
(default 60). The recon probes and the one-off migrations stay on the command line.

**The Activity page** is the app's own account of what it did, newest first (the last seven days by
default; the *Last* control widens it): for every scheduled
run, what each retailer's scrape found and what the ledger write updated or added (split boxes,
ignored tracking numbers, key conflicts named), what the buying-group sync submitted, insured and
read back and how many rows it updated, the emails the BFMR auto-reply sent; every alert (with
its message) and every failure dossier — the Failures page lives here now:
a dossier row opens its report and files in place, and dossiers written before the
log existed are listed from disk (`/failures` redirects to the dossier rows);
and every change made from the dashboard — cell edits (with the before and after), rows added or
deleted, receipts uploaded, backups made / restored / deleted, settings saved (paths only, never a
value). Filter by kind, by the last day / week / month, by text, or by one run (click a run stamp);
expand a row for its details. The source is `logs/activity.jsonl` (`diagnostics/activity.py`),
append-only, recorded at the source — `alerts.notifier.alert` records the alert, the dossier
records itself, each step of `main.py` records what it did — so nothing is reconstructed from
`run.log`. The header's gear opens Settings; `/health` stays a JSON endpoint for the container's
healthcheck and is not linked.

**Open rows** on the overview are `ordered`, `shipped` **or `delivered`** (the buying group has not
paid yet), which is deliberately wider than the scrapers' terminal statuses; a gift-card row is
never open.

**Needs-attention cards** sit at the top of the overview only when something needs a hand:
unacknowledged alerts and failure dossiers from the last seven days, failing and warning audit
checks, short- and over-paid orders -- five to a row, each linking to Activity, Audit or Recon
filtered to the matter. The two loud cards carry an *acknowledge all* button (asked once): it
drops that card, and the count on the nav's Activity link, for everything up to the newest one
shown, records the acknowledgement in the Activity log (kind *Acknowledged*), and a newer alert
or dossier shows again. The card opens Activity on the still-unacknowledged ones only (an
*unacknowledged only* tag in the filter bar, with an ✕ to widen). There every alert or dossier
still to acknowledge has its own *acknowledge* button, left of *details*, for one at a time. **Nav badges**: on every page the Activity, Audit and Recon links wear their counts --
unacknowledged alerts plus dossiers of the last week, the rows a failing audit check flags, the
orders paid short of or over their commitment. Nothing waiting, no badge.

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
widened or narrowed without going back. The reconciliation line under Lifetime is the plain sum of
the ledger's Total Profit column, so the page can be checked against the column at a glance.

**Two backends, one adapter** (`web/ledger_reader.py`), chosen in `config.json`'s `web` section or
by `WEB_LEDGER_SOURCE=db|snapshot` (the variable table in [configuration.md](configuration.md)
lists every `WEB_*` setting):

| Backend | Reads | For |
|---|---|---|
| `db` (default) | `data/ledger.sqlite3`, the ledger (see below), re-read every `web.ledger_cache_ttl_seconds` (300 s); `?refresh=1` re-reads now | the host, and any desktop that has the file |
| `snapshot` | the newest `data/ledger_backup_*.csv` (every `--apply` script writes one), or `web.snapshot_path` | offline work and tests; view-only |

Cells are read **by column name**, so a backup written before a column moved still reads correctly;
`/health` reports `schema_matches: false` (with the missing and extra columns) when a source's header
is not the current order. `COGS` and `Total Profit` are never stored — `web.ledger_reader.cogs_of`
/ `profit_of` compute them from the row (the ledger's own worksheet adapter uses the same two
functions, pinned against `ledger.sync._cogs_formula`'s cell references by a test), so an old CSV
backup that still holds them as formula text reads the same.

**Projected versus realized.** Since 2026-09-11 a Actual Payout with a blank Payout Date on an open
row is BFMR's *committed* price, not money received ([data model](data-model.md)). The dashboard
reads the `(Payout Date, Status)` pair exactly as the audit does: a payout cell is **settled** when
its date is set or the status is `paid` / `return` (MOD's paid rows carry no date) — a `$0.00`
settlement included, since a return or clawback that paid nothing is a real loss the ledger's Total
Profit shows — and a non-zero amount with neither is **committed**. Projected profit sums the
committed rows, realized profit the settled ones, and a committed cell is tagged `proj.` in every
table. The **Total Profit column sum** tile is the plain sum of the column (realized + projected,
plus anything with a payout cell in neither state), so the page reconciles with the ledger at a
glance; numbers come from the stored values, not the displayed cents, so the sums agree to the
cent. Since 2026-09-18 the commitment has its own `Expected Payout` column ([data
model](data-model.md)); the projected figure is a view over it.

**Running it locally** (main PC; nothing to configure):

```bash
.venv/Scripts/pip install -r requirements-web.txt      # once (Linux: .venv/bin/pip)
python -m web                                          # http://127.0.0.1:8765/ over data/ledger.sqlite3
python -m web --source snapshot                        # the newest data/ledger_backup_*.csv, view-only
python -m web --snapshot data/ledger_backup_20260910T105451Z.csv
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

`web.ledger_source` is `db` unless you say otherwise. `WEB_ENABLED=false` (or `web.enabled:
false`) makes the container a pure scheduler again.

### The ledger file (`ledger_db/`)

`data/ledger.sqlite3` (`database.path` / `LEDGER_DB_PATH`) **is** the ledger: one `ledger_rows`
table whose columns are `FIELDNAMES` in order, typed from `ledger/sync.py`'s own field sets, keyed
on the upsert key, plus the `hand_edits` table that records the cells the dashboard protected.
Every writer — the scrapers' upsert, the sort, the buying-group sync (payouts, insurance, the
submitted tick), the BFMR auto-reply, the dashboard's editor, the repair scripts — and every reader
(`load_order_state`, the sync, the audit, the tax report, the dashboard) goes through
`ledger_db/worksheet.py:DbWorksheet`, a worksheet-faced adapter (`get_all_values` / `get_values` /
`update` / `batch_update` / `sort` / `delete_rows`) that `ledger.sync._get_worksheet()` hands out.
The money-path code addresses the ledger as a positional grid through those few methods, so it
runs unchanged over the file and the tests that pin its behaviour run against the adapter. The
audit and the tax report open it through `scripts.audit_ledger.open_ledger_readonly`, a handle
that refuses every write.

COGS and Total Profit are never stored: the two columns are computed from the row on every read
(`web.ledger_reader.cogs_of` / `profit_of`), and a write into them is ignored. A row with no
Order ID is not a ledger row and is never stored. The file **migrates its own table on open**:
when `FIELDNAMES` changes, `ledger_db/store.py` rebuilds `ledger_rows` in the new order with every
value carried across by column *name*, and refuses loudly — restore a backup or migrate by hand —
if a populated table holds a column the schema no longer knows, rather than drop it. The Google
Sheet the file replaced was retired on 2026-09-18; its story is in `the design notes`.

### Backup and restore

One zip of everything a `git clone` does not give you — `config.json`, `.state.json`, `.env` and the
whole `data/` directory (the ledger, the receipts, the CSV safety copies, the audit snapshots), with a
manifest naming the commit it came from. Logs and failure dossiers are not included. **The archive
holds every live credential**: keep it private (`backups/` is gitignored and never enters an image).

```bash
python -m scripts.backup                       # -> backups/ledger_backup_<UTC stamp>.zip
python -m scripts.backup --list
python -m scripts.backup --restore backups/ledger_backup_20260917T120000Z.zip          # keeps existing files
python -m scripts.backup --restore backups/ledger_backup_20260917T120000Z.zip --force  # overwrites them
```

**Scheduled backups** (2026-09-18). The container's own cron makes one on the `backups.*`
schedule in `config.json` — `enabled`, `frequency` (`daily` / `weekly` / `monthly`), `time`
(HH:MM in `container.timezone`), `days` (weekly: `mon,thu`; monthly: `1,15`; blank = Sunday / the
1st) and `keep` (the oldest zips beyond this are deleted after every backup, scheduled or from the
page; `0` keeps all) — all on the Settings page under *Backups*, taking effect at the next
container start (the page prompts for the restart). `docker/entrypoint.sh` asks `python -m
scripts.backup --print-cron` for the line and appends it to the crontab; `backup_once.sh` runs
`python -m scripts.backup --scheduled`, which records the backup in the Activity log and ALERTS if
it fails. The ledger goes into the zip through SQLite's own backup API (opened read-only), so
a backup that lands during a run is still a consistent database. On a native install put the
same line in your crontab: `$(python -m scripts.backup --print-cron) cd /path/to/repo && .venv/bin/python -m scripts.backup --scheduled`.
The zips stay on the same machine — copy `backups/` somewhere else (rsync over your WireGuard link,
a second disk, object storage) so the ledger survives the Pi's card.

The script is **standard library only**, so on a new machine the whole move is: `git clone`, then
`python -m scripts.backup --restore <zip>` with the system Python, then the normal setup
(`docker compose up -d --build`, or the venv). The dashboard's Settings page has a **Backup & Restore** panel that offers the same
(`/backup` redirects there):
a *Create a backup now* button (the zip lands in `backups/`, which the compose file bind-mounts so
a rebuild does not take it with it; each is downloadable and deletable from the page, deletion
confirmed in-page), and a *Restore* upload form, on any host: files that already exist are kept
unless *overwrite* is ticked, the upload is confirmed in-page, and a restore that brings a
`config.json` prompts for the container restart that makes it take effect. The page has no login,
so restoring from it is only as safe as the network the dashboard is on — keep it on loopback or
your own network. Each backup's manifest names the commit it was made from; inside the image that
comes from `.git/HEAD` and its ref, which `.dockerignore` lets in for exactly this.

### The Audit and Reconciliation pages

Both are the **Orders view** — the same filter bar, table or cards, sort, search and cell editing
— over a subset of rows, with a **Finding** column (table) or block (card) beside each.
Neither writes anything.

- **Audit** (`/audit`, `web/audit_view.py`; its description, tiles and checks panel sit under the title and scroll away with it, after which the filter bar sticks to the top, as on Orders; the four tiles filter the rows to the checks they count): every check of `scripts.audit_ledger` run against the
  ledger the dashboard serves (through the worksheet adapter — the CLI's own read-only path), then
  every row a check named, mapped to its order. The lead shows the
  checks with their status and how many rows each flagged; the **Check** dropdown narrows the rows
  to the checks you pick. A detail line that names no row (a count, advice to run a backfill) is
  shown under its check in the summary. The report is rebuilt whenever the rows change (a cell
  edit, a sync) and shared between a page load and its htmx swaps.
- **Reconciliation** (`/recon`, `web/recon_view.py`): every order the buying group paid **more or
  less** than it committed to — Actual Payout against Expected Payout, compared as order totals
  over the same settled rows (two cents of tolerance plus a cent per row for proration drift), the
  biggest gap first, with short-paid / over-paid / net totals in the lead. A row with no
  commitment (MOD publishes none) is not compared. Fix a figure by editing the cell; take a real
  shortfall up with the group.

Each page -- Orders, Audit, Recon -- remembers its **own** view, page size and filters, and
its Reset button clears only its own.


### The Taxes page

`/taxes?year=YYYY` (`web/tax_inputs.py`) lays the tax year out on Schedule C's lines from the
cash-basis report `scripts/tax_report` computes (Part I lines 1, 4, 6, 7; Part II lines 15, 27a,
28, 31; Part III lines 36 and 42, with the card cashback netted from cost on its own row), and
holds what the ledger cannot know:

- **Expenses** — your own list of everything spent for the business in the year beyond the
  ledger's purchases. Every entry requires its date (in the year), amount, the profile and the
  email of the account that paid, and a receipt: an uploaded file (kept under `data/expenses/`,
  inside every backup, served back from the page) or a link. Who paid is the profile OR the
  email of the account -- one is enough. A row's *edit* button opens it in the form and saves it
  in place (the receipt stays unless a new file or a different link is given). Deleting an entry
  deletes its file. The list itself is a grid like the Orders table: click a cell, type, Enter;
  fill a range; Ctrl+C / Ctrl+V; Ctrl+Z; the row numbers select rows and Delete removes them after
  one confirmation. A link typed into a receipt cell replaces an uploaded file; a blank leaves the
  receipt alone, since one is required.
  Counted under line 27a.
- **Program cashback** — Prime (young adult cashback), Prime Business (rewards) and Costco
  Executive (cashback) pay cashback of their own,
  separate from any card or portal: one row per retailer login in your profiles. Line 6.
- **Cards** — one row per card used on an order placed in the year (every Card Last 4 on the
  year's rows, whether or not the Cards settings list it), minus any the settings mark `virtual`:
  the sign-up bonus received (line 6). A card's annual fee is an expense: it goes in the
  Expenses list with its receipt.
- **Cashback sites** — the usual portals plus any you add. Line 6.
- **Other income** — an open list of income lines, and notes for the preparer. Income only: every
  expense goes through the expense list, with its receipt.

Everything is stored per year in `data/tax_inputs.json`. It is a summary for a preparer, not tax
advice — every line says what it holds. Nothing on the page writes the ledger.

### The Settings page

`/settings` edits `config.json` in place. A side index lists the panels; each panel is one
section of the file with its fields in a label / input grid (the variable name and any restart or
*env override* tag under the label, the example file's comment as help under the input). Booleans
are checkboxes; secrets are password fields that show only whether a value is set (blank keeps it,
*clear* blanks it). A key `config.json` omits shows the value the code applies for it, tagged
*default* (so a flag that defaults to on reads as ticked); saving writes it to the file. The
The combined-package Gmail address and app password are the auto-reply's own account, entered
separately from the alerts account even when they are the same (the two are not linked). The sticky bar at the end of the scalar form says whether there are unsaved
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
anything the cards do not cover. Your `//` comment keys survive, because the write goes
through the same `config.loader.save_config` that `scripts/create_profile.py` uses. A setting
whose variable is exported in the environment is marked *env override*: the file is saved, but
the environment still wins for the running process, as everywhere else.

**When a change takes effect.** Three scopes, and the page tells you which one a save touched:

| Setting | Applies | The page |
|---|---|---|
| everything else | on the next scheduled run — each run is a fresh process that reads the file on start | nothing to do |
| `web.*`, `database.*` | after the dashboard restarts — it reads settings once at start | prompts for *Restart dashboard* (the entrypoint's loop brings it back within seconds; on a desktop, run `python -m web` again) |
| what the entrypoint resolves at container start: `container.*` and `web.enabled` (derived from `scripts/container_settings.py`'s export list, so a new knob there is prompted for automatically) | after a container restart | prompts with a *Restart the container now* button (also under *Apply*); the fields carry a *container restart* tag. The button signals the container's main process and the compose policy brings it back in about half a minute, config.json re-read; it is **refused while a run is in progress**, so nothing is aborted. `docker compose restart` on the host does the same |

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

**What the dashboard cannot do, by design:** change the ledger's columns, or submit tracking, file
insurance or scrape *on its own* — those happen in a run (the schedule's, or Tools → *Run once*,
which is you pressing the button and confirming in-page). For anything else, the commands in
[CLAUDE.md](../CLAUDE.md)'s cost table remain the way.
