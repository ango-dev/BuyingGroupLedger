# Profiles and Sign-In

_Part of the [Buying Group Ledger](../README.md) docs._

A **profile** is a Browser-Use cloud browser identity with its own static ISP proxy and the
retailers it is logged into. Keep Amazon and Amazon Business on **separate** profiles: one identity
for both links the accounts.

## Set Up a Profile

Add the profile to `config.json`'s `profiles` list (or Settings › Profiles) with its `proxy` and a
blank `profile_id`, then:

```bash
.venv/bin/python -m scripts.create_profile --label profile-1
.venv/bin/python -m scripts.create_profile --label profile-1 --add-retailer costco   # add one later
```

It prints a live browser URL: log in there, press Enter, and it saves the `profile_id`. Re-run it
whenever a session needs a manual log-in, or use Tools › Accounts on the dashboard, which embeds
the session and closes it after `web.tool_session_minutes`.

The Proxy switch on the profile's line in Settings (`"enabled": false`) makes runs go direct
without losing the proxy's details.

> **Close that browser window when you're done.** A profile's state is saved when a session
> *closes*, so a stale window left open writes its (possibly logged-out) cookies over a sign-in a
> scheduled run just made. If "it logs in every run and never stays logged in", rule this out first.

## Unattended Sign-In

A dead session is a run that records nothing. Best Buy's web sessions die in about 20-25 minutes
and Amazon Business lapses now and then, so give the profile an `auth` block, keyed by retailer,
and it signs itself back in:

```json
"auth": {
  "Best Buy":        { "method": "password", "username": "you@example.com", "password": "…", "totp_secret": "…" },
  "Amazon Business": { "method": "password", "username": "you@example.com", "password": "…", "totp_secret": "…" },
  "Costco":          { "method": "password", "username": "you@example.com", "password": "…" }
}
```

Amazon, Amazon Business, Best Buy and Costco accept one. Costco's order data needs no browser (a
stored refresh token, `scripts.costco_token`); its sign-in serves the token grab and receipt
capture, and has no `totp_secret` (Costco has no 2-step verification).

> **Turn 2-step verification ON, with an authenticator app**, and paste that enrolment's base32 key
> (the "can't scan the barcode?" key) into `totp_secret`. With 2FA off, Best Buy escalated untrusted
> sessions to a "text me a code" challenge nothing here can receive. An authenticator code is the
> one challenge a script can answer: the sign-in generates it locally (`scrapers/totp.py`) and ticks
> *"don't ask on this device"*, so the next lapse usually needs no code. **Enrol the app, not SMS
> or email**: the challenge the site serves follows what the account has enrolled.

- `password` is the only method. Google and Apple sign-in are not supported, and passkeys cannot
  work (the cloud browser has no WebAuthn; Amazon's passkey error banner is noise).
- Credentials are typed into the browser session and the code computed on the host; no LLM sees them.
- Without an `auth` block a logged-out profile alerts and skips, without trying to log in.
- **Sign-in is tried once per run, never retried**: repeated attempts get accounts locked.
- **A failed sign-in alerts with its reason** (stale password, locked account, CAPTCHA, SMS-only
  challenge, network failure), never as a page-shape failure. On Best Buy only an unrecognised
  failure captures the page as a dossier.
