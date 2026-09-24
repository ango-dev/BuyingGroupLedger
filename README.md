# Buying Group Ledger

Automated order tracking, reconciliation and payout settlement for buying-group reselling. It reads
order history from Amazon, Amazon Business, Best Buy and Costco into a ledger (a SQLite file with a
web dashboard over it) kept current as a live P&L, posts shipped tracking numbers to two
buying-group APIs, files shipment insurance and reads payouts back, unattended, on a schedule.

**The design problem is cost against reliability.** An LLM agent driving a browser can read any
order page but bills on every run; a hand-written scraper is free but breaks *silently*, and a
silently missed order is missed reimbursement. So every retailer has a **deterministic, agent-free
path** that **fails loudly**: it records nothing and writes a failure dossier (traceback, page HTML,
screenshot, and an audit of every selector the parser depends on) to `logs/failures/`, which the
alert names. Normal runs cost nothing; a site change costs one missed run and a fix made from
evidence. Most of the rest is invariants, idempotency and auditing aimed at silent failures; see
[Design Notes](#design-notes).

> **Status:** running in production against real accounts. `pytest` runs 2,500+ offline tests that
> need no credentials and no network. US retailers and US taxes only (amazon.com, bestbuy.com,
> costco.com; the tax report is a Schedule C).

---

## Documentation

| Page | What It Covers |
|---|---|
| [Getting Started](docs/getting-started.md) | A first install, step by step, and a safe first run |
| [Architecture](docs/architecture.md) | The run loop, the cost model, a shipment's lifecycle |
| [Retailers](docs/retailers.md) | Running a scrape; each retailer's path; Costco token setup |
| [Data Model](docs/data-model.md) | The 36 columns, the upsert key, profit accounting, the tax report |
| [Configuration](docs/configuration.md) | `config.json`, environment overrides, warehouse jigs, card rates |
| [Profiles and Sign-In](docs/profiles-and-auth.md) | Cloud-browser profiles, unattended sign-in |
| [Buying Groups](docs/buying-groups.md) | Posting tracking to BFMR and MaxOutDeals, insurance, payouts |
| [Receipt Capture](docs/receipts.md) | Proof of purchase as PDFs kept beside the ledger |
| [Diagnostics](docs/diagnostics.md) | Failure dossiers, preflight, the ledger audit, the tests |
| [Importing History](docs/importing-history.md) | Bringing in orders from another spreadsheet |
| [Operations](docs/operations.md) | Scheduling, Docker, the dashboard, backups ([DEPLOY.md](DEPLOY.md): the server runbook) |
| [Roadmap](docs/roadmap.md) | What's next |

Before exposing the dashboard, read [SECURITY.md](SECURITY.md). Issues and patches:
[CONTRIBUTING.md](CONTRIBUTING.md).

---

## Design Notes

The parts worth reading for the engineering:

| Idea | Where | Why It Exists |
|---|---|---|
| Deterministic primary, loud failure | `scrapers/<retailer>{,_api,_mapping}.py` | Cost is a per-run tax; silent data loss is unbounded. A failure records nothing, and says so. |
| The failure dossier is the failure path | `diagnostics/dossier.py`, `CdpBrowser.__exit__` | It captures the page and audits every declared selector against it, so the fix comes from evidence, not a paid retry. |
| The schema is a wire format | `models/order.py` `FIELDNAMES`, `ledger/sync.py` `HEADER` | Rows are written positionally; an unmigrated reorder would scramble every row silently. A test pins the pairing. |
| Idempotent upsert, blanks never overwrite | `ledger/sync.py` `_merge_row`, `_collapse_records` | Re-checks return partial data: a blank never erases a known value, and two reports of one row collapse. |
| Reconcile on tracking number first | `ledger/sync.py` `sync_csv_to_ledger` | Writers can disagree on shipment numbering; a tracking number cannot, so it beats the synthetic key. |
| Undisclosed-split safety net | `ledger/sync.py` | A changed non-blank tracking number appends a row and alerts, so a rotating per-line number cannot silently lose a box. |
| The ledger is a file behind a worksheet face | `ledger_db/worksheet.py`, `ledger_db/store.py` | SQLite implements the few worksheet methods every writer uses. It migrates by column *name* and refuses to drop an unknown column. |
| A hand-typed cell is protected | `web/ledger_writer.py`, `ledger_db/hand_edits.py` | Cells typed on the dashboard are recorded, and no run-time writer touches them until cleared or released. |
| Audit the live data, not just the code | `scripts/audit_ledger.py` | Tests can't see the data: 36 invariant checks, through a **read-only** handle. |
| Ask storage before opening a browser | `receipts/capture.py` | The existence check comes first, so only a genuinely new order pays for a cloud browser. |
| Refuse to store a sign-in page | `receipts/sources.py` `looks_logged_out` | A login wall renders to a fine PDF, and a stored receipt is never retried. |
| Catch silent misconfiguration at boot | `scripts/preflight.py`, `docker/healthcheck.sh` | A missing dependency or a dead scheduler otherwise fails without a signal. |
| Guard the dashboard | `web/guard.py`, `web/auth.py` | It holds every credential: unknown host names and cross-site writes are refused; reachable with no password, it only offers to set one. |




---

## How It Works

Each run, for every profile × retailer: decide from the ledger what is new or needs re-checking,
fetch it, upsert it, then post tracking to the buying groups and read payouts back. **No browser
runs locally**: Playwright is only a CDP client to a cloud browser, so this runs happily on a
Raspberry Pi.

| Retailer | Primary Path | Why It's Shaped That Way |
|---|---|---|
| **Costco** | Private GraphQL API over `curl_cffi` with a stored refresh token | No browser; TLS impersonation passes the fingerprint check. |
| **Best Buy** | Cloud CDP browser → in-page `fetch` of `/profile/ss/api/v1/orders/<id>` | Akamai-guarded, so the read runs inside the signed-in page. |
| **Amazon** | CDP browser parsing order-details HTML, then the package-tracking page | No order JSON exists; tracking lives on its own page. |
| **Amazon Business** | The same parser; its own discovery and pagination | Kept separate so it can't regress the consumer scraper. |

Details: [Architecture](docs/architecture.md), [Retailers](docs/retailers.md).

---

## Quickstart

### What You Need

- **Python 3.12+**, or Docker.
- A **Browser-Use Cloud** account on a paid tier (custom proxies need one) and one static ISP proxy
  per profile. Retailer sign-ins run in that cloud browser.
- The **retailer accounts** you buy on, with the authenticator app's seed where a site asks for
  2-step ([Profiles and Sign-In](docs/profiles-and-auth.md)); Costco also needs a one-time refresh
  token ([Retailers → Costco](docs/retailers.md#costco)).
- Optionally, **buying-group API access** (a BFMR key and secret, a MaxOutDeals token; MaxOutDeals
  allowlists your server's IP) and somewhere for **alerts** (a Gmail app password, a Discord webhook).

### With Docker

```bash
git clone https://github.com/ango-dev/BuyingGroupLedger.git && cd BuyingGroupLedger
touch config.json .state.json && chmod 600 config.json .state.json   # empty: the setup wizard fills them
docker compose up -d --build
# open http://127.0.0.1:8765 -- a fresh install lands on the setup wizard
```

The wizard saves everything into `config.json`; the container then runs on its own schedule.
[DEPLOY.md](DEPLOY.md) is the full server runbook.

### Without Docker

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-web.txt -r requirements-dev.txt   # Windows: .venv\Scripts\pip
cp config.example.json config.json                              # every key commented; or `python -m web` and its wizard
.venv/bin/python -m scripts.create_profile --label profile-1    # log the profile in, once
.venv/bin/python -m scripts.preflight                           # offline config check
.venv/bin/python main.py costco                                 # or amazon | amazon-business | bestbuy; none = all
```

[Getting Started](docs/getting-started.md) has the detail, a safe first run and scheduling. The
buying-group sync is **off by default** because it spends real money unattended;
`python -m sync_tracking` is its free dry run ([Buying Groups](docs/buying-groups.md)).

---

## Project Layout

```
main.py                 orchestration + run lock
config/                 settings (config.json-backed, env-overridable), loader, profiles / warehouses / cards
models/                 OrderItem, ProfileConfig, Warehouse/Jig, Card schemas
scrapers/               base.py (the scrape contract), cdp.py (Playwright over CDP, dossier on failure), and
                        per retailer: <r>.py, <r>_api.py (deterministic client), <r>_mapping.py (pure
                        payload -> rows, offline-tested), <r>_signin.py where sign-in is needed
diagnostics/            dossier.py (the failure dossier -> logs/failures/), activity.py (the activity log)
ledger/sync.py          the ledger upsert (safe partial refresh), sort, order-state loader
ledger_db/              store.py (SQLite, self-migrating by column name), worksheet.py (the adapter every
                        reader and writer uses), hand_edits.py
web/                    the dashboard: Overview, Orders, Activity, Audit, Recon, Taxes, Tools, Settings
web/guard.py            host check, cross-site write refusal, first-password gate
alerts/notifier.py      email + Discord alerts
sync_tracking.py        post tracking to buying groups + read payouts back (dry-run default)
respond_bfmr.py         answer BFMR's combined-package emails
buying_groups/          provider contract, BFMR + MaxOutDeals adapters, Buying Group -> provider registry
receipts/               receipt rules, the file store, capture
scripts/                preflight, audit_ledger, tax_report, backup, import_history, create_profile,
                        costco_token, backfill_*, restore_cells, sort_ledger, hand_edits, bg_probe, ...
tests/                  offline pytest suite
run.sh / run.ps1, docker/, Dockerfile, docker-compose.yml   scheduler entry points, the container
```

---


## License

Copyright (C) 2026 ango-dev

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

## Disclaimer

This project is not affiliated with, endorsed by or supported by Amazon, Best Buy, Costco, BFMR,
MaxOutDeals or Browser-Use. It signs in to your retailer accounts and reads their order pages and
private APIs automatically, which a retailer's terms of use may not allow; you are responsible for how
you use it, and a retailer may limit or close an account it believes is automated. With the
buying-group sync on, it submits tracking numbers and **buys shipment insurance with real money,
unattended**, and those submissions cannot be taken back. The tax report is arithmetic over your
ledger, not tax advice. It comes with no warranty (see the license above). Read
[SECURITY.md](SECURITY.md) before exposing the dashboard beyond your own machine.

**Nothing in this repository is a credential.** No API key, token, session cookie or account
password is committed, and none ever has been: every one is gitignored and supplied at runtime.
Running this requires your own accounts and config files, none of which are included; see
[Quickstart](#quickstart).
