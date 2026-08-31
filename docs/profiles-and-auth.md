# Profiles and sign-in

_Part of the [Buying Group Ledger](../README.md) docs._

A **profile** is a Browser-Use cloud browser identity with its own static ISP proxy and the retailers it is logged into. This page covers creating one and letting it sign itself back in.

## Set up a profile (log in through its proxy)

Fill a profile's `proxy` in `config.json`'s `profiles` list (leave `profile_id` blank), then:

```bash
.venv/bin/python -m scripts.create_profile --label profile-1
# add a retailer to an existing profile later:
.venv/bin/python -m scripts.create_profile --label profile-1 --add-retailer walmart
```

It opens a live browser URL — log into the retailer(s) there, press Enter, and it saves the
`profile_id` back into `config.json`. Re-run it any time to log back in if a session expires.

> ⚠️ **Close that browser window when you're done, and don't leave one open while runs are
> scheduled.** A profile's state is saved when a session *closes*, so the last session to close wins.
> A stale window lingering in the background will write its own (possibly logged-out) cookies over
> the profile, silently discarding a sign-in a scheduled run had just completed. The symptom is
> "it logs in every run and never stays logged in", which looks exactly like a broken login — so
> rule this out first.

**Auto-auth (username + password + an authenticator code).** A dead session is a run that records
nothing, and the two retailers that log out do it for different reasons: **Best Buy**'s web sessions
die in ~20–25 min, and **Amazon Business** lapses occasionally but was, until 2026-08-25, the one
retailer that could never heal itself. Give the profile an `auth` block, keyed by retailer, and both
sign themselves back in:

```json
"auth": {
  "bestbuy":         { "method": "password", "username": "you@example.com", "password": "…", "totp_secret": "…" },
  "amazon-business": { "method": "password", "username": "you@example.com", "password": "…", "totp_secret": "…" }
}
```

> ⚠️ **Turn 2-step verification ON, with an AUTHENTICATOR APP**, and paste that enrolment's base32
> key (the "can't scan the barcode?" key) into `totp_secret`. This reverses the older "turn 2FA off"
> advice, and the reason is worth keeping: leaving it off did **not** make sign-in reliable — Best
> Buy kept escalating untrusted sessions to a challenge offering only *"text me a code"*, which
> nothing here can receive, so a run died as a mystery logout. An authenticator code is the one
> challenge a script can answer unattended. It generates the 6-digit code itself and ticks the
> *"don't ask on this device"* box, so a trusted device makes the next lapse need no code at all.
>
> **Enrol the app, not SMS or email.** Which challenge the site serves follows what the account has
> enrolled, so an SMS-only account still stops the run cold.

`password` is the only supported method. Google SSO and Apple were removed in 2026-08-13: each was a
second login path that had to keep working, exercised only when a session happened to die, so a break
in one surfaced days later as a mystery logout. Passkeys were never usable — Browser-Use's cloud
browser has no WebAuthn support. (Amazon's sign-in page shows a permanent passkey error banner for
exactly that reason; it's noise, and the sign-in code ignores it.)

Where the secrets go depends on which path runs:

the sign-in types them into a CDP browser session and computes the TOTP code locally with
`scrapers/totp.py` — it builds no LLM prompt, so nothing leaves the host. (In the retired agent-
fallback era the password rode in the agent's task prompt; that exposure is gone with the agent.)

Without an `auth` block a profile just reports logged-out and alerts, without trying to log in.

> **A failed sign-in is never misreported as a page-shape failure.** It alerts with a *classified* reason —
> a stale password, a locked account, a CAPTCHA, an SMS-only challenge, or auth requests dying at the
> network layer — because those need four different responses and are indistinguishable otherwise.
> Sign-in is also attempted **once per run, never retried**: repeated automated attempts are what
> escalate an account to a forced reset or a lock, and a skipped run is far cheaper than that.
