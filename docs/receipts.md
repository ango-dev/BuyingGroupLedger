# Receipt capture

Proof of purchase for every order, rendered to PDF and kept beside the ledger, linked from the
**Receipt Link** column.

## Why

Every row in the ledger is a cost you will substantiate at tax time, and a claim you may have to
prove to a buying group when a package goes missing. Retailer order pages are not a durable record:
Best Buy and Costco keep limited history, and a cancelled or reissued order can vanish from the page.

So each run renders every **newly-seen** order's receipt to PDF and stores it under
`receipts.dir` (default `data/receipts`, inside every backup) as
`<retailer>/<YYYY-MM>/<order id>.<ext>`, and writes the link `/receipts/<retailer>/<YYYY-MM>/<order id>.<ext>`
into Receipt Link — on every row of the order, since a receipt is per order. The link is
**relative to the dashboard**: it opens wherever the dashboard is served from (the LAN, your
WireGuard link, a new host after a restore) and carries no hostname or secret. The BFMR auto-reply
reads such a link straight off the disk when it attaches the receipt.

`receipts.capture_enabled` (`RECEIPT_CAPTURE_ENABLED`) is the switch; off = every run records
orders exactly as before with a blank Receipt Link. There is nothing else to configure.

## What a receipt is, per retailer

The capture opens the order's own page in the profile's cloud browser (the same session the
scraper uses) and prints it to PDF, or falls back to a PNG screenshot where printing is not
available. Which page, and what the page must show before it counts as final, is the rule table in
`receipts/sources.py` — a pre-shipment invoice ("Not Yet Shipped") is refused at capture time so it
never becomes the permanent record, and the document must name its own order id (a Costco page can
show the previous order's render; two Amazon Business invoices can be near-identical).

> ⚠️ **Receipts are PII.** A receipt carries your name, delivery address, card last 4 and totals.
> `data/` is gitignored and never enters an image; the dashboard has no login, so keep it on
> loopback, your LAN or your VPN.

## By hand

The Orders page's add form takes a photo or PDF, and an order's page has an *Upload receipt*
button: the file is stored under the same key the capture uses and the link becomes Receipt Link
on every row of the order. Accepted: pdf, png, jpg, webp, up to 25 MB.

## Checking what is stored

```bash
python -m scripts.receipt_verify                  # every stored receipt: its own order id, not
                                                  # pre-shipment, a total, a payment method
python -m scripts.receipt_verify --purge          # delete the failing ones and blank their links,
                                                  # so the next run captures them again
python -m scripts.backfill_receipts               # orders on the ledger with no receipt (dry run)
python -m scripts.backfill_receipts --apply       # capture them (a cloud browser session per profile)
```

Both are on the dashboard's Tools menu too. `receipt_verify` reads files; `backfill_receipts` spends
browser sessions and asks first.

## Moving off OCI (2026-09-18)

Receipts used to live in an OCI bucket linked by a pre-authenticated request. One run of
`python -m scripts.migrate_receipts_local --apply` (Tools → Ledger Fixes) downloads every hosted
receipt the ledger still links to into `receipts.dir` and rewrites the links; run it **before**
revoking the PAR in the console, since the link is what fetches the object. Rows whose download
fails keep their old link for a re-run. After that, delete the bucket and the customer secret key.

## Probing a retailer's receipt page

`python -m scripts.receipt_probe` settles what a retailer's receipt page actually gives you
(printable? what wording marks it final?). Uploads nothing; it is how a new retailer's row in
`receipts/sources.py` gets written.
