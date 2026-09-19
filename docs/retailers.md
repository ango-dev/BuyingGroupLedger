# Retailers — running a scrape, and each deterministic path

_Part of the [Buying Group Ledger](../README.md) docs._

Every retailer has a **deterministic, agent-free primary path** and the same failure contract: on any
error it writes a [failure dossier](diagnostics.md#failure-dossiers), alerts with the path, and
records nothing for that retailer that run. **No browser runs locally** on any of them — Playwright
is only a CDP *client* to a Browser-Use cloud browser.

## Running

```bash
# all retailers x all configured profiles:
.venv/bin/python main.py            # Windows: .venv\Scripts\python main.py

# a specific retailer:
.venv/bin/python main.py amazon
.venv/bin/python main.py amazon-business
.venv/bin/python main.py bestbuy
.venv/bin/python main.py costco
```

Profiles with a blank `profile_id` are skipped. Output goes to `data/orders_*.csv` and the ledger (`data/ledger.sqlite3`);
logs to `logs/run.log`. A lock (`logs/.run.lock`, auto-expires after 3h) prevents overlapping runs.

> **A run is not just a scrape.** With `BUYING_GROUP_SYNC_ENABLED=true` it ends by posting tracking
> numbers and filing BFMR insurance — see [Buying groups](buying-groups.md). Narrow a test run with
> `LOOKBACK_DAYS=1 python main.py costco`, or override the sync off for one command with
> `BUYING_GROUP_SYNC_ENABLED=false`.

Each retailer's discovery is paginated and reads the whole window (`LOOKBACK_DAYS`); already-recorded
open orders are re-checked for status/tracking, terminal ones are skipped. An account whose order
history renders **with no orders** is a legitimate empty result, not a failure — only a missing
orders *container* is treated as a shape change.

## Amazon

A CDP browser parses the **server-rendered order-details HTML** — a network capture proved there is
no order JSON to read; every JSON response was telemetry or recommendation carousels. Each shipment's
tracking number lives only on Amazon's separate **package-tracking page**, so the path hops there per
shipment. A tracking page the reader cannot recognise is reported as a soft problem (dossier + alert)
rather than left as `ordered` forever.

Gift-card netting and promo-cashback reading are Amazon-specific and documented under
[Profit accounting](data-model.md#profit-accounting).

## Amazon Business

The **same parser** as consumer Amazon — order details are identical — but its own discovery and
click-through pagination, as a separate scraper (`retailer_key` `amazon-business`) so it cannot
regress the consumer one. Its invoice is a real PDF rather than a rendered page
([Receipt capture](receipts.md)). Keep the two Amazon accounts on **separate profiles**; the account
switcher is handled, but a profile whose `auth['amazon-business'].username` is not the Business
account's own email is refused rather than guessed.

## Best Buy

A CDP browser holds the profile's logged-in cookie, discovers order ids from the purchase-history
page's embedded (Next.js flight) data, and reads each order from Best Buy's own
`/profile/ss/api/v1/orders/<id>` endpoint via an **in-page `fetch`** — the endpoint is Akamai-guarded,
so the read rides the session from inside the page rather than replaying it out-of-band. Tracking
numbers, per-item cost, card and native shipment grouping all come structured.

Best Buy's web sessions expire in ~20–25 minutes, so nearly every run signs in again; the profile's
`auth` block with an authenticator seed is what makes that unattended
([Profiles and sign-in](profiles-and-auth.md)).

## Costco

Costco reads its **private GraphQL API** over `curl_cffi` (TLS impersonation, to pass the fingerprint
check) with a stored **refresh token** — no browser at all. It tracks **online shipped orders only**;
in-warehouse pickups and Same-Day/Instacart grocery are skipped. Discounts are netted per line, and
shipments are numbered by physical package (a 2-box order reads Shipment 1 / Shipment 2).

**Token setup (one-time).** The path needs a refresh token saved against the profile that owns the
membership. Either let it grab one:

```bash
python -m scripts.costco_token --label profile-2 --grab
```

which reconnects to the profile's cloud browser, signs in if the session has lapsed (needs an
`auth.costco` block), and captures the token off the app's own token exchange — or copy it by hand
from a logged-in browser (DevTools → Application → Local Storage → `https://signin.costco.com`, the
`secret` of the key whose name contains `refreshtoken`):

```bash
python -m scripts.costco_token --label profile-2 --token "<REFRESH_TOKEN>" --warehouses 847
```

847 covers all US warehouses. The token is written to `.state.json` (gitignored — treat it like a
password) and **rotates** on every exchange, so that file must stay writable. A dead token is
recovered automatically the same way `--grab` works; if that fails too, the run alerts with a
dossier and records nothing for Costco until the next run. This path needs `curl_cffi` and `PyJWT`
from `requirements.txt`.

Costco's data path never opens a browser, so nothing keeps the profile's costco.com **browser**
session warm — the only things that need it are the token grab and receipt capture, and both sign in
themselves when the profile has an `auth.costco` block.
