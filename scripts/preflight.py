"""Fail loudly, at startup, on the kinds of misconfiguration that otherwise fail SILENTLY.

Run it before trusting an unattended deployment:

    python -m scripts.preflight            # human-readable report; exit 1 if anything FAILED
    python -m scripts.preflight --alert     # also fire the email/Discord alert on failure

It is completely offline and free: no network, no Browser-Use run, no ledger read. It only imports
modules, stats files, and reads env vars. That is deliberate — it has to be cheap enough to run on
every container start.

WHY THIS EXISTS. Every check here corresponds to a real failure mode where the run keeps *working*
and quietly does the wrong thing, so no exception ever surfaces:

- **A missing deterministic-path import.** `scrape()` on Amazon / Amazon Business / Best Buy wraps
  `_scrape_via_api` in a catch-all. An `ImportError` is caught by that catch-all, so a dependency
  missing from the image doesn't crash anything — it fails those retailers on EVERY run, each time
  writing a failure dossier and alerting that a "selector" broke, which sends whoever reads it
  hunting through page HTML for a problem that is really a missing package. `playwright` was exactly
  this: used by scrapers/cdp.py, installed in the dev venv by accident, and absent from
  requirements.txt.
- **A bind mount whose host file is missing.** Docker creates an empty DIRECTORY at that path. The
  loaders test `is_file()`, so an optional config silently reads as "not configured" — every address
  tags `Unclassified` and every card falls back to DEFAULT_CASHBACK_RATE, quietly misstating profit.
- **A missing Costco refresh token.** Self-healing now (see check_costco_tokens), but a profile
  with no `auth["costco"]` block to heal from alerts and skips on every run.

The rule this file encodes: on an unattended host, "still works but costs money" is a failure.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

# The project root, regardless of where this is invoked from.
ROOT = Path(__file__).resolve().parent.parent

OK, WARN, FAIL = "OK", "WARN", "FAIL"


@dataclass
class Result:
    level: str
    name: str
    detail: str


# Modules that make up the deterministic paths, and which retailers fail every run if the import
# breaks. Imported for real — a stale transitive dependency shows up here.
DETERMINISTIC_IMPORTS = {
    "scrapers.cdp": "Amazon, Amazon Business and Best Buy (the CDP browser client)",
    "scrapers.amazon_api": "Amazon",
    "scrapers.amazon_mapping": "Amazon",
    "scrapers.amazon_business_api": "Amazon Business",
    "scrapers.amazon_business_mapping": "Amazon Business",
    "scrapers.bestbuy_api": "Best Buy",
    "scrapers.bestbuy_mapping": "Best Buy",
    "scrapers.costco_api": "Costco",
    "scrapers.costco_mapping": "Costco",
}


def check_deterministic_imports() -> list[Result]:
    """Import every deterministic-path module. A failure here is the expensive-but-silent one."""
    out: list[Result] = []
    for module, covers in DETERMINISTIC_IMPORTS.items():
        try:
            importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001 — any import problem has the same consequence
            out.append(Result(
                FAIL, f"import {module}",
                f"{type(exc).__name__}: {exc} — {covers} would fail on every run, without raising.",
            ))
        else:
            out.append(Result(OK, f"import {module}", covers))
    return out


def _check_path(path: Path, *, required: bool, what: str, parses_json: bool = False) -> Result:
    """Stat a config path, distinguishing 'missing' from Docker's empty-directory bind-mount trap."""
    name = path.name
    if path.is_dir():
        return Result(
            FAIL, name,
            f"is a DIRECTORY, not a file. This is Docker's bind-mount behaviour when the host file "
            f"does not exist: create {path} on the host (even as an empty stub) or remove its "
            f"volume line from docker-compose.yml. Until then: {what}",
        )
    if not path.exists():
        level = FAIL if required else WARN
        return Result(level, name, f"missing. {what}")
    if parses_json:
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            return Result(FAIL, name, f"is not valid JSON ({exc}).")
    return Result(OK, name, "present")


#: The files config.json replaced. Their presence is how an un-migrated host is recognised.
LEGACY_CONFIG_FILES = ("profiles.json", "warehouses.json", "cards.json", "service_account.json")


def check_config_files(root: Path = ROOT) -> list[Result]:
    """One config file now, and a loud failure for a host that still has the old six.

    A silent fallback would be worse than a hard stop here. Without config.json the loaders return
    empty sections, so an un-migrated host does not crash — it scrapes NOTHING (no profiles), tags
    every address Unclassified and posts to no buying group, all while exiting 0. That is precisely
    the silent misconfiguration this whole script exists to catch, and it gates the container
    (docker/entrypoint.sh), so the run stops instead.
    """
    config = root / "config.json"
    legacy = [name for name in LEGACY_CONFIG_FILES if (root / name).is_file()]

    if config.is_dir():
        # Docker creates an empty DIRECTORY when a bind-mounted host file is missing. Reporting it
        # as "missing" sends someone hunting for the wrong problem.
        return [_check_path(config, required=True, parses_json=True,
                            what="nothing can be loaded: no profiles, no ledger, no credentials.")]

    if not config.is_file():
        if legacy:
            return [Result(
                FAIL, "config.json",
                f"is missing, but the old config is still here ({', '.join(legacy)}). This host has "
                f"not been migrated, and nothing reads those files any more — it would scrape "
                f"nothing and exit 0. Fix with `python -m scripts.migrate_config` (dry run), then "
                f"`--apply`. The old files are left in place.",
            )]
        return [Result(
            FAIL, "config.json",
            "is missing — there is no configuration at all, so no profile or credential is "
            "available. Copy config.example.json to config.json and fill it in.",
        )]

    out = [_check_path(config, required=True, parses_json=True,
                       what="nothing can be loaded: no profiles, no ledger, no credentials.")]
    if legacy:
        out.append(Result(
            WARN, "legacy config",
            f"{', '.join(legacy)} still present but NO LONGER READ — config.json wins. Delete them "
            f"once you are satisfied, so nobody edits a file that has no effect.",
        ))

    # Each section is optional in a different way, and each failure is quiet, so name the cost.
    try:
        from config.loader import config_section
        for name, cost in (
            ("profiles", "no profile can be loaded, so NOTHING is scraped."),
            ("warehouses", "every address tags Unclassified, so no order routes to a buying group."),
            ("cards", "every row falls back to DEFAULT_CASHBACK_RATE, misstating profit."),
        ):
            entries = config_section(name)
            level = (FAIL if name == "profiles" else WARN) if not entries else OK
            out.append(Result(
                level, f"config.json `{name}`",
                f"{len(entries)} entr(y/ies)" if entries else f"is empty — {cost}",
            ))
    except Exception as exc:  # noqa: BLE001 — a malformed section is already a FAIL above
        out.append(Result(FAIL, "config.json sections", f"could not be read ({exc})."))

    return out


def check_costco_tokens(root: Path = ROOT) -> list[Result]:
    """Costco's API path needs a refresh token per profile — but a MISSING one is now self-healing.

    Since 2026-08-29 a profile that has `auth["costco"]` bootstraps its own token on the first run:
    `costco_api` raises `CostcoAuthError`, `costco.py` answers it with `_refresh_token_via_browser`,
    which signs in and captures one. So "no token yet" is a state, not a misconfiguration, and
    reporting it as a FAIL trains people to ignore this check — the one outcome worse than not
    having it.

    The old wording also claimed a missing token "silently uses the agent" (this predates the
    agent's removal). It never did: an auth failure raises `ApiLoginError` and costco.py alerts
    and SKIPS.
    """
    try:
        from config.profiles import load_profiles_for_retailer
        profiles = load_profiles_for_retailer("costco")
    except Exception as exc:  # noqa: BLE001 — a broken profiles.json is already reported above
        return [Result(WARN, "costco token", f"could not read profiles ({exc}); skipped.")]

    if not profiles:
        return [Result(OK, "costco token", "no profile lists costco; nothing to check.")]

    out = []
    for profile in profiles:
        from config.loader import STATE_FILE
        from scrapers.costco_api import load_costco_auth

        token = STATE_FILE
        if load_costco_auth(profile.label):
            # Present is not enough: Costco ROTATES the refresh token on every refresh and
            # _save_auth persists the new one. A read-only mount (the natural-looking `:ro` for a
            # file full of secrets) turns that write into an OSError, so the rotated token is
            # thrown away, which can strand the stored one. Probe the directory rather than trusting permission bits, since a
            # container running as root reads as writable right up until the mount refuses.
            probe = token.parent / ".preflight_write_probe"
            try:
                probe.touch()
                probe.unlink()
            except OSError as exc:
                out.append(Result(
                    FAIL, f"costco token [{profile.label}]",
                    f"present but its directory is NOT WRITABLE ({exc.strerror}). Costco ROTATES "
                    f"its refresh token on every use and must save the new one; discarding it "
                    f"strands the stored copy, so the next run cannot authenticate and alerts "
                    f"and SKIPS Costco. In "
                    f"Docker, drop the `:ro` from the ./.state.json volume in docker-compose.yml.",
                ))
            else:
                out.append(Result(OK, f"costco token [{profile.label}]", "present and writable"))
        else:
            auth = (profile.auth or {}).get("costco")
            if auth is not None and auth.username and auth.password:
                out.append(Result(
                    OK, f"costco token [{profile.label}]",
                    "no token stored YET, and none is needed by hand: auth['costco'] is configured, "
                    "so the first run signs in over the browser and captures one "
                    "(costco.py:_refresh_token_via_browser). Costs one cloud browser, once.",
                ))
            else:
                out.append(Result(
                    FAIL, f"costco token [{profile.label}]",
                    f"no token stored in {token} AND no auth['costco'] to sign in with, so Costco "
                    f"cannot authenticate at all: every run alerts and SKIPS it. Fix EITHER by "
                    f"adding auth['costco'] "
                    f"(method/username/password) and letting a run bootstrap the token, OR by hand "
                    f"with `python -m scripts.costco_token --label {profile.label} --token "
                    f"'<REFRESH_TOKEN>'`. In a container, also check ./.state.json is mounted.",
                ))
    return out


# Env vars with no safe default: without them a run either cannot start or cannot record anything.
REQUIRED_ENV = {
    "BROWSER_USE_API_KEY": "no cloud browser can be created, so no retailer can be read.",
}


def check_env() -> list[Result]:
    """Resolved through `settings`, NOT os.getenv.

    These values may come from config.json or from the environment, and reading the environment
    directly would report a perfectly configured host as broken — the exact false alarm that makes
    people stop trusting preflight. `settings` is the one place that knows the resolution order.
    """
    # Resolved through settings.py's own helpers, NOT the `settings` singleton and not os.getenv.
    #
    # Not the singleton because a dataclass evaluates its field defaults ONCE, when the class is
    # defined — so `Settings()` re-reads nothing and would report whatever was true at import. Not
    # os.getenv because a value may legitimately live only in config.json, and reading the
    # environment alone would call a correctly configured host broken. The helpers are the only
    # thing that applies the real order: environment, then config file, then default.
    from config.settings import _get_str

    resolved = {
        "BROWSER_USE_API_KEY": os.getenv("BROWSER_USE_API_KEY", ""),
    }
    out = [
        Result(FAIL, name, f"is unset — {why}") if not str(resolved.get(name, "")).strip()
        else Result(OK, name, "set")
        for name, why in REQUIRED_ENV.items()
    ]

    # Alerts are how an unattended host tells you anything at all. Neither channel configured means
    # a silent failure stays silent until you happen to look at the ledger.
    def on(name: str) -> bool:  # a switch that defaults to true
        return _get_str(name).strip().lower() not in ("false", "0", "no", "off")

    email = on("GMAIL_ALERTS_ENABLED") and _get_str("GMAIL_ADDRESS").strip() and _get_str("GMAIL_APP_PASSWORD").strip()
    discord = on("DISCORD_ALERTS_ENABLED") and _get_str("DISCORD_WEBHOOK_URL").strip()
    if email or discord:
        channels = ", ".join(c for c, live in (("Gmail", email), ("Discord", discord)) if live)
        out.append(Result(OK, "alerts", f"on and configured ({channels})"))
    else:
        out.append(Result(
            WARN, "alerts",
            "neither Gmail nor Discord is on and configured. On an unattended host this means a "
            "logged-out session or a failing run reports to nobody. Test with `python -m alerts.notifier`.",
        ))
    return out


def check_money_switches() -> list[Result]:
    """Not errors — the settings that spend real money unattended, surfaced so they're never a surprise."""
    try:
        from config.settings import settings
    except Exception as exc:  # noqa: BLE001
        # config/settings.py validates at IMPORT time and deliberately raises rather than guessing
        # — e.g. DEFAULT_CASHBACK_RATE=2 is rejected because it reads equally as 2% or 200%. That
        # makes every run die on import, so preflight has to name it rather than die the same way.
        return [Result(
            FAIL, "config values",
            f"config/settings.py could not be loaded: {type(exc).__name__}: {exc}. Every run fails "
            f"at import until this is fixed.",
        )]

    out: list[Result] = []
    # The combined-package auto-reply sends mail as ITS OWN account and never borrows the alerts
    # one (2026-09-18); switched on without that account it would fail every run.
    if settings.bfmr_combined_package_autoreply_enabled:
        try:
            settings.bfmr_reply_account()
        except RuntimeError as exc:
            out.append(Result(FAIL, "combined-package auto-reply", f"ON, but {exc}"))
        else:
            out.append(Result(OK, "combined-package auto-reply",
                              f"ON, sending as {settings.bfmr_combined_package_gmail_address.strip()}"))
    if not settings.buying_group_sync_enabled:
        out.append(Result(WARN, "buying-group sync", "DISABLED — tracking numbers are not submitted and "
                                                     "no payout is read back (set buying_groups.sync_enabled to true in config.json)."))
        return out

    out.append(Result(OK, "buying-group sync", "ENABLED — every run submits tracking and files BFMR "
                                               "insurance unattended, spending real money."))
    if (settings.maxoutdeals_api_key or "").strip():
        out.append(Result(
            WARN, "MaxOutDeals IP allowlist",
            "MOD rejects any call from an unregistered IP, whatever the token. This host's public IP "
            "must be added under the firewall tab in your MOD profile — re-check it after ANY move "
            "to a new machine, ISP or container host.",
        ))
    return out


def check_receipt_capture() -> list[Result]:
    """Receipt capture stores files beside the ledger (receipts/store.py); the failure worth
    catching is a directory the process cannot write, which would cost a cloud browser session
    per receipt every run and store nothing. Offline and free, as every preflight check is."""
    try:
        from config.settings import settings
        from receipts.store import receipts_dir
    except Exception as exc:  # noqa: BLE001 — a broken settings import is already reported elsewhere
        return [Result(WARN, "receipt capture", f"could not be checked ({exc}).")]
    if not settings.receipt_capture_enabled:
        return [Result(WARN, "receipt capture",
                       "DISABLED (RECEIPT_CAPTURE_ENABLED) — no order receipts are stored.")]
    directory = receipts_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".preflight"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        return [Result(FAIL, "receipt capture",
                       f"{directory} is not writable ({exc}) — every run would open a cloud browser "
                       "per receipt and store nothing. Fix the mount / permissions.")]
    out = [Result(OK, "receipt capture", f"files under {directory}")]

    # pypdf is OPTIONAL at runtime and the finality guard fails open without it, which is precisely
    # the shape preflight exists for: receipts keep being captured and stored, so nothing looks
    # wrong, but a pre-shipment invoice ("Not Yet Shipped") is no longer rejected -- and these are
    # the documents that substantiate COGS at tax time.
    try:
        importlib.import_module("pypdf")
    except Exception:  # noqa: BLE001
        out.append(Result(
            WARN, "import pypdf",
            "missing, so a captured receipt's TEXT cannot be read: pre-shipment invoices are no "
            "longer refused, and `python -m scripts.receipt_verify` cannot audit what is stored. "
            "Receipts are still captured. Fix with `pip install -r requirements.txt`.",
        ))
    else:
        out.append(Result(OK, "import pypdf", "receipt finality guard active"))
    return out


def runs_per_day(hours: int) -> int:
    """How many times `0 */H * * *` actually fires in a day.

    NOT 24/H. Cron's step operator enumerates multiples of H within the hour field's 0-23 range, so
    an interval that doesn't divide 24 evenly gives an uneven day: H=5 fires at 0,5,10,15,20 — five
    times, not 24/5. The last gap is short and the count is what the quota cares about.
    """
    return (23 // hours) + 1


def check_run_interval() -> list[Result]:
    """Warn when the schedule would outrun MaxOutDeals' daily quota.

    The interval is bounded by a THIRD PARTY, not by anything here: MOD allows a fixed number of
    received-items calls per day and every run spends exactly one. Two things stop that being
    self-correcting, which is why it needs saying out loud at startup:

    - `DailyCallBudget` is deliberately per-process, so it bounds a single run and cannot see the
      day's total across scheduled runs. MOD's server is the only real authority, and it simply
      starts refusing.
    - A DRY RUN spends one too — `fetch_payouts` is a non-mutating read, and the client only
      short-circuits when the call is BOTH mutating and dry-run.

    Going over is contained rather than dangerous: it stops the payout/premium/status write-back,
    not tracking submission (whose separate push limit is far higher). Payouts just stop updating
    until the daily reset. So this is a WARNING, not a failure — the user may well accept it to get
    fresher shipment status.
    """
    # Environment first, then config.json — the same order docker/entrypoint.sh resolves it in.
    #
    # os.getenv ALONE IS NOT ENOUGH ANY MORE. The interval moved into config.json under
    # `container.run_interval_hours`, and compose passes the variable through with no default, so on
    # a normal container host the environment says nothing and this check used to return [] — silently
    # skipping the MOD daily-limit warning on exactly the deployment it was written for.
    from config.loader import config_value
    raw = (os.getenv("RUN_INTERVAL_HOURS") or "").strip()
    if not raw:
        configured = config_value("container.run_interval_hours")
        raw = "" if configured is None else str(configured).strip()
    if not raw:
        # Neither source names one: not a container deployment. (The native cron path takes its
        # interval as an argument to scripts/install_cron.sh, which does this same check itself.)
        return []

    if not raw.isdigit() or not 1 <= int(raw) <= 23:
        return [Result(WARN, "RUN_INTERVAL_HOURS",
                       f"{raw!r} is not a whole number of hours from 1 to 23; the container will "
                       f"fall back to 6.")]

    hours = int(raw)
    per_day = runs_per_day(hours)

    try:
        from buying_groups.maxoutdeals import RECEIVED_ITEMS_DAILY_LIMIT as limit
        from config.settings import settings
        sync_on = settings.buying_group_sync_enabled
    except Exception:  # noqa: BLE001 — reported by check_money_switches; don't fail twice
        return []

    if not sync_on or per_day <= limit:
        spare = limit - per_day
        detail = f"every {hours}h = {per_day} run(s)/day"
        if sync_on:
            detail += f"; uses {per_day} of MaxOutDeals' {limit} daily payout reads ({spare} spare)"
        return [Result(OK, "run interval", detail)]

    return [Result(
        WARN, "run interval",
        f"every {hours}h = {per_day} runs/day, but MaxOutDeals allows only {limit} payout reads per "
        f"day and each run spends one. Roughly {per_day - limit} run(s)/day will fail to read "
        f"payouts, so Actual Payout / Insurance / paid status stop updating until the daily reset "
        f"(tracking submission is unaffected). A manual `sync_tracking` spends one too, even as a "
        f"DRY RUN. Use {-(-24 // limit)}h or longer to stay inside the quota.",
    )]


#: Retailers whose session lapses AND that can sign themselves back in, mapped to where the
#: authenticator seed is enrolled. Both are opt-in per profile: no auth block means a lapsed session
#: records NOTHING until a human re-authenticates by hand.
#: Each entry is (where the authenticator seed is enrolled, is a seed EXPECTED at all).
#: **Costco expects none**: it had no US 2-step verification as of 2026-08-25, so a blank
#: `totp_secret` there is correct rather than a gap. Warning about it would train someone to ignore
#: this check, which is the one outcome worse than not having it. If Costco ever adds 2FA, flip the
#: flag and pick up the deferred work in the design notes.
SELF_LOGIN_RETAILERS = {
    "bestbuy": ("Account Settings -> Sign-in & Security", True),
    "amazon-business": ("Login & Security -> 2-step verification -> Authenticator App", True),
    "amazon": ("Login & Security -> 2-step verification -> Authenticator App", True),
    "costco": ("no 2-step verification exists on Costco US today", False),
}


def check_self_login() -> list[Result]:
    """A retailer that can heal a lapsed session, but has no credentials to do it with.

    This is the silent-misconfiguration shape preflight exists for. Nothing fails at boot, nothing
    fails on a warm run, and the gap only shows up as a run that quietly recorded zero rows for one
    retailer — days later, if anyone happens to read the log. Amazon Business did exactly that for
    two consecutive runs before it was noticed.

    A missing TOTP seed is called out separately, because it fails LATER than the password does: the
    password is accepted and the run then stops dead at the 2-step screen.
    """
    try:
        from config.profiles import load_profiles
        profiles = load_profiles()
    except Exception as exc:  # noqa: BLE001 — a broken config.json is already reported above
        return [Result(WARN, "self-login", f"could not read profiles ({exc}); skipped.")]

    out = []
    for profile in profiles:
        for retailer, (where, expects_totp) in SELF_LOGIN_RETAILERS.items():
            if retailer not in profile.retailers:
                continue
            name = f"self-login [{profile.label}/{retailer}]"
            auth = (profile.auth or {}).get(retailer)
            if auth is None or not auth.username or not auth.password:
                consequence = (
                    "a lapsed browser session cannot mint a refresh token or capture receipts"
                    if retailer == "costco" else
                    f"a lapsed session records NOTHING for {retailer}"
                )
                out.append(Result(
                    WARN, name,
                    f"no usable auth block, so {consequence} until someone runs "
                    f"`python -m scripts.create_profile --label {profile.label}` by hand. Add "
                    f"auth['{retailer}'] (method/username/password) to config.json to let it heal "
                    f"itself.",
                ))
            elif expects_totp and not auth.totp_secret:
                out.append(Result(
                    WARN, name,
                    f"password is set but totp_secret is EMPTY. If the account has 2-step "
                    f"verification on, sign-in gets as far as the code screen and then stops — the "
                    f"password being right is not enough. Paste the base32 key from {where}.",
                ))
            elif not expects_totp:
                out.append(Result(OK, name, f"password configured; no seed needed ({where})"))
            else:
                out.append(Result(OK, name, "password + authenticator seed configured"))
    return out


#: Every retailer key `main.SCRAPERS` knows. Duplicated here rather than imported, because importing
#: `main` configures logging and opens logs/run.log as a side effect of the import -- unacceptable in
#: a check that runs on every container start. `tests/test_preflight.py` asserts this matches
#: `main.SCRAPERS` exactly, the same way `receipts/sources.py` pins its duplicated constants.
KNOWN_RETAILER_KEYS = ("amazon", "amazon-business", "bestbuy", "costco")

#: One profile must never hold both Amazon accounts -- see config/profiles.py, which RAISES on it.
_MUTUALLY_EXCLUSIVE = frozenset({"amazon", "amazon-business"})


def check_profiles() -> list[Result]:
    """Profiles that are configured but can never actually run.

    THREE WAYS A PROFILE SILENTLY DOES NOTHING, and none of them is visible where you would look:

    - **A blank `profile_id`.** `main.py` skips it for every retailer it lists, with one
      `log.warning` buried in a scheduled run's log.
    - **A misspelled retailer key** (`Amazon`, `amazon_business`, `best-buy`).
      `load_profiles_for_retailer` is an EXACT-match filter, and `main.py` only warns when NO profile
      matches a retailer -- so an existing profile masks the typo completely and nothing is logged at
      all. That is why this one is a FAIL: it has no other signal anywhere.
    - **An empty `retailers` list.** Selected for nothing, ever.

    The per-retailer coverage line exists for the fourth case, which is not a broken profile at all:
    a config that never reached the host. "amazon: 1 usable profile" when you added a second account
    is the cheapest possible way to notice, and `config.json` is gitignored so it does NOT travel
    with a `git pull`.
    """
    try:
        from config.profiles import load_profiles
        profiles = load_profiles()
    except ValueError as exc:
        # config/profiles.py raises this for the amazon + amazon-business conflict. Report it as a
        # check rather than letting preflight die on a traceback -- a config error should be told to
        # the operator in the same format as everything else.
        return [Result(FAIL, "profiles", f"config.json is rejected: {exc}")]
    except Exception as exc:  # noqa: BLE001 -- a broken config is already reported by check_config_files
        return [Result(WARN, "profiles", f"could not read profiles ({exc}); skipped.")]

    out: list[Result] = []
    for profile in profiles:
        name = f"profile [{profile.label}]"
        unknown = [r for r in profile.retailers if r not in KNOWN_RETAILER_KEYS]
        if unknown:
            out.append(Result(
                FAIL, name,
                f"unknown retailer key(s) {unknown} — this profile is NEVER selected for them and "
                f"nothing says so at run time (the filter is an exact match, and another profile "
                f"covering the same retailer hides it). Valid keys: {list(KNOWN_RETAILER_KEYS)}.",
            ))
        elif not profile.retailers:
            out.append(Result(
                WARN, name,
                "no retailers listed, so this profile is never selected for anything. Add the "
                f"retailer key(s) it is logged into: {list(KNOWN_RETAILER_KEYS)}.",
            ))
        elif not profile.profile_id:
            out.append(Result(
                WARN, name,
                f"no profile_id, so it is SKIPPED for every retailer it lists ({profile.retailers}) "
                f"and records nothing. Fix with `python -m scripts.create_profile --label "
                f"{profile.label}` — adding the entry by hand is not enough.",
            ))
        else:
            out.append(Result(OK, name, f"id set; covers {profile.retailers}"))

    # Coverage, so a config that never reached this host is visible as a NUMBER rather than inferred.
    usable = [p for p in profiles if p.profile_id]
    covered = {
        key: sum(1 for p in usable if key in p.retailers) for key in KNOWN_RETAILER_KEYS
    }
    summary = ", ".join(f"{key}={count}" for key, count in covered.items())
    uncovered = [key for key, count in covered.items() if not count]
    out.append(Result(
        WARN if uncovered else OK, "profile coverage",
        f"usable profiles per retailer: {summary}"
        + (f" — NOTHING is scraped for {uncovered}" if uncovered else ""),
    ))
    return out


def run_checks() -> list[Result]:
    results: list[Result] = []
    results += check_deterministic_imports()
    results += check_config_files()
    results += check_env()
    results += check_costco_tokens()
    results += check_profiles()
    results += check_self_login()
    results += check_money_switches()
    results += check_receipt_capture()
    results += check_run_interval()
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--alert", action="store_true",
                        help="send the configured email/Discord alert if any check FAILS")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures too")
    args = parser.parse_args(argv)

    # The detail strings contain em-dashes. A legacy Windows console (cp1252) mangles or raises on
    # those, and a preflight that crashes while reporting is worse than useless.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — not all streams support it (pytest capture, pipes)
            pass

    results = run_checks()
    failures = [r for r in results if r.level == FAIL]
    warnings = [r for r in results if r.level == WARN]

    width = max(len(r.name) for r in results)
    for r in results:
        print(f"[{r.level:4}] {r.name.ljust(width)}  {r.detail}")

    print(f"\n{len(results) - len(failures) - len(warnings)} ok, {len(warnings)} warning(s), "
          f"{len(failures)} failure(s)")

    if failures and args.alert:
        try:
            from alerts.notifier import alert
            body = "\n".join(f"- {r.name}: {r.detail}" for r in failures)
            alert("Preflight FAILED — the ledger is misconfigured",
                  f"{len(failures)} check(s) failed on this host:\n\n{body}")
        except Exception:  # noqa: BLE001 — an unsendable alert must not mask the real failures
            print("(could not send the alert; the failures above still stand)", file=sys.stderr)

    return 1 if failures or (args.strict and warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
