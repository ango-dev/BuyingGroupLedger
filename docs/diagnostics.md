# Diagnostics — dossiers, preflight, the audit, and tests

_Part of the [Buying Group Ledger](../README.md) docs._

Four things stand between a silent failure and a noticed one. In the order you meet them: the
**failure dossier** a broken scrape leaves behind, **preflight** for misconfiguration that keeps
running while doing the wrong thing, the **sheet audit** for data that is wrong on the live sheet,
and the **offline tests** for the code.

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

**The alert carries a link, not just a path.** When the receipt bucket is configured, each dossier is
also uploaded under `failures/<retailer>_<profile>_<timestamp>/` in the same bucket — pages and
responses first, then `report.md` with a "Hosted copies" section linking to them — and the alert
reads `Failure dossier: <link to report.md>` with the local path underneath. One paste hands a
coding agent the report, and the report hands it the page.

The links use **their own PAR**, separate from the receipts one: in the OCI console create a
Pre-Authenticated Request with Target **Objects with prefix** `failures/`, **Permit object reads**,
listing **off**, far-future expiry, and paste its URL as given (ending `/o/failures/`) into
`receipts.oci.failures_par_url_prefix` (`OCI_FAILURES_PAR_URL_PREFIX`). Blank = dossiers are not
uploaded and preflight says so. Two PARs means the two can be revoked independently and the receipt
PAR stays scoped to receipts. Same PII class as receipts, same rules — private bucket, no listing,
and **a 30-day lifecycle rule on the `failures/` prefix** so it does not accumulate.
`DOSSIER_UPLOAD_ENABLED=false` turns it off (do that if an alert channel is shared with people who
should not see order pages). The upload is best-effort: if storage fails, the alert falls back to
the local path.

The paid Browser-Use agent still exists behind `AGENT_FALLBACK_ENABLED` (default `false`) and a
per-retailer `*_FORCE_AGENT` hook. With it on, the dossier is still written and *then* the agent runs.
Since 2026-08-29 the answer to a broken selector is a fix made from the dossier, not a paid run.

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
It cannot verify the MaxOutDeals IP allowlist from the host, so it warns about that every time.

## Auditing the sheet

`pytest` proves the *code* is right; it can't see the live sheet. `scripts/audit_sheet.py` checks the
sheet itself against every invariant the ledger depends on, and **writes nothing, ever** (it
authenticates with a read-only scope, so it isn't merely well-behaved — it isn't permitted to write):

```bash
.venv/bin/python -m scripts.audit_sheet                  # Windows: .venv\Scripts\python -m ...
.venv/bin/python -m scripts.audit_sheet --expect-rows 23 # also assert the row count
```

It's worth running **before and after** a live run — the diff is what proves a run updated rows
instead of duplicating them, and `--compare` does that for you:

```bash
.venv/bin/python -m scripts.audit_sheet --save-snapshot before.json
.venv/bin/python main.py
.venv/bin/python -m scripts.audit_sheet --compare before.json
```

That reports added / removed / changed rows keyed on the upsert key, so a run that appended a
genuinely new order is immediately distinguishable from one that duplicated an existing row.
(`Last Scraped At` is ignored — it changes on every touch and would otherwise mark every row as
changed.) Exit code is `0` when nothing failed, `1` on a failure (or on a warning under `--strict`),
so it can gate a scheduled run.

What it checks, and why each one matters: the header matches `HEADER` **exactly** (right names in the
wrong order is the one failure that scrambles every row with no error — see the column-order warning in [Data model](data-model.md)); no duplicate upsert keys, on all three of the keys `sync_csv_to_sheet` uses; every row's
`Total Profit` still holds the *live formula* rather than a number frozen from a past read, and that
formula still points at the current columns; and that the cell **types** are intact — `Shipment` an
int, `Card Last 4` text with its leading zeros, the money columns numeric rather than `"$1,299.00"`
text, and the date columns plain ISO text.

That last one is the one to care about. **`Order Date` is part of the upsert key**, so if the date
columns are ever re-formatted as real Dates, a row without a tracking number will append a duplicate
on its next re-check. A more general check catches the same class of bug for any key column: it
builds each row's key twice — once from the displayed text and once from the stored value — and
fails if they differ, i.e. if a row's identity depends on how you happen to have formatted it.

It also checks the things that go wrong *around* the data rather than in it: content outside the
28-column block (a stray note below the data misplaces the next appended row), `#REF!` errors left by
a deleted column, a cashback rate outside 0–1, merged cells (they blank their neighbours on read), and
**open orders that stopped being re-scraped** — the failure nobody notices, whether that's an order
stuck open forever or the scheduler silently not running. Tune that last one with `--stale-days`.

Two flags make it free to iterate on: `--save-snapshot FILE` dumps the raw sheet, and
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
positionally, so drift misaligns every row), the blank-preserving upsert, the delivered rollup, agent
JSON parsing, status normalization, and prompt instructions that data integrity depends on. Behavior
that only a real page can prove — selector accuracy, whether an order actually splits — still needs a
live run.
