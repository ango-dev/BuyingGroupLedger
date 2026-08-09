"""
Create (or reuse) a Browser-Use cloud profile pinned to a custom proxy, and open a live
browser session for you to log into retailer sites manually. Cookies/login state get saved
to the profile when the session is stopped, so future scrapes on that profile skip sign-in.

The built-in Browser-Use profile importer only supports their managed proxies, not a custom
proxy provider — this does the equivalent by creating the session explicitly with
`custom_proxy` set, so login happens through the same IP the scraper will later use.

Usage (run from the project root):
    .venv\\Scripts\\python -m scripts.create_profile --label profile-1

Requires a stub entry for that label already in profiles.json with the proxy filled in
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

from config.profiles import load_profiles, save_profiles


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", required=True, help="Label of the profile entry in profiles.json")
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
            f"No entry with label '{args.label}' in profiles.json. Add a stub entry first "
            "(label, proxy, retailers — profile_id can stay blank), then re-run this script."
        )

    if not profile.proxy or not profile.proxy.host:
        sys.exit(
            f"Profile '{args.label}' has no proxy configured in profiles.json. "
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
        retailers = ", ".join(pending_retailers) if pending_retailers else "(none listed yet in profiles.json)"
        print(f"Open that URL in your own browser and log into: {retailers}")
        print("You can log into more than one retailer in the same session if this profile covers several.")

        # Retailers set to auto-auth via Google (see models.profile.RetailerAuth) re-log-in by
        # riding this profile's long-lived Google session, so that Google login must exist here.
        google_retailers = [r for r, a in profile.auth.items() if a.method in ("google", "apple")]
        if google_retailers:
            print()
            print(
                f"IMPORTANT: {', '.join(sorted(google_retailers))} auto-auth via Google in this profile — "
                "ALSO log into your Google/Gmail account in this same session, and verify 'Sign in with "
                "Google' on that retailer lands in the account holding your orders. Auto-auth rides this "
                "Google session; without it the agent can't self-heal a lapsed retailer session."
            )
        print()
        input("Press Enter here once you're done logging in (this stops the session and saves cookies)... ")
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

    print(f"\nSaved profile_id '{profile_id}' for '{args.label}' into profiles.json.")
    if newly_added:
        print(f"Added retailer(s) to '{args.label}': {', '.join(newly_added)}")
    print("Run main.py for a retailer this profile covers to confirm the login stuck.")


if __name__ == "__main__":
    main()
