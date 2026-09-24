# Diagnostics: Dossiers, Preflight, the Audit, Tests and the Activity Log

_Part of the [Buying Group Ledger](../README.md) docs._

A broken scrape leaves a **failure dossier**; **preflight** catches misconfiguration that keeps
running while doing the wrong thing; the **ledger audit** catches wrong data; the **offline tests**
catch wrong code; the **activity log** records what everything did.

## Failure Dossiers

A deterministic-path failure never guesses. It records nothing for that retailer that run, writes a
dossier, and alerts with its path. The next scheduled run retries.

```
logs/failures/<retailer>_<profile>_<timestamp>/
  report.md       exception + traceback, a timeline of what the path was doing, and a SELECTOR
                  AUDIT: every selector the parser depends on, its match count on the captured
                  page, and a sample of the text. A 0 where there used to be a hit is the fix.
  page_N.html     the DOM at the moment of failure (secrets + common PII patterns redacted)
  page_N.png      what it looked like
  response_N.txt  the API request/response, for Costco's no-browser path
```

- **Soft problems**: a scrape that succeeds but couldn't read part of a page (a tracking page, an
  order page that didn't load, a Best Buy detail fetch that wasn't 200, an unreadable cell) ends
  with a dossier and a "completed with N problem(s)" alert.
- **Sign-in failures** the page names (wrong password, captcha, anti-bot, identity challenge) alert
  without a dossier; an unrecognised one captures the page.
- **Selectors are declared** (`<mapping>.SELECTORS` plus `diagnostic_selectors`). A test fails if a
  `select("...")` literal in a `_mapping.py` is undeclared, so none escapes the audit.
- **Reading it**: the alert gives the local path and, with `web.public_url` set, a link to the
  dashboard's Activity page, which renders every dossier. Its *download* button gives the whole
  dossier as one zip: hand that, with the retailer's `_mapping.py` / `_api.py`, to whoever fixes
  it. The captured page becomes the test fixture for the fix. There is no paid retry.
- **Privacy**: the newest 40 are kept under `logs/` (gitignored). They are redacted (secrets,
  emails, phones, card digits) but can hold names and addresses. **Never commit one.**

## What a Capture Must Read

A page or payload that stops yielding a cell is a **shape change**, and always leaves a dossier:

- **Identity cells raise.** A blank Order ID, Order Date or Item Name (the upsert key) raises the
  mapping's shape error (`OrderPageShapeError` / `PayloadShapeError`); nothing is recorded for that
  retailer that run. So does an Amazon page with no order id or with items but no shipment cards,
  and a Best Buy or Costco payload with no order date.
- **Other mandatory cells are reported.** An unreadable Quantity, Cost Per Item, Delivery Address or
  Card Last 4 (`models.order.CAPTURE_MANDATORY_FIELDS`) is recorded blank and reported as a
  `diagnostics.problem` naming the order, item and **the selector or JSON path** it comes from
  (the mapping's `FIELD_SOURCES`). An unparsed Amazon order summary is reported the same way.

No silent defaults: an Amazon quantity element with no digit is unread, not 1, and a Costco line
with no readable price has a blank cost, not $0.00. Exempt: money on cancelled or superseded rows,
the address of a gift card, a Quantity of `*` (an undisclosed split). The audit's
`mandatory_by_stage` demands the same cells (`tests/test_capture_mandatory.py` pins it).

## Preflight

```bash
python -m scripts.preflight                                        # offline, free
docker compose run --rm --entrypoint python ledger -m scripts.preflight   # in the container
```

It checks what otherwise **fails silently**: a missing deterministic-path dependency; a bind mount
whose missing host file became an empty directory; `BROWSER_USE_API_KEY` unset or no alert channel
on; a Costco token missing with no `auth.costco` to mint one; a profile that can never run or can't
sign itself in again; the buying-group sync on (spends money), or the BFMR auto-reply on without its
own mailbox; an unwritable receipts folder or missing `pypdf`; a run interval that overspends
MaxOutDeals' daily quota.

Exit `1` on a failure (`--strict`: a warning too). Every container start runs it with `--alert` and
**continues** on failure, since a container that won't start also stops scraping;
`container.preflight_strict` (`PREFLIGHT_STRICT=true`) refuses to start instead. It can't verify the
MaxOutDeals IP allowlist; the sync alerts when the IP is unregistered.

## Auditing the Ledger

`scripts/audit_ledger.py` runs 36 checks over `data/ledger.sqlite3` and **writes nothing**: it opens
the file through `open_ledger_readonly`, which refuses every write. The dashboard's Audit page runs
the same checks and shows the findings by row.

```bash
python -m scripts.audit_ledger                   # Windows venv: .venv\Scripts\python -m ...
python -m scripts.audit_ledger --expect-rows 23  # also assert the row count
python -m scripts.audit_ledger --stale-days 2    # open rows not re-scraped in 2 days (default 3)
```

- **Schema**: the header matches `HEADER` **exactly** (right names in the wrong order scramble
  every row silently); nothing outside the schema's columns.
- **Keys**: no duplicates on any of the four keys the upsert matches on, no blank Order IDs, no
  stray whitespace or newlines in key cells, contiguous shipment numbers.
- **Types**: whole-number Shipment and Quantity, Card Last 4 as text with its leading zeros, numeric
  money columns, ISO-text dates. `Order Date` is in the upsert key, so any other spelling appends a
  duplicate on the next re-check.
- **Money**: Total Cost = Quantity × Cost Per Item; shipping and payouts cost-weighted; no money on
  cancelled or superseded rows; paid rows paid; returns consistent; rates within 0–1; COGS inputs
  complete.
- **Stage** (`mandatory_by_stage`): identity on every row; cost inputs, Profile, Order Link,
  Delivery Address, Card, Card Last 4, COGS and Buying Group on every row with money; from shipped,
  a tracking number with Tracking Submitted ticked; delivered adds its date and receipt; paid adds
  Actual Payout, Payout Date and Insurance; return adds Return Qty and Return Date. A cell too early
  for its stage is a stale-status warning. `impossible_values` catches negative or oversized
  amounts, impossible dates and ids or links that don't fit their retailer.
- **Staleness**: open orders no longer re-scraped (stuck, or the scheduler stopped).

**Before and after a live run**, the diff proves rows were updated, not duplicated:

```bash
python -m scripts.audit_ledger --save-snapshot before.json
python main.py
python -m scripts.audit_ledger --compare before.json
```

| `--compare` finds | Level |
|---|---|
| a removed row (no run deletes rows) | FAIL |
| an appended row reusing a tracking number of the same order | FAIL |
| an edited key cell (the row is orphaned for re-checks) | FAIL |
| a status moving backwards | FAIL |
| a scraped cost changing on a terminal row | WARN |
| a new order; a row retired as superseded | INFO |

These count toward the exit code, so `--compare before.json --strict` can gate a sweep.
`Last Scraped At` is ignored. `--save-snapshot` / `--from-snapshot FILE` dump and re-audit the raw
grids offline; a bare filename lands under `data/` (gitignored: a snapshot holds addresses and card
digits). `--json` for machine-readable output, `-v` for detail on passing checks.

**Exit codes**: `0` nothing failed; `1` a failure (or a warning under `--strict`); `2` the audit could
not run. Never read `2` as a green light.

## Tests

```bash
.venv/bin/pip install -r requirements.txt -r requirements-web.txt -r requirements-dev.txt
.venv/bin/python -m pytest            # Windows: .venv\Scripts\python -m pytest
```

About 2,600 tests, offline and free: no credentials, no network, no cloud browser. They cover what
fails silently: `FIELDNAMES`/`HEADER` drift, the blank-preserving upsert, status handling, the
capture gate and the selector-declaration tripwires. `pytest` isn't in the Docker image, so run
them on a development machine.

`tests/test_browser_grid.py` drives the machine's own Chrome through Playwright and skips where
there is none; run it separately on a PC with Chrome (`python -m pytest tests/test_browser_grid.py`).
Selector accuracy on real pages still needs a live run.

## The Activity Log

`logs/activity.jsonl` (`diagnostics/activity.py`) is the app's own record of what it **did**, one
JSON line per event, written at the source: runs, scrapes, ledger writes, sync outcomes, auto-reply
emails, alerts, dossiers, spend-limit warnings, imports, backups, tool runs, container health, and
every dashboard change (edits, added and deleted rows, receipts, settings, sign-ins,
acknowledgements). A run's events share a `run_id`; a failed write never fails the run. The
dashboard's **Activity** page filters it, opens dossiers, and acknowledges alerts. A new step that
changes something should call `activity.record`.
