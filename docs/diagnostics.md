# Diagnostics — dossiers, preflight, the audit, and tests

_Part of the [Buying Group Ledger](../README.md) docs._

Four things stand between a silent failure and a noticed one. In the order you meet them: the
**failure dossier** a broken scrape leaves behind, **preflight** for misconfiguration that keeps
running while doing the wrong thing, the **ledger audit** for data that is wrong in the ledger
itself, and the **offline tests** for the code.

## Failure dossiers

A deterministic-path failure never guesses. It writes a dossier and alerts with its path:

```
logs/failures/<retailer>_<profile>_<timestamp>/
  report.md       exception + traceback, a timeline of what the path was doing, and a SELECTOR
                  AUDIT: every selector the parser depends on, how many matches it got on the
                  captured page, and a sample of the text — a 0 where there used to be a hit is
                  the fix
  page_N.html     the DOM at the moment of failure (secrets + common PII patterns redacted)
  page_N.png      what it looked like
  response_N.txt  the API request/response, for the no-browser paths (Costco GraphQL)
```

The alert names the dossier path. Hand the directory to a coding agent with the retailer's
`_mapping.py` / `_api.py`; the captured HTML becomes the test fixture that proves the fix offline.
Nothing is recorded for that retailer that run, and the next scheduled run retries. A scrape that
*succeeds* but could not read part of a page (a tracking page whose selectors stopped matching, an
order-details page that failed to load) also leaves a dossier and alerts, because those used to be
silent. Login failures never ran the agent and still don't; their alerts now point at a dossier too.

Two things make the selector audit trustworthy. Every scraper **declares** its selectors
(`<mapping>.SELECTORS` plus the sign-in and tracking-page selectors, as `diagnostic_selectors`), and
a test scans each `_mapping.py` for `select("...")` literals and fails if one is missing from the
declaration — so a new selector cannot escape the audit. Costco has no browser on its data path;
the one page it can capture is the B2C sign-in screen the token grab drives, and those are the
selectors it declares.

**Soft problems** are reported too. A scrape that *succeeds* but could not read part of a page — a
tracking page whose selectors stopped matching, an order-details page that failed to load, a Best Buy
detail fetch that came back non-200 — ends with a dossier and a "completed with N problem(s)" alert,
because each of those used to be silent. Sign-in failures never ran the agent and still don't; their
alerts point at a dossier as well, including a Costco sign-in that fails inside the token grab.

Dossiers are **redacted** (configured secrets, emails, phones, card-digit phrases) but can still hold
names and addresses. They live under `logs/` (gitignored) and keep the newest 40. Do not commit one.

**The alert names the dossier, and where to read it.** It gives the local path and, when
`web.public_url` is set (the dashboard's address as you reach it, e.g. over WireGuard), a link to
the dashboard's Activity page, which renders every dossier in place — the report, the page HTML,
the screenshot. Nothing is uploaded anywhere: dossiers used to be copied to an OCI bucket so an
unreachable host could still hand the alert a link; that went with OCI on 2026-09-18.

The paid Browser-Use agent fallback has been REMOVED entirely (it was flag-gated and off from
2026-08-29 until its removal). The dossier is the failure path; there is no paid retry.
Since 2026-08-29 the answer to a broken selector is a fix made from the dossier, not a paid run.

## What a capture must read

A page or payload that stopped yielding a cell it used to yield is a **shape change** -- the thing
this app breaks on -- and since 2026-09-19 it always leaves a dossier, in one of two ways:

- **Identity cells raise.** Order ID, Order Date and Item Name are the upsert key; a blank one
  cannot be recorded at all, so the mapping raises its shape error (`OrderPageShapeError` /
  `PayloadShapeError`): nothing is recorded for that retailer that run, the dossier holds the page
  or payload, the alert names it. Same for an Amazon order page with no order id at all (a sign-in
  bounce or an error page, which used to parse as "no rows" in silence), one with items but no
  shipment cards, and a Best Buy / Costco payload with no order date.
- **The other mandatory cells are reported.** Quantity, Cost Per Item, Delivery Address and Card
  Last 4 (`models.order.CAPTURE_MANDATORY_FIELDS`) are recorded blank when unreadable -- a blank
  never overwrites, and the next re-read of an open order may fill the cell -- but every blank is a
  `diagnostics.problem` naming the order, the shipment, the item and **the selector or JSON path the
  cell is read from** (each mapping's `FIELD_SOURCES`), with that order's page or payload attached.
  The run then ends with the "completed with N problem(s)" alert and dossier. An unparsed Amazon
  order summary (Shipping / Sales Tax / Gift Card / Rewards Used) is reported the same way.

Two former defaults are gone: an Amazon quantity element that is present but holds no digit is
unread, not 1 (the element is simply absent on a qty-1 line, and that still means 1), and a Costco
line with no readable price has a blank cost, not $0.00.

Exemptions are deliberate and few: a cancelled or superseded row carries no money; a gift-card row
has no package, so no address; a Quantity of `*` is the undisclosed-split marker. The ledger audit's
`mandatory_by_stage` demands the same cells under the same names afterwards --
`tests/test_capture_mandatory.py` pins that the two lists agree -- so a gap the run reports is the
gap the Audit page would show, caught while the page is still in hand.

## Preflight

```bash
python -m scripts.preflight                                        # offline, free
docker compose run --rm --entrypoint python ledger -m scripts.preflight   # in the container
```

It checks the misconfigurations that otherwise **fail silently**: a missing deterministic-path
dependency (which would fail three retailers on every run without raising), a bind mount whose
missing host file became an empty directory, a Costco token that is absent with no way to mint one,
a profile that is configured but can never run, a partially configured receipt bucket, an interval
that overspends MaxOutDeals' daily quota. Every container start runs it and **alerts and continues**
— a container that refuses to start also stops scraping; `PREFLIGHT_STRICT=true` fails fast instead.
It cannot verify the MaxOutDeals IP allowlist from the host; the sync's own push alerts when the
IP is unregistered.

## Handing a dossier to whoever fixes it

The Activity page's *download* button on a dossier row is the whole dossier as one zip -- the
report (selector audit, traceback), the captured pages and the screenshots -- which is what to
upload to an AI coding agent, or send to a person, to fix the selector that broke. The dossier is
redacted but may still carry names and addresses: never commit one.

## Auditing the ledger

`pytest` proves the *code* is right; it can't see the data. `scripts/audit_ledger.py` checks the
ledger itself (`data/ledger.sqlite3`) against every invariant it depends on, and **writes nothing,
ever** — it opens the file through `open_ledger_readonly`, a worksheet handle that refuses every
write, so it isn't merely well-behaved: it isn't permitted to write. The dashboard's Audit page runs
the same checks and shows the findings by row.

```bash
.venv/bin/python -m scripts.audit_ledger                  # Windows: .venv\Scripts\python -m ...
.venv/bin/python -m scripts.audit_ledger --expect-rows 23 # also assert the row count
```

It's worth running **before and after** a live run — the diff is what proves a run updated rows
instead of duplicating them, and `--compare` does that for you:

```bash
.venv/bin/python -m scripts.audit_ledger --save-snapshot before.json
.venv/bin/python main.py
.venv/bin/python -m scripts.audit_ledger --compare before.json
```

That reports added / removed / changed rows keyed on the upsert key — and **judges** them, as
`compare_*` results that count toward `--strict` and the exit code: a removed row is a FAIL (nothing
in the system deletes rows); an appended row that reuses a tracking number an existing row of the same
order already has is a FAIL (the split-order duplicate); a key cell edited by hand is a FAIL (the row
is orphaned for future re-checks); a status moving backwards is a FAIL; a scraped cost changing on a
terminal row is a WARN (no scraper re-reads those); a genuinely new order is INFO `compare_appended`;
everything else is the normal `compare_updated`. So a cutover or a historical sweep can be gated by
`--compare before.json --strict` instead of by reading a diff.
(`Last Scraped At` is ignored — it changes on every touch and would otherwise mark every row as
changed.) Exit code is `0` when nothing failed, `1` on a failure (or on a warning under `--strict`),
so it can gate a scheduled run.

What it checks, and why each one matters: the header matches `HEADER` **exactly** (right names in the
wrong order is the one failure that scrambles every row with no error — see the column-order warning in [Data model](data-model.md)); no duplicate upsert keys, on all four of the keys `sync_csv_to_ledger` uses (a package id under two Shipment numbers of one order is the re-tracked-shipment double-count); the money invariants (Total Cost = Quantity × Cost Per Item, shipping and payouts cost-weighted across an order, a cancelled or superseded row carrying no money, a paid row having a payout, the return columns agreeing); the mandatory cells (a Status, an Order ID, tracking on a shipped row); and that the cell **types** are intact — `Shipment` an
int, `Card Last 4` text with its leading zeros, the money columns numeric rather than `"$1,299.00"`
text, and the date columns plain ISO text.

That last one is the one to care about. **`Order Date` is part of the upsert key**, so a date stored
in any spelling other than `YYYY-MM-DD` is a different key, and the row appends a duplicate on its
next re-check.

It also checks the things that go wrong *around* the data rather than in it: content outside the
schema's columns, a cashback rate outside 0–1, and
**open orders that stopped being re-scraped** — the failure nobody notices, whether that's an order
stuck open forever or the scheduler silently not running. Tune that last one with `--stale-days`.

(The checks that only a Google Sheet could fail — formula coverage, stray formulas, merged cells,
the displayed-versus-stored disagreement — went with the Sheet on 2026-09-18.)

Two flags make it free to iterate on: `--save-snapshot FILE` dumps the raw grids, and
`--from-snapshot FILE` re-audits that dump offline with no credentials and no API calls. A bare
filename lands under `data/` (gitignored) for all three snapshot flags, because a snapshot holds
delivery addresses and card digits; give a path with a directory to put it elsewhere. `--json`
emits the same results machine-readably, `--compare` included.

Exit code `2` means the audit could not run at all — never read that as a green light.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest            # Windows: .venv\Scripts\python -m pytest
```

Fully offline and free — no credentials, no network, no Browser-Use run. They cover the parts that
fail *silently* rather than loudly: column drift between `FIELDNAMES`/`HEADER` (rows are written
positionally, so drift misaligns every row), the blank-preserving upsert, the delivered rollup,
status normalization, and the selector-declaration tripwires that the dossier audit depends on. Behavior
that only a real page can prove — selector accuracy, whether an order actually splits — still needs a
live run.
