"""Fail loudly, at startup, on the kinds of misconfiguration that otherwise fail SILENTLY.

Run it before trusting an unattended deployment:

    python -m scripts.preflight            # human-readable report; exit 1 if anything FAILED
    python -m scripts.preflight --alert     # also fire the email/Discord alert on failure

It is completely offline and free: no network, no Browser-Use run, no Sheets call. It only imports
modules, stats files, and reads env vars. That is deliberate — it has to be cheap enough to run on
every container start.

WHY THIS EXISTS. Every check here corresponds to a real failure mode where the run keeps *working*
and quietly does the wrong thing, so no exception ever surfaces:

- **A missing deterministic-path import.** `scrape()` on Amazon / Amazon Business / Best Buy wraps
  `_scrape_via_api` in a catch-all that degrades to the Browser-Use agent. An `ImportError` is
  caught by that catch-all, so a dependency missing from the image doesn't crash anything — it just
  moves three retailers onto the PAID agent path, forever, at roughly $0.01-0.10 per retailer per
  run. `playwright` was exactly this: used by scrapers/cdp.py, installed in the dev venv by
  accident, and absent from requirements.txt.
- **A bind mount whose host file is missing.** Docker creates an empty DIRECTORY at that path. The
  loaders test `is_file()`, so an optional config silently reads as "not configured" — every address
  tags `Unclassified` and every card falls back to DEFAULT_CASHBACK_RATE, quietly misstating profit.
- **A missing Costco refresh token.** Costco alerts and falls back to the agent, which works, so the
  only symptom is a recurring bill.

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


# Modules that make up the deterministic (agent-free) paths, and what falls back to the paid agent
# if the import breaks. Imported for real — a stale transitive dependency shows up here.
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
                f"{type(exc).__name__}: {exc} — {covers} would fall back to the PAID Browser-Use "
                f"agent on every run, without raising.",
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


def check_config_files(root: Path = ROOT) -> list[Result]:
    out = [
        _check_path(root / "profiles.json", required=True, parses_json=True,
                    what="no profile can be loaded, so NOTHING is scraped."),
        _check_path(root / "warehouses.json", required=False, parses_json=True,
                    what="every address tags Unclassified, so no order routes to a buying group."),
        _check_path(root / "cards.json", required=False, parses_json=True,
                    what="every row falls back to DEFAULT_CASHBACK_RATE, misstating profit."),
    ]

    # The service-account key is env-configurable, so resolve it the way settings.py does.
    sa = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
    sa_path = Path(sa) if Path(sa).is_absolute() else root / sa
    out.append(_check_path(sa_path, required=True, parses_json=True,
                           what="the Sheet cannot be read or written, so every run fails at sync."))
    return out


def check_costco_tokens(root: Path = ROOT) -> list[Result]:
    """Costco's API path needs a stored refresh token per profile, or it silently uses the agent."""
    try:
        from config.profiles import load_profiles_for_retailer
        profiles = load_profiles_for_retailer("costco")
    except Exception as exc:  # noqa: BLE001 — a broken profiles.json is already reported above
        return [Result(WARN, "costco token", f"could not read profiles ({exc}); skipped.")]

    if not profiles:
        return [Result(OK, "costco token", "no profile lists costco; nothing to check.")]

    out = []
    for profile in profiles:
        token = root / ".costco" / f"{profile.label}.json"
        if token.is_file():
            # Present is not enough: Costco ROTATES the refresh token on every refresh and
            # _save_auth persists the new one. A read-only mount (the natural-looking `:ro` for a
            # file full of secrets) turns that write into an OSError, so Costco degrades to the
            # PAID agent on every run AND the rotated token is thrown away, which can strand the
            # stored one. Probe the directory rather than trusting permission bits, since a
            # container running as root reads as writable right up until the mount refuses.
            probe = token.parent / ".preflight_write_probe"
            try:
                probe.touch()
                probe.unlink()
            except OSError as exc:
                out.append(Result(
                    FAIL, f"costco token [{profile.label}]",
                    f"present but its directory is NOT WRITABLE ({exc.strerror}). Costco rotates "
                    f"its refresh token and must save the new one, so this degrades Costco to the "
                    f"PAID agent every run and discards the rotation. In Docker, drop the `:ro` "
                    f"from the ./.costco volume in docker-compose.yml.",
                ))
            else:
                out.append(Result(OK, f"costco token [{profile.label}]", "present and writable"))
        else:
            out.append(Result(
                FAIL, f"costco token [{profile.label}]",
                f"{token} is missing — Costco falls back to the PAID agent every run. Fix with "
                f"`python -m scripts.costco_token --label {profile.label} --token '<REFRESH_TOKEN>'`, "
                f"and check the ./.costco volume is mounted if this is a container.",
            ))
    return out


# Env vars with no safe default: without them a run either cannot start or cannot record anything.
REQUIRED_ENV = {
    "BROWSER_USE_API_KEY": "no cloud browser can be created, so no retailer can be read.",
    "GOOGLE_SHEET_ID": "there is no ledger to write to; every run fails at sync.",
}


def check_env() -> list[Result]:
    out = [
        Result(FAIL, name, f"is unset — {why}") if not (os.getenv(name) or "").strip()
        else Result(OK, name, "set")
        for name, why in REQUIRED_ENV.items()
    ]

    # Alerts are how an unattended host tells you anything at all. Neither channel configured means
    # a silent failure stays silent until you happen to look at the sheet.
    email = (os.getenv("GMAIL_ADDRESS") or "").strip() and (os.getenv("GMAIL_APP_PASSWORD") or "").strip()
    discord = (os.getenv("DISCORD_WEBHOOK_URL") or "").strip()
    if email or discord:
        channels = ", ".join(c for c, on in (("email", email), ("Discord", discord)) if on)
        out.append(Result(OK, "alerts", f"configured ({channels})"))
    else:
        out.append(Result(
            WARN, "alerts",
            "neither Gmail nor Discord is configured. On an unattended host this means a logged-out "
            "session or a failing run reports to nobody. Test with `python -m alerts.notifier`.",
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
            FAIL, ".env values",
            f"config/settings.py could not be loaded: {type(exc).__name__}: {exc}. Every run fails "
            f"at import until this is fixed.",
        )]

    if not settings.buying_group_sync_enabled:
        return [Result(WARN, "buying-group sync", "DISABLED — tracking numbers are not submitted and "
                                                  "no payout is read back (set BUYING_GROUP_SYNC_ENABLED=1).")]

    out = [Result(OK, "buying-group sync", "ENABLED — every run submits tracking and files BFMR "
                                           "insurance unattended, spending real money.")]
    if (settings.maxoutdeals_api_key or "").strip():
        out.append(Result(
            WARN, "MaxOutDeals IP allowlist",
            "MOD rejects any call from an unregistered IP, whatever the token. This host's public IP "
            "must be added under the firewall tab in your MOD profile — re-check it after ANY move "
            "to a new machine, ISP or container host.",
        ))
    return out


def check_receipt_capture() -> list[Result]:
    """Receipt capture is optional, so the failure worth catching is a PARTIAL config.

    Fully unconfigured is fine and reported as such — orders record with a blank Receipt Link. But a
    bucket set without a PAR prefix (or without boto3 installed) is the silent case: capture looks
    switched on, and either every run pays to render receipts it cannot link, or the whole feature
    no-ops while the operator believes it is working.

    Config-presence only — no bucket call, no network. Preflight runs on every container start and
    has to stay offline and free.
    """
    try:
        from config.settings import settings
        from receipts.store import is_configured, missing_settings
    except Exception as exc:  # noqa: BLE001 — a broken settings import is already reported elsewhere
        return [Result(WARN, "receipt capture", f"could not be checked ({exc}).")]

    if not settings.receipt_capture_enabled:
        return [Result(WARN, "receipt capture",
                       "DISABLED (RECEIPT_CAPTURE_ENABLED) — no order receipts are stored.")]

    missing = missing_settings()
    if len(missing) == len(["OCI_BUCKET", "OCI_S3_ENDPOINT_URL", "OCI_S3_ACCESS_KEY_ID",
                            "OCI_S3_SECRET_ACCESS_KEY", "OCI_PAR_URL_PREFIX"]):
        return [Result(WARN, "receipt capture",
                       "not configured — orders record normally with a blank Receipt Link. Set the "
                       "OCI_* values in .env to store proof of purchase for each order.")]
    if missing:
        return [Result(
            FAIL, "receipt capture",
            f"PARTIALLY configured — {', '.join(missing)} unset. Capture stays off, so every order "
            f"records with a blank Receipt Link while the config reads as if it were on.",
        )]

    out = [Result(OK, "receipt capture", f"bucket {settings.oci_bucket!r} via the S3 compat endpoint")]
    try:
        importlib.import_module("boto3")
    except Exception as exc:  # noqa: BLE001
        out.append(Result(
            FAIL, "import boto3",
            f"{type(exc).__name__}: {exc} — receipt capture is configured but nothing can be "
            f"uploaded (`pip install -r requirements.txt`). Orders still record.",
        ))
    else:
        out.append(Result(OK, "import boto3", "receipt uploads"))
    if not is_configured():  # belt and braces: the settings agree, so this should be unreachable
        out.append(Result(WARN, "receipt capture", "settings look complete but the store reports "
                                                   "itself unconfigured; check for stray whitespace."))

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
    raw = (os.getenv("RUN_INTERVAL_HOURS") or "").strip()
    if not raw:
        # Not a container deployment (the native cron path takes its interval as an argument to
        # scripts/install_cron.sh, which does this same check itself).
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
        f"payouts, so Payout Amount / Insurance / paid status stop updating until the daily reset "
        f"(tracking submission is unaffected). A manual `sync_tracking` spends one too, even as a "
        f"DRY RUN. Use {-(-24 // limit)}h or longer to stay inside the quota.",
    )]


def run_checks() -> list[Result]:
    results: list[Result] = []
    results += check_deterministic_imports()
    results += check_config_files()
    results += check_env()
    results += check_costco_tokens()
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
