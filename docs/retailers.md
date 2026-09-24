# Retailers: Running a Scrape, and Each Deterministic Path

_Part of the [Buying Group Ledger](../README.md) docs._

Every retailer has a **deterministic, agent-free path**. On any error it writes a
[failure dossier](diagnostics.md#failure-dossiers), alerts with its path, and records nothing for
that retailer that run. **No browser runs locally**: Playwright is only a CDP client to a
Browser-Use cloud browser.

## Running

```bash
.venv/bin/python main.py                  # every retailer x every configured profile
.venv/bin/python main.py amazon           # or amazon-business, bestbuy, costco (several allowed)
# Windows: .venv\Scripts\python main.py
```

Or Tools → Run → *Run once* on the dashboard. Profiles with a blank `profile_id` are skipped. Rows go
to `data/orders_*.csv`, then the ledger; the log is `logs/run.log`. A lock (`logs/.run.lock`, stale
after 3h) stops overlapping runs.

> **A run is not just a scrape.** With the buying-group sync on (`buying_groups.sync_enabled`) it
> ends by posting tracking numbers and filing BFMR insurance ([Buying groups](buying-groups.md)).
> Narrow a test run with `LOOKBACK_DAYS=1 python main.py costco`, or add
> `BUYING_GROUP_SYNC_ENABLED=false` to skip the sync for one command.

Discovery reads the whole lookback window (`scraping.lookback_days`); open orders are re-checked,
terminal ones skipped. An order history with **no orders** is a valid empty result; only a missing
orders *container* is a shape change.

## Amazon

A CDP browser parses the **server-rendered order-details HTML** (there is no order JSON). Tracking
numbers live only on Amazon's **package-tracking page**, which the path visits per shipment. A
tracking page it can't recognise is a soft problem (dossier + alert); "being prepared to ship" with
no carrier number is a normal `ordered`. Gift cards, Rewards Used and promo cashback:
[Profit accounting](data-model.md#profit-accounting).

## Amazon Business

The **same order-details parser**, with its own discovery and click-through pagination, as a separate
scraper (`amazon-business`) so it can't regress the consumer one. Its invoice is a real PDF
([Receipt capture](receipts.md)). Keep the two Amazon accounts on **separate profiles**; on Amazon's
account switcher, sign-in picks the account matching the profile's username or gives up.

## Best Buy

A CDP browser holds the session, discovers order ids from the purchase-history page's embedded
(Next.js flight) data, and reads each order from `/profile/ss/api/v1/orders/<id>` by an **in-page
`fetch`**, since the endpoint is Akamai-guarded. Tracking, per-item cost, card and shipment grouping
come structured. Order numbers are any `BBYnn-…`.

Sessions expire in about 20–25 minutes, so nearly every run signs in again; the profile's `auth`
block with an authenticator seed makes that unattended ([Profiles and sign-in](profiles-and-auth.md)).

## Costco

Costco reads its **private GraphQL API** over `curl_cffi` (TLS impersonation) with a stored
**refresh token**, and no browser. **Online shipped orders only**: warehouse pickups and
Same-Day/Instacart are skipped.

- Discounts are netted per line; an Allstate plan bought with an item joins that item's cost (one no
  single item can claim leaves the cost unread and reported, never understated).
- The item number is appended to the name (`… (Item #…)`), since Costco truncates descriptions.
- Shipments are numbered by physical package (`packageNumber`), so a re-labelled box keeps its number.
- **BFMR's TV rule**: a Costco TV routed to BFMR carries its order number as its tracking number from
  `ordered` on, so the sync submits and insures it; a real carrier number replaces it. Settings:
  `buying_groups.bfmr.costco_tv_order_number_as_tracking` (on) and
  `buying_groups.bfmr.costco_tv_item_pattern` (default `\bTV\b`).

**Token setup (one-time)**, against the profile that owns the membership:

```bash
python -m scripts.costco_token --label profile-2 --grab
```

This reconnects to the profile's cloud browser, signs in if needed (with an `auth.costco` block) and
captures the token from the site's own token exchange. An empty grab usually means the session is
still warm; retry later, or copy the token from a signed-in browser (DevTools → Application → Local
Storage → `https://signin.costco.com`, the `secret` of the key containing `refreshtoken`):

```bash
python -m scripts.costco_token --label profile-2 --token "<REFRESH_TOKEN>" --warehouses 847
python -m scripts.costco_token --label profile-2 --show     # what's stored, masked
```

The dashboard has the same tool (Tools → Accounts → *Costco refresh token*), with the token in a
password box and masked in the logs. Warehouse 847, the default, covers all US warehouses.

The token lives in `.state.json` (gitignored; treat it as a password) and **rotates** on every
exchange, so the file must stay writable. A dead token is re-grabbed automatically; if that fails,
the run alerts with a dossier and records nothing for Costco. Needs `curl_cffi` and `PyJWT`
(`requirements.txt`). The token grab and receipt capture are the only things that use the
costco.com browser session, and both sign in themselves given `auth.costco`.
