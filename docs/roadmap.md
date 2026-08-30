# Roadmap

_Part of the [Buying Group Ledger](../README.md) docs._

> The source of truth for pending work is `the design notes`, the private engineering journal. This page is a public summary and can lag it.

**Next up**

- **Backfill receipts for rows already on the sheet.** Capture only fires for orders a run actually
  scrapes, and terminal orders are never re-read — so historical rows keep a blank Receipt Link
  until a one-off script walks the sheet and captures them. Most of the machinery already exists
  (`receipts.capture.attach_receipts` takes any list of rows); what's missing is reading the sheet
  and writing the column back.
- **Event-driven re-checks from retailer emails.** Ingest Amazon / Best Buy shipped + delivered +
  order-update emails (Gmail API or IMAP) to trigger a targeted re-check of just that order, instead of
  or alongside the multi-hour poll. Faster status, fewer wasted runs.

**Built and live-validated; still accumulating evidence**

Everything below works end to end against real accounts. What's listed is the *specific state
transition* that hasn't happened to occur yet during a run — these ride real orders, so they close on
their own schedule rather than being work items.

- **Receipt capture end to end.** All four retailers' receipt *pages* are live-verified (see [Receipt capture](receipts.md)), and the storage layer is fully offline-tested — but no receipt has been uploaded to
  a real bucket yet, because that needs OCI credentials. What rides the first configured run: the
  upload itself, the PAR link opening from the sheet, and the re-run proving zero browsers are
  opened. Costco additionally needs its profile signed into costco.com **in a browser** — its data
  path uses a stored token and never opens one, so nothing keeps that session warm.
- **Amazon / Amazon Business split lifecycle** — a single-shipment order splitting at ship time, with
  shipment `1` updating in place while `2`/`3` append, and the order staying open until every shipment
  delivers. Validated on Costco and Best Buy; both Amazons share the code path but haven't yet had an
  order actually split mid-run. The digital-item skip is likewise unexercised on a real digital order.
- **Best Buy cold-start over time** — the deterministic path is validated including a genuine
  logged-out self-login. Still worth watching: the agent fallback firing on a real API outage.
- **Costco refresh-token rotation over many days** of scheduled runs.
- **arm64 / Raspberry Pi.** The container builds and runs correctly on amd64, and a build-time smoke
  test makes a wrong-architecture image fail loudly, but it has not yet run on a real Pi.

**Open questions / smaller items**

- **`scripts/import_history.py` — MAYBE, not needed yet.** A dry-run-default importer for a foreign
  CSV: auto-map its headers onto `FIELDNAMES` (with `--map` overrides), normalise dates to ISO, derive
  `Cost Per Item` from `Total Cost ÷ Quantity`, derive `Shipment` by grouping each order's rows by
  tracking number, run `tag_cards` + `tag_and_filter_personal` so the derived columns fill themselves,
  **refuse non-terminal rows** unless forced, preview update-vs-append counts against the live sheet,
  then sync + sort. The feature that would justify it over hand-pasting: **reconcile its computed
  profit against the source's own profit column and fail on a mismatch** — that's what catches a wrong
  rate or a flipped Insurance sign, which the audit cannot (see [Importing history](importing-history.md)).
  Deferred because a one-off import of ~60 rows is faster to paste than to automate, and the manual
  route has the same protection via a two-minute spot check. Worth building if imports become
  recurring, or if a future import is large enough that hand-checking each row stops being realistic.
- `delivery_date`: the prompts ask for `YYYY-MM-DD`, but the dormant CDP path writes the raw promise
  text ("Arriving Monday") into the same field. No live exposure while that path stays disabled —
  revisit only if it's ever re-enabled.
- **The healthcheck's unhealthy state notifies nobody.** It shows in `docker ps` / `docker inspect`.
  Wiring unhealthy → an alert (a sidecar, or an autoheal container) is the obvious next step if a
  silent scheduler death ever actually happens.

**Known wrinkle.** A *legacy* Amazon row written before the Shipment column existed (blank shipment)
will orphan once if that order later splits: the scraper emits `1`… and the blank row goes stale
and stays perpetually open. Only affects pre-migration rows; clear the test sheet if it shows up.

**Recently done.** Offline test suite (`pytest`); `sync_csv_to_sheet` now derives its row from
`FIELDNAMES` instead of a second hand-maintained copy, with a drift guard test; `status` is normalized
to the known vocabulary (unknown values log a warning and fall back to `ordered` rather than aborting
the run); Best Buy numbers shipments like Amazon instead of copying page wording; both prompts show a
trimmed JOB 2 example so re-checks don't re-fill identity fields.
