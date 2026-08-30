# Buying Group Ledger

Automated order tracking, reconciliation and payout settlement for buying-group reselling. It reads
order history from four retailer accounts (Amazon, Amazon Business, Best Buy, Costco), keeps a Google
Sheet ledger current as a live P&L, posts shipped tracking numbers to two buying-group APIs, files
shipment insurance, and reads payouts back — unattended, on a schedule.

**The design problem is cost against reliability.** A browser-driving LLM agent can read any retailer's
order page, but it bills real money on every run. A hand-written scraper is free but breaks *silently*
when a page changes — and a silently missed order is missed reimbursement, which is a worse outcome
than an expensive one. So every retailer has a **deterministic, agent-free primary path**, and when
that path fails it **fails loudly with a failure dossier** — the traceback, the page HTML and a
screenshot at the moment of failure, and an audit of every selector the parser depends on, written
to `logs/failures/` and pointed at by the alert. Normal runs cost nothing; a site change costs one
missed run and a selector fix, made from the dossier rather than guessed. The LLM agent still exists
as an opt-in fallback (`AGENT_FALLBACK_ENABLED`), off by default.

The second theme is that **the failures worth engineering against here are silent**. Nothing throws
when a scraper reads page 1 of a paginated order history and misses the rest, or when a re-check
overwrites a good tracking number with a blank. Most of the work is invariants, idempotency and
auditing aimed squarely at that class of bug — see **[Design notes](#design-notes)**.

> **Status:** running in production against real accounts. `pytest` runs ~1,480 offline tests that
> need no credentials and no network.

---

## Documentation

This page is the overview. Each topic has its own page under [`docs/`](docs/):

| Page | What it covers |
|---|---|
| [Architecture](docs/architecture.md) | The run loop, the four deterministic paths, the cost model, a shipment's lifecycle |
| [Retailers](docs/retailers.md) | Running a scrape; how each retailer's path works; Costco token setup |
| [Data model](docs/data-model.md) | The 28 columns, the upsert key, column order as a migration, profit accounting (COGS, cashback, gift cards, pro-rata shipping), the year-end tax report |
| [Configuration](docs/configuration.md) | `config.json`, the environment override layer and every variable, warehouse jigs, card cashback rates |
| [Profiles and sign-in](docs/profiles-and-auth.md) | Creating a cloud-browser profile, auto-auth with an authenticator app, where secrets go |
| [Buying groups](docs/buying-groups.md) | Posting tracking to BFMR and MaxOutDeals, insurance filing, payouts and `paid`/`return` |
| [Receipt capture](docs/receipts.md) | Proof of purchase rendered to PDF in your own OCI bucket |
| [Diagnostics](docs/diagnostics.md) | Failure dossiers, preflight, the read-only sheet audit, the offline tests |
| [Importing history](docs/importing-history.md) | `import_history.py` (reconciles the source's profit column before writing), and pasting by hand |
| [Operations](docs/operations.md) | Cron / Task Scheduler, Docker, moving hosts — with [DEPLOY.md](DEPLOY.md) as the server runbook |
| [Roadmap](docs/roadmap.md) | What's next, what's built but still accumulating evidence |

---

## Design notes

The parts worth reading if you're here to look at the engineering rather than to run it:

| Idea | Where | Why it exists |
|---|---|---|
| Deterministic primary, loud failure | `scrapers/<retailer>{,_api,_mapping}.py` | Cost is a per-run tax; silent data loss is unbounded. A normal run spends nothing, and a failure records nothing rather than something wrong — and says so. |
| The failure dossier replaces the agent | `diagnostics/dossier.py`, `CdpBrowser.__exit__` | A paid agent run hid *what* broke. The dossier captures the page at the failure and audits every declared selector against it, so the fix is a code change made from evidence, not a retry that costs money. The agent is opt-in (`AGENT_FALLBACK_ENABLED`), off by default. |
| The schema is a wire format | `models/order.py` `FIELDNAMES`, `sheets/ledger_sync.py` `HEADER` | Rows are written *positionally*. Reordering columns without migrating scrambles every historical row with no error, so a test pins the pairing and the sync refuses to write a mismatched header. |
| Idempotent upsert, blanks never overwrite | `ledger_sync.py` `_merge_row`, `_collapse_records` | Re-checks return partial data. A blank field must never erase a known-good value, and two paths reporting the same row in one sync must collapse rather than clobber. |
| Reconcile on tracking number first | `ledger_sync.py` `sync_csv_to_sheet` | The deterministic path and the agent legitimately disagree about shipment *numbering*. Tracking number is an identity both read identically, so it beats the synthetic key. |
| Undisclosed-split safety net | `ledger_sync.py` | A retailer API that exposes one tracking number per line and rotates it will silently lose a box. An update that changes a non-blank tracking number to a *different* one appends instead of overwriting, and alerts. |
| Audit the live data, not just the code | `scripts/audit_sheet.py` | Tests prove the code; they can't see the sheet. 40+ invariant checks, authenticated **read-only** so it cannot write even by accident. |
| Ask storage before opening a browser | `receipts/capture.py` | Receipt capture runs every scrape, but the existence check comes first — so the common re-check run creates no cloud browser at all, and a browser is only ever paid for by a genuinely new order. |
| Refuse to store a sign-in page | `receipts/sources.py` `looks_logged_out` | A login wall renders and uploads perfectly. Storing one would mark the order as having a receipt *forever*, because the object exists and no later run retries. |
| Catch silent misconfiguration at boot | `scripts/preflight.py`, `docker/healthcheck.sh` | A missing dependency fails three retailers without raising; a dead scheduler produces no signal at all. Both now announce themselves. |

> **A note on `the design notes §N` references.** Code and test comments cite section numbers in `the design notes`, an
> internal engineering journal that records why each of these decisions was made and what live run
> proved it. That file is not published — it contains real order and payout data. The reasoning it
> holds is summarized across these docs; the citations are left in place because they're accurate in
> the private repository the code is developed in.

---

## How it works

Each run, for every configured profile × retailer: read the Sheet to decide what's new versus what
needs re-checking, fetch through that retailer's **deterministic path**, upsert the results back, then
post tracking numbers to the buying groups and read payouts back. **No browser runs locally** —
Playwright is only a CDP *client* to a cloud browser, which is why this runs happily on a Raspberry Pi.

| Retailer | Primary path | Why it's shaped that way |
|---|---|---|
| **Costco** | Private GraphQL API over `curl_cffi` with a stored refresh token | No browser at all. Needs TLS impersonation to pass Costco's fingerprint check. |
| **Best Buy** | Cloud CDP browser → in-page `fetch` of `/profile/ss/api/v1/orders/<id>` | The endpoint is Akamai-guarded, so the read rides the logged-in session cookie *from inside the page* rather than replaying it out-of-band. |
| **Amazon** | CDP browser parsing server-rendered order-details HTML, then a hop to the package-tracking page | A network capture proved there is no order JSON to read. The tracking number lives only on a separate page. |
| **Amazon Business** | Same parser; its own discovery and click-through pagination | Order details are identical to consumer Amazon; only discovery and pagination diverge, so it's a separate scraper that can't regress the consumer one. |

The run flowchart, the cost model and a shipment's lifecycle are in
**[Architecture](docs/architecture.md)**; each retailer's path in detail is in
**[Retailers](docs/retailers.md)**.

---

## Quickstart

Requires Python 3.11+ and a Browser-Use Cloud account with the **Dev tier** (custom proxies need a
paid plan).

```bash
# 1. dependencies
python -m venv .venv
# Windows:  .venv\Scripts\pip install -r requirements.txt
# Linux:    .venv/bin/pip install -r requirements.txt

# 2. config — ONE file (every key is commented in place)
cp config.example.json config.json

# 3. a profile: log into the retailer(s) through its proxy, once
.venv/bin/python -m scripts.create_profile --label profile-1

# 4. check the setup offline, then run
.venv/bin/python -m scripts.preflight
.venv/bin/python main.py                 # or: main.py amazon | amazon-business | bestbuy | costco
```

`config.json` holds everything — credentials, profiles, warehouse jigs, card rates, the Google
service-account key — and is gitignored; environment variables only *override* it
([Configuration](docs/configuration.md)). Costco needs a one-time refresh token
([Retailers → Costco](docs/retailers.md#costco)). The buying-group sync is **off by default** because
it spends real money unattended; turn it on after a dry run and a one-package live test
([Buying groups](docs/buying-groups.md)). Schedule it with cron, Task Scheduler or the self-scheduling
container ([Operations](docs/operations.md)).

Free things worth running often:

```bash
.venv/bin/python -m pytest                          # offline tests, no credentials
.venv/bin/python -m scripts.preflight               # offline config check
.venv/bin/python -m scripts.audit_sheet             # read-only audit of the live sheet
.venv/bin/python -m sync_tracking                   # DRY RUN of the buying-group sync
.venv/bin/python -m scripts.tax_report 2026         # read-only cash-basis year report
```

---

## Project layout

```
main.py                 orchestration + run lock
config/settings.py      config.json-backed settings, env-overridable
config/loader.py        config.json + .state.json access (one file each way)
config/profiles.py      profiles section loader + Sheet order-state reader
config/warehouses.py    warehouses section + address -> buying-group/jig classifier
config/cards.py         cards section + card last-4 -> card name/cashback-rate resolver
models/                 OrderItem + ProfileConfig + Warehouse/Jig + Card schemas
scrapers/base.py        the scrape contract: dossier context, failure handling, (opt-in) agent path
scrapers/<retailer>.py           per-retailer scraper: selectors declared for the audit, agent prompt
scrapers/<retailer>_api.py       deterministic client (CDP browser or GraphQL)
scrapers/<retailer>_mapping.py   pure payload -> OrderItem rows (offline-tested)
scrapers/<retailer>_signin.py    deterministic sign-in for the retailers that need one
scrapers/cdp.py         Playwright-over-CDP browser helper; snapshots the page into a dossier on failure
diagnostics/dossier.py  the failure dossier: page + screenshot + selector audit -> logs/failures/
sheets/ledger_sync.py   Google Sheet upsert (safe partial refresh), sort, formulas, order-state loader
output/csv_writer.py    per-run CSV
alerts/notifier.py      email + Discord alerts
sync_tracking.py        post tracking numbers to buying groups + pull payouts back (dry-run default)
buying_groups/          provider contract + BFMR + MaxOutDeals adapters + Buying Group -> provider registry
receipts/               receipt URL/key rules, OCI store (S3 compat), capture orchestration
scripts/preflight.py    offline check for silent misconfiguration
scripts/audit_sheet.py  read-only audit of the live sheet's invariants (writes nothing)
scripts/tax_report.py   read-only cash-basis tax report for one year (two dates: payout vs order)
scripts/import_history.py  import a foreign spreadsheet of finished orders; reconciles its profit column (dry-run default)
scripts/                create_profile, costco_token, sort_ledger, reorder_sheet, apply_sheet_formats,
                        backfill_receipts, receipt_verify, receipt_probe, bg_probe, install_cron, ...
tests/                  offline pytest suite (no credentials/network needed)
run.sh / run.ps1        scheduler entry points
Dockerfile / docker-compose.yml / docker/   containerized, self-scheduling, with preflight + healthcheck
```

---


## License

Copyright (C) 2026 Alpha

This program is free software: you can redistribute it and/or modify it under the terms of the **GNU
General Public License** as published by the Free Software Foundation, either version 3 of the
License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but **WITHOUT ANY WARRANTY**; without
even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General
Public License for more details.

You should have received a copy of the GNU General Public License along with this program. If not,
see <https://www.gnu.org/licenses/>.

The full text is in [LICENSE](LICENSE).

> **What GPL-3.0 means here, in one line:** anyone may use, study, modify and redistribute this, but a
> distributed derivative must ship its source under the same license. It does not restrict running the
> software, and it places no obligation on you for changes you keep to yourself.

**Nothing in this repository is a credential.** No API key, token, service-account file, session
cookie or account password is committed, and none ever has been — every one of them is gitignored and
injected at runtime. Running this requires your own accounts and your own config files, none of which
are included; see [Quickstart](#quickstart).
