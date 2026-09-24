# Receipt Capture

Proof of purchase for every order, saved as a file beside the ledger and linked from the
**Receipt Link** column. You need it at tax time and when a buying group asks about a missing
package, and retailer order pages are not a durable record (limited history; cancelled or reissued
orders can vanish).

## How It Works

Each run stores every **newly-seen** order's receipt under `receipts.dir` (default `data/receipts`,
inside every backup) as `<retailer>/<YYYY-MM>/<order id>.<ext>`, and writes the link
`/receipts/<retailer>/<YYYY-MM>/<order id>.<ext>` into Receipt Link on every row of the order.

- The link is **relative to the dashboard**, so it works wherever the dashboard is served (LAN, VPN,
  a new host after a restore) and carries no hostname. The BFMR auto-reply reads the file straight
  off the disk.
- The dashboard serves `/receipts/` files of type pdf, png, jpg, jpeg and webp only.
- From the Settings page the receipts folder can be moved only within `data/`; a path set by hand
  in `config.json` stays.
- `receipts.capture_enabled` (`RECEIPT_CAPTURE_ENABLED`) is the switch. Capture costs a paid
  Browser-Use session: one per profile and retailer, on a run that found an order with no stored
  receipt. Off, orders record with a blank Receipt Link.

The capture opens the order's page in the profile's cloud browser and prints it to PDF (a PNG
screenshot where printing is unavailable). `receipts/sources.py` says, per retailer, which page to
use and what marks it final. A pre-shipment invoice ("Not Yet Shipped") is refused, and the
document must name its own order id, so a stale or look-alike page never becomes the record.

> **Receipts are PII**: name, delivery address, card last 4, totals. `data/` is gitignored and
> never enters the image. Set a dashboard password and keep the dashboard on loopback, your LAN or
> your VPN; never port-forward it.

## By Hand

The Orders page's *Add a row* form, the ⤒ button in a Receipt Link cell, and the drop zone on an
order's page all take a pdf, png, jpg or webp up to 25 MB. The file is stored under the same key the
capture uses and linked on every row of the order. Deleting an order's rows deletes its receipt
once nothing links to it.

## Checking What Is Stored

```bash
python -m scripts.receipt_verify              # each stored receipt: its own order id, final, a total, a payment method
python -m scripts.receipt_verify --purge      # delete the failures and blank their links; the next run recaptures
python -m scripts.backfill_receipts           # orders with no receipt (dry run)
python -m scripts.backfill_receipts --apply   # capture them (a cloud browser session per profile)
python -m scripts.receipt_files               # orphan files, dead links, twin names (dry run; --apply tidies)
```

All are on the dashboard's Tools menu. `backfill_receipts --apply` spends browser sessions and asks
first. The Audit fails a delivered or paid row with no Receipt Link and names this backfill.

`python -m scripts.receipt_probe` shows what a retailer's receipt page offers (printable? what
marks it final?); it is how a new retailer's entry in `receipts/sources.py` is written.
