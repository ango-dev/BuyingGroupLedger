"""Create (or reuse) a Browser-Use cloud profile pinned to a custom proxy, and open a live
browser session on it for a manual login, so the profile's cookies carry the retailer sessions
the scrapers reuse.

    python -m scripts.create_profile --label profile-1 [--add-retailer walmart ...]

The profile entry must already exist in config.json `profiles` with a label, a proxy and its
retailers (profile_id can be blank — this script fills it in for you). See profiles.example.json.

Re-running against a label that already has a profile_id reuses that same profile and just
opens a fresh live session on it (to re-login after a session lapses, or to add a retailer).

The same steps drive the dashboard's Tools page (web/tools.py: the live session embedded in
the page, closed by the user or after `web.tool_session_minutes`), so the pieces below are
functions, and main() is the command-line face over them.
"""

from __future__ import annotations

import argparse
import sys

# BrowserUse() reads BROWSER_USE_API_KEY out of the ENVIRONMENT itself. Importing config.settings is
# what puts it there, since the key lives in config.json now — without it this dies at "No API key
# provided" against a perfectly valid setup. (config.settings loads .env too, so overrides still win.)
import config.settings  # noqa: F401
from config.profiles import load_profiles, save_profiles


def make_client():
    """The v2 client, not v4: v4 only creates sessions implicitly as a side effect of running a
    task, with no way to open a bare live browser for manual login, and no way to explicitly stop
    one. v2's sessions.create()/.stop() do exactly what this needs."""
    from browser_use_sdk import BrowserUse

    return BrowserUse()


def find_profile(label: str):
    """(profiles, the one with `label`) -- raises ValueError with the operator's message."""
    profiles = load_profiles()
    profile = next((p for p in profiles if p.label == label), None)
    if profile is None:
        raise ValueError(
            f"No entry with label '{label}' in config.json `profiles`. Add a stub entry first "
            "(label, proxy, retailers — profile_id can stay blank), then try again."
        )
    if not profile.proxy or not profile.proxy.host:
        raise ValueError(
            f"Profile '{label}' has no proxy configured in config.json. This pins a profile to a "
            "custom proxy at login time — fill in the proxy fields first."
        )
    return profiles, profile


def ensure_profile_id(client, profile) -> tuple[str, bool]:
    """The Browser-Use profile id for this entry, creating the cloud profile when the entry has
    none. Returns (profile_id, created)."""
    if profile.profile_id:
        return profile.profile_id, False
    created = client.profiles.create(name=profile.label)
    return str(created.id), True  # created.id is a UUID; config.json needs a str


def open_live_session(client, profile, profile_id: str):
    """A kept-alive cloud session on the profile, through its proxy. `.live_url` is the page a
    human logs in on; `.id` is what close_session needs."""
    from browser_use_sdk import CustomProxy

    custom_proxy = CustomProxy(
        host=profile.proxy.host,
        port=profile.proxy.port,
        username=profile.proxy.username or None,
        password=profile.proxy.password or None,
    )
    return client.sessions.create(profile_id=profile_id, custom_proxy=custom_proxy, keep_alive=True)


def close_session(client, session_id: str) -> str:
    """Stop the session -- THIS is what saves the profile's cookies (a profile's state is saved
    when a session CLOSES, so the last session to close wins; a window left open in the background
    later writes its own, possibly logged-out, cookies over the profile, diagnosed live).
    Returns "" or a warning."""
    try:
        client.sessions.stop(session_id)
        return ""
    except Exception as exc:  # noqa: BLE001 -- report, never mask the save that follows
        return f"failed to stop session {session_id} cleanly: {exc}"


def save_profile(profiles, profile, profile_id: str, add_retailers=()) -> list[str]:
    """Write the profile id (and any added retailers) into config.json. Returns the retailers
    that were added."""
    profile.profile_id = profile_id
    newly_added = [r for r in add_retailers if r not in profile.retailers]
    profile.retailers.extend(newly_added)
    save_profiles(profiles)
    return newly_added


def login_guidance(profile, add_retailers=()) -> list[str]:
    """The lines a human should read while logging in: which retailers, and the 2-step rule.

    THE ADVICE HERE USED TO BE THE OPPOSITE ("turn 2-step verification OFF"), and it was wrong:
    leaving it off did not make sign-in reliable, because the sites escalate an untrusted session
    to a challenge that offers only "text me a code" -- which nothing here can answer. An
    AUTHENTICATOR code is the one challenge a script CAN answer unattended, so 2FA on with a stored
    seed is the supported configuration. See the design notes.
    """
    pending = list(profile.retailers) + [r for r in add_retailers if r not in profile.retailers]
    lines = [f"Log into: {', '.join(pending) if pending else '(no retailers listed yet in config.json)'}. "
             "You can log into more than one retailer in the same session if this profile covers several."]
    auto_auth = [r for r, a in profile.auth.items() if a.username and a.password]
    if auto_auth:
        # One source of truth for which retailers expect a seed -- Costco has no US 2FA, so telling
        # anyone to enrol one there would be noise, and noise is what makes a warning get ignored.
        try:
            from scripts.preflight import SELF_LOGIN_RETAILERS
        except Exception:  # noqa: BLE001 -- guidance must never break profile setup
            SELF_LOGIN_RETAILERS = {}
        lines.append(f"{', '.join(sorted(auto_auth))} sign themselves back in from this profile using "
                     "the stored username + password, so a lapsed session heals itself.")
        for retailer in sorted(auto_auth):
            _, expects_totp = SELF_LOGIN_RETAILERS.get(retailer, ("", True))
            seed = (profile.auth[retailer].totp_secret or "").strip()
            if not expects_totp:
                lines.append(f"{retailer}: no 2-step verification exists on this retailer today, so no "
                             "authenticator seed is needed.")
            elif seed:
                lines.append(f"{retailer}: 2-step verification is configured (a code is generated locally "
                             "and the 'don't ask on this device' box is ticked).")
            else:
                lines.append(f"{retailer}: TURN 2-STEP VERIFICATION ON with an AUTHENTICATOR APP and put its "
                             f"base32 key in auth['{retailer}'].totp_secret. Not SMS or email -- the "
                             "challenge served follows what the account has enrolled, and a texted code is "
                             "one this cannot receive.")
    lines.append("When you are done, close the session here (that is what saves the cookies), and CLOSE "
                 "the browser tab you logged in on: a window left open can later overwrite this profile "
                 "with stale cookies -- the last session to close wins.")
    return lines


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

    try:
        profiles, profile = find_profile(args.label)
    except ValueError as exc:
        sys.exit(str(exc))

    client = make_client()
    profile_id, created = ensure_profile_id(client, profile)
    print(f"{'Created new' if created else 'Reusing existing'} Browser-Use profile: {profile_id}")

    session = open_live_session(client, profile, profile_id)
    try:
        print()
        print(f"Live browser session (through {profile.proxy.host}:{profile.proxy.port}):")
        print(f"  {session.live_url}")
        print()
        for line in login_guidance(profile, args.add_retailer):
            print(line)
        print()
        input("Press Enter here once you're done logging in (this stops the session and saves cookies)... ")
    finally:
        warning = close_session(client, session.id)
        if warning:
            print(f"Warning: {warning}", file=sys.stderr)
        client.close()

    newly_added = save_profile(profiles, profile, profile_id, args.add_retailer)
    print(f"\nSaved profile_id '{profile_id}' for '{args.label}' into config.json.")
    if newly_added:
        print(f"Added retailer(s) to '{args.label}': {', '.join(newly_added)}")
    print("Run main.py for a retailer this profile covers to confirm the login stuck.")


if __name__ == "__main__":
    main()
