# Receipt capture

_Part of the [Buying Group Ledger](../README.md) docs._

Proof of purchase for every order, rendered to PDF and stored in your own OCI bucket, linked from the **Receipt Link** column.

The ledger records *what* you bought; it doesn't prove it. That gap has a concrete cost: BFMR wants
**proof of purchase** whenever a combined-carton tracking number has to be suffixed, and the tool's
own alert currently tells you to go find one by hand. Worse, it's a closing window — a delivered
order is terminal and never re-read, so once a run finishes, the chance to grab that receipt is gone.

So each run renders every **newly-seen** order's receipt to PDF, uploads it to OCI Object Storage,
and writes a link into the **Receipt Link** column.

`receipts.oci` in `config.json` — `bucket` is the master switch; blank leaves the feature inert:

```json
"oci": {
  "bucket": "ledger-receipts",
  "s3_endpoint_url": "https://<namespace>.compat.objectstorage.<region>.oraclecloud.com",
  "s3_region": "<region>",
  "// s3_access_key_id": "an OCI *customer secret key*, NOT the API signing key",
  "s3_access_key_id": "...",
  "s3_secret_access_key": "...",
  "par_url_prefix": "https://objectstorage.<region>.oraclecloud.com/p/<secret>/n/<ns>/b/<bucket>/o"
}
```

**One manual setup step: create a bucket-level PAR.** In the OCI console → your bucket →
Pre-Authenticated Requests → Create:

- **Target: Bucket** (not *Objects with prefix* — that's a separate target type, and its URL already
  ends in the prefix, which would double up against the key this code appends. If you do use it, set
  `OCI_PAR_URL_PREFIX` to the part ending at `/o`.)
- **Access type: Permit object reads**
- **Leave "Enable Object Listing" unchecked.** Receipts are PII; listing would let anyone holding
  the URL enumerate every order you've placed, rather than only fetch a receipt they already have a
  link to.
- Expiry: far future

The URL it gives you ends in `/o` — that's `OCI_PAR_URL_PREFIX`, and each object's link is that plus
the object key. Uploads go through the **S3 Compatibility API** (plain boto3),
but a PAR is an OCI-native concept that the S3 API cannot mint — and boto3's presigned URLs expire
within 7 days, while a ledger row gets read months later. One console click buys a link that doesn't
rot, and revoking it is one more.

> ⚠️ **The PAR URL is a secret, and receipts are PII.** Anyone holding it can read every receipt
> under the prefix, and a receipt carries your name, delivery address, card last 4 and order totals.
> Keep the bucket private, and `chmod 600 config.json`.

**One receipt per ORDER, not per shipment.** The object key is `receipts/<retailer>/<YYYY-MM>/<order-id>.pdf`
— no shipment component — so a five-shipment order stores one invoice and all five rows carry the
same Receipt Link. A shipment that appears on a later run finds the object already stored, opens no
browser, and inherits the link.

**A receipt is captured once the order has `shipped`** — never while it's still `ordered`, and
never for a `cancelled` one. Ship time is when proof of purchase is actually wanted (submitting
tracking; BFMR asking for proof behind a suffixed tracking number), and a **lost package never
delivers** — so waiting for delivery would miss the very order an insurance claim depends on. The
receipt is an order-level invoice, so nothing is gained by waiting: the items, prices, totals,
payment method and ship-to all exist the moment it ships. An order first *seen* already delivered is
captured too.

For a **split** order the capture waits until *every* shipment has moved. A receipt is stored once
and never refreshed, so capturing early would permanently keep a partial invoice — printed
`Not Yet Shipped` against the shipments still pending — and these documents substantiate COGS at tax
time. The cost of that: an order with one indefinitely-backordered line never becomes capturable on
a live run, and needs `scripts/backfill_receipts.py`. It's taken once and never refreshed, so capturing early would
permanently store a document predating its own final totals, tracking and delivery date. A split
order waits for its last box, and a cancelled order is never captured at all.

That means orders finished *before* you configured this are unreachable by any normal run, since a
terminal order is never re-read. `python -m scripts.backfill_receipts` (dry run by default,
`--apply` to write, `--retailer` and `--limit` to bound it) walks the sheet and fills them in. It's
safe to re-run: it only fills blank cells and skips anything already stored.

**It costs almost nothing to leave on.** Storage is asked *first*, before any browser exists: an
order whose receipt is already stored just gets its link written from the object key. So a routine
re-check run — the common case — opens **zero** cloud browsers. One browser is created only when at
least one genuinely new order needs a receipt, and it covers all of them.

**What each retailer gives you**, established by `scripts/receipt_probe.py` against real accounts:

| Retailer | What gets stored |
|---|---|
| **Amazon** | Its **print invoice** page rendered to PDF — order number, date, ship-to, payment method, items, quantities, grand total |
| **Amazon Business** | Amazon's **own invoice PDF**, downloaded rather than rendered. The print-invoice URL redirects to a real `order-document.pdf`; rendering that would capture Chrome's PDF *viewer* instead of the document |
| **Best Buy** | The order-details page with its **Payment Details** disclosure expanded, rendered in **print media** — the page ships its own `@media print` rules, so the PDF is the clean receipt, not the navigation and footer |
| **Costco** | The order-details page, with its collapsed **Order Summary** expanded first — otherwise the receipt shows the item and none of the money. ⚠️ Needs a logged-in **browser** session, which Costco's normal path never creates — it runs on a stored GraphQL token with no browser at all. Re-run `scripts.create_profile` and sign into costco.com if captures start being skipped |

**Failures are always partial, never fatal.** A missing receipt is an inconvenience the next run
retries; a missing *order* is missed reimbursement. So one order failing doesn't stop the others, a
capture failure can't stop the CSV write or the sheet sync, and a page that redirects to a sign-in
wall is **refused rather than stored** — storing it would upload a perfect PDF of a login form and
mark that order done forever, since the object would then exist and no later run would retry.

**The document is checked, not just the status.** Amazon labels its Business invoice `Final Details
for Order #…` once shipped and `Details for Order #…` + `Not Yet Shipped` before — and the two can
disagree with your ledger, because Amazon Logistics assigns a `TBA…` tracking number at *label
creation*, not dispatch. So a receipt whose own text says it hasn't shipped is **refused rather than
stored**; the order simply gets captured on a later run. That matters because these receipts
substantiate COGS at tax time and each is written once and never refreshed.

To audit what's already stored — it fetches each object and reads it, no browser:

```bash
python -m scripts.receipt_verify            # every receipt: right order id, final, has a total + payment
python -m scripts.receipt_verify --purge    # delete the failures so capture replaces them
```

Check the whole setup offline and free with `python -m scripts.preflight`, which reports a
*partially* configured bucket as a failure — the case where capture looks switched on but silently
stores nothing.

```bash
# Settle what a retailer's receipt page actually gives you. Uploads nothing.
python -m scripts.receipt_probe --label profile-bravo --retailer amazon --order-id 113-...
```
