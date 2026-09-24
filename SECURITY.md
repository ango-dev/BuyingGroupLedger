# Security

Buying Group Ledger signs in to your retailer accounts and holds the keys to your buying groups, so
the machine it runs on and its dashboard deserve the care you would give a password manager.

## Where Secrets Live

| File | What it holds |
|---|---|
| `config.json` | **Every credential, in plaintext**: the Browser-Use key, proxy logins, retailer passwords and authenticator seeds, buying-group API keys, alert passwords, the dashboard password. |
| `.state.json` | Costco's refresh token (as good as a password) and the setup record. |
| `.env` | Optional overrides; may hold any of the above. |
| `backups/*.zip` | Copies of all three files and the ledger. |

All are gitignored and kept out of the Docker image. Lock them down on the host:

```bash
chmod 600 config.json .state.json   # and .env, if you use one
chmod 700 backups                   # once it exists
```

Copy backups off the host, but somewhere as private as the host itself.

## The Dashboard

**Access to the dashboard is access to every credential**: the Settings page edits `config.json`
and the backups can be downloaded. So:

- **Set a password** (Settings → Dashboard → Sign-in, or the setup wizard). If the dashboard is reachable beyond
  loopback with no password, it serves only a page that sets one, and that page wants a one-time
  **setup token** printed to the dashboard's log (`docker compose logs ledger`, or the terminal
  running `python -m web`).
- **Never port-forward it to the internet.** Reach it over your LAN or a VPN such as Tailscale or
  WireGuard. The compose file publishes it on `127.0.0.1` unless `WEB_PUBLISH_HOST` says otherwise.
- It answers only under an IP address, `localhost`, the host of `web.public_url`, or a name listed
  in **Allowed hosts** (`web.allowed_hosts`), which stops DNS rebinding. Writes a browser marks as
  coming from another site are refused.

## Personal Data

- **Receipts** under `data/receipts/` and the ledger itself hold names, delivery addresses, card
  last 4s and order history.
- **Failure dossiers** under `logs/failures/` are redacted but may still contain names and
  addresses. Never post one publicly, including in a GitHub issue.

## Reporting a Vulnerability

Please open a **GitHub security advisory** (Security → Report a vulnerability) on
<https://github.com/ango-dev/BuyingGroupLedger>. Please don't file a public issue.
