"""
Create (or reuse) a Browser-Use cloud profile pinned to a custom proxy, and open a live
browser session for you to log into retailer sites manually. Cookies/login state get saved
to the profile when the session is stopped, so future scrapes on that profile skip sign-in.

The built-in Browser-Use profile importer only supports their managed proxies, not a custom
proxy provider — this does the equivalent by creating the session explicitly with
`custom_proxy` set, so login happens through the same IP the scraper will later use.

Usage (run from the project root):
    .venv\\Scripts\\python -m scripts.create_profile --label profile-1

Requires a stub entry for that label already in config.json `profiles` with the proxy filled in
(profile_id can be blank — this script fills it in for you). See profiles.example.json.

Re-running against a label that already has a profile_id reuses that same profile and just
opens a fresh live session on it — use this to log back in if a retailer session expires.

Pass --add-retailer to also register this profile for a new retailer while you're logged in:
    .venv\\Scripts\\python -m scripts.create_profile --label profile-1 --add-retailer walmart
"""

import argparse
import sys

# Intentionally the v2 client, not v4: v4 only creates sessions implicitly as a side effect
# of running a task, with no way to open a bare live browser for manual login, and no way to
# explicitly stop one. v2's sessions.create()/.stop() do exactly what this script needs.
from browser_use_sdk import BrowserUse, CustomProxy

# BrowserUse() reads BROWSER_USE_API_KEY out of the ENVIRONMENT itself. Importing config.settings is
# what puts it there, since the key lives in config.json now — without it this dies at "No API key
# provided" against a perfectly valid setup. (config.settings loads .env too, so overrides still win.)
import config.settings  # noqa: F401
from config.profiles import load_profiles, save_profiles


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", required=True, help="Label of the profile entry in config.json `profiles`")
    parser.add_argument(
        "--add-retailer",
        action="append",
        default=[],
        metavar="RETAILER_KEY",
        help="Retailer key (e.g. walmart) to add to this profile's retailers list. Repeatable.",
    )
    args = parser.parse_args()

    profiles = load_profiles()
    profile = next((p for p in profiles if p.label == args.label), None)
    if profile is None:
        sys.exit(
            f"No entry with label '{args.label}' in config.json `profiles`. Add a stub entry first "
            "(label, proxy, retailers — profile_id can stay blank), then re-run this script."
        )

    if not profile.proxy or not profile.proxy.host:
        sys.exit(
            f"Profile '{args.label}' has no proxy configured in config.json. "
            "This script is for pinning a profile to a custom proxy at login time — fill in "
            "the proxy fields first."
        )

    client = BrowserUse()

    if profile.profile_id:
        print(f"Reusing existing Browser-Use profile: {profile.profile_id}")
        profile_id = profile.profile_id
    else:
        created = client.profiles.create(name=args.label)
        profile_id = str(created.id)  # created.id is a UUID; profiles.json needs a str
        print(f"Created new Browser-Use profile: {profile_id}")

    custom_proxy = CustomProxy(
        host=profile.proxy.host,
        port=profile.proxy.port,
        username=profile.proxy.username or None,
        password=profile.proxy.password or None,
    )

    session = client.sessions.create(
        profile_id=profile_id,
        custom_proxy=custom_proxy,
        keep_alive=True,
    )

    try:
        print()
        print(f"Live browser session (through {profile.proxy.host}:{profile.proxy.port}):")
        print(f"  {session.live_url}")
        print()
        pending_retailers = list(profile.retailers) + [r for r in args.add_retailer if r not in profile.retailers]
        retailers = ", ".join(pending_retailers) if pending_retailers else "(none listed yet in config.json)"
        print(f"Open that URL in your own browser and log into: {retailers}")
        print("You can log into more than one retailer in the same session if this profile covers several.")

        # Retailers with stored credentials re-log-in on their own (see models.profile.RetailerAuth).
        # THE ADVICE HERE USED TO BE THE OPPOSITE ("turn 2-step verification OFF"), and it was wrong:
        # leaving it off did not make sign-in reliable, because the sites escalate an untrusted
        # session to a challenge that offers only "text me a code" -- which nothing here can answer.
        # An AUTHENTICATOR code is the one challenge a script CAN answer unattended, so 2FA on with a
        # stored seed is now the supported configuration. See the design notes.
        auto_auth = [r for r, a in profile.auth.items() if a.username and a.password]
        if auto_auth:
            # One source of truth for which retailers expect a seed -- Costco has no US 2FA, so
            # telling anyone to enrol one there would be noise, and noise is what makes a warning
            # get ignored.
            try:
                from scripts.preflight import SELF_LOGIN_RETAILERS
            except Exception:  # noqa: BLE001 -- guidance must never break profile setup
                SELF_LOGIN_RETAILERS = {}
            print()
            print(f"IMPORTANT: {', '.join(sorted(auto_auth))} sign themselves back in from this "
                  "profile using the stored username + password, so a lapsed session heals itself.")
            for retailer in sorted(auto_auth):
                _, expects_totp = SELF_LOGIN_RETAILERS.get(retailer, ("", True))
                seed = (profile.auth[retailer].totp_secret or "").strip()
                if not expects_totp:
                    print(f"  - {retailer}: no 2-step verification exists on this retailer today, so "
                          f"no authenticator seed is needed.")
                elif seed:
                    print(f"  - {retailer}: 2-step verification is configured (a code is generated "
                          f"locally and the 'don't ask on this device' box is ticked).")
                else:
                    print(f"  - {retailer}: TURN 2-STEP VERIFICATION ON with an AUTHENTICATOR APP and "
                          f"put its base32 key in auth['{retailer}'].totp_secret. Not SMS or email -- "
                          f"the challenge served follows what the account has enrolled, and a texted "
                          f"code is one this cannot receive.")
        print()
        input("Press Enter here once you're done logging in (this stops the session and saves cookies)... ")
        # THEN CLOSE THE BROWSER WINDOW. A profile's state is saved when a session CLOSES, so the last
        # session to close wins. A window left open in the background will later write its own
        # (possibly logged-out) cookies over the profile, silently discarding a sign-in a scheduled
        # run had just completed. Diagnosed live, where it looked exactly like a retailer
        # refusing to stay logged in.
        print("Now CLOSE that browser window. Leaving it open lets it overwrite this profile later "
              "-- the last session to close wins, and a stale one can undo a scheduled run's sign-in.")
    finally:
        try:
            client.sessions.stop(session.id)
        except Exception as exc:
            print(f"Warning: failed to stop session {session.id} cleanly: {exc}", file=sys.stderr)
        client.close()

    profile.profile_id = profile_id
    newly_added = [r for r in args.add_retailer if r not in profile.retailers]
    profile.retailers.extend(newly_added)
    save_profiles(profiles)

    print(f"\nSaved profile_id '{profile_id}' for '{args.label}' into config.json.")
    if newly_added:
        print(f"Added retailer(s) to '{args.label}': {', '.join(newly_added)}")
    print("Run main.py for a retailer this profile covers to confirm the login stuck.")


if __name__ == "__main__":
    main()
