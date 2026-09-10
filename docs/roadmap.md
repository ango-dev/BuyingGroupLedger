# Roadmap

_Part of the [Buying Group Ledger](../README.md) docs._

> The source of truth for pending work is `the design notes` in the private repository; this page is the
> public summary and can lag it.

**Where things stand (2026-09).** Every retailer runs on a deterministic, agent-free path
(Amazon and Amazon Business parse the order pages; Best Buy reads its order API through a cloud
browser; Costco reads its GraphQL API with a stored token). The buying-group sync posts tracking,
files insurance and reads payouts back unattended. Receipts are captured to object storage with a
capture-time guard that refuses a page which does not name its own order. The sheet audit is a
read-only script with 40 checks. Failures write a dossier — page, screenshot, selector audit —
instead of running a paid agent (retired 2026-08-29).

**Worth building, none urgent**

- **A Tax Summary tab.** Two `SUMIFS` over two *different* date columns: receipts count on
  `Payout Date`, costs on `Order Date`, so a December order paid in January belongs to different
  years on the two sides. Insurance sums separately as a Schedule C expense, never inside COGS.
  `scripts/tax_report.py` already computes all of it read-only.
- **Fail loudly on a same-key collision from one order's parse.** Three silent row losses came
  from the sync's same-key collapse; every known page shape is now handled in the mappings, so a
  residual collision is an unknown shape and should end the run with a dossier problem.
- **Auditor hardening leftovers.** A display-round-trip check across every numeric column,
  cross-row order-level consistency, a locale-proof formula compare, profit recomputed in Python,
  strict ISO date parsing.
- **Docker unhealthy → notify.** An unhealthy container currently shows only in `docker ps`.
- **Best Buy rewards** into the `Rewards Used` column (only Amazon prices rewards today).

**Built and live-validated; still accumulating evidence**

These ride real orders and close on their own schedule rather than being work items.

- **Amazon multi-shipment split.** Shipment `1` must update in place while `2`/`3` append, and
  the order must stay open until the last box delivers. Validated on Costco and Best Buy; the
  Amazons share the code path but have not yet had an order split mid-run.
- **The qty-1 re-label rule.** A changed tracking number on a single-unit row is treated as the
  carrier re-issuing the label (new number recorded, old one kept as a `superseded` row) rather
  than as an undisclosed split. Built 2026-09-09, unproven live.
- **BFMR split shipment.** No purchase has ever had two shipments; the create-vs-update path is
  still theory.
- **arm64 / Raspberry Pi.** The container builds and runs on amd64 and a wrong-architecture build
  fails loudly, but real Pi hardware has never run it.

**Parked**

- **`scripts/import_history.py`** — designed, deferred: a one-off import is faster to paste than
  to automate; its old-style negative return rows would be netted at import if one ever recurs
  (see [Importing history](importing-history.md)).
- **Walmart** as the next retailer, only if volume warrants. **Costco Business Center** accounts,
  only if one exists. **Costco 2FA**, deferred with a tripwire. **A tracking-API tier**
  (17TRACK/EasyPost), evaluated and not worth it as a delivery watch.
- **Auto-creating BFMR purchases from reservations** — order numbers go into BFMR by hand right
  after ordering, by design.
- **The healthcheck's unhealthy state notifies nobody** and **preflight cannot verify the
  MaxOutDeals IP allowlist** from the host; both accepted for now.
