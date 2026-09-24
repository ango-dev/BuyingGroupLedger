# Roadmap

_Part of the [Buying Group Ledger](../README.md) docs._

**Where things stand.** Every retailer runs on a deterministic, agent-free path (Amazon and Amazon
Business parse the order pages; Best Buy reads its order API through a cloud browser; Costco reads
its GraphQL API with a stored token). The buying-group sync posts tracking, files insurance and
reads payouts back unattended. Receipts are files beside the ledger. The ledger is a SQLite file
with the web dashboard as its UI, and everything runs in one Docker container (amd64 or a 64-bit
Raspberry Pi). A failure writes a dossier (page, screenshot, selector audit) and alerts; there is no paid
fallback.

Nothing is queued to build right now.

## Built, Waiting for a Real Case

These ride real orders and close when one comes along.

- **Carrier re-label.** A changed tracking number on a single-unit row is treated as the carrier
  re-issuing the label (new number recorded, old one kept as a money-free `superseded` row), not as
  an undisclosed split.
- **Package ID matching.** Rows match on the retailer's own package id (Amazon's `shipmentId`,
  Costco's `packageNumber`, Best Buy's `groupId`) before the tracking number, so a package keeps its
  row whatever position its card takes. The re-ordered-page and re-label cases have not all been
  seen live.
- **Spend caps.** The first card to pass a limit in real use: the blended row and the re-derived
  rates after later syncs.
- **Dashboard import and setup.** The first real CSV through Tools › Import, and the setup wizard on
  a fresh host.

## Parked

- **Walmart** as a retailer, if volume ever warrants it.
- **Best Buy rewards** in the Rewards Used column (only Amazon prices rewards today), when a Best Buy
  order pays with a rewards certificate.
- **Costco Business Center** accounts, if one exists. **Costco 2FA**, when Costco adds it (the
  sign-in names it if so).
- **Auto-creating BFMR purchases from reservations.** Order numbers go into BFMR by hand, by design
  (see [Buying groups](buying-groups.md)).
- **A tracking-API tier** (17TRACK, EasyPost): evaluated, not worth it as a delivery watch.
