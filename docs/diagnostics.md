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

**The alert names the dossier, and where to read it.** It gives the local path and, when
`web.public_url` is set (the dashboard's address as you reach it, e.g. over WireGuard), a link to
the dashboard's Activity page, which renders every dossier in place — the report, the page HTML,
the screenshot. Nothing is uploaded anywhere: dossiers used to be copied to an OCI bucket so an
unreachable host could still hand the alert a link; that went with OCI on 2026-09-18.

The paid Browser-Use agent fallback has been REMOVED entirely (it was flag-gated and off from
2026-08-29 until its removal). The dossier is the failure path; there is no paid retry.
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
wrong order is the one failure that scrambles every row with no error — see the column-order warning in [Data model](data-model.md)); no duplicate upsert keys, on all four of the keys `sync_csv_to_sheet` uses (a package id under two Shipment numbers of one order is the re-tracked-shipment double-count); every row's
`Total Profit` still holds the *live formula* rather than a number frozen from a past read, and that
formula still points at the current columns; and that the cell **types** are intact — `Shipment` an
int, `Card Last 4` text with its leading zeros, the money columns numeric rather than `"$1,299.00"`
text, and the date columns plain ISO text.

That last one is the one to care about. **`Order Date` is part of the upsert key**, so if the date
columns are ever re-formatted as real Dates, a row without a tracking number will append a duplicate
on its next re-check. A more general check catches the same class of bug for any key column: it
builds each row's key twice — once from the displayed text and once from the stored value — and
fails if they differ, i.e. if a row's identity depends on how you happen to have formatted it.

**`state_visibility`** runs the same classifier a scrape starts with (`ledger_sync.classify_order_state`)
once per configured profile × retailer and reports what each run would see — terminal / open /
needs-a-re-read counts, the last being the next run's browser bill — and then the finding no other
check makes: rows whose Profile + Retailer match **no configured run** and so can never be re-checked
or closed (FAIL for a retailer a scraper exists for; INFO for a hand-entered one such as Newegg).

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
positionally, so drift misaligns every row), the blank-preserving upsert, the delivered rollup,
status normalization, and the selector-declaration tripwires that the dossier audit depends on. Behavior
that only a real page can prove — selector accuracy, whether an order actually splits — still needs a
live run.
