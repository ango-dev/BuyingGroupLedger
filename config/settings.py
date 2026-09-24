"""Every setting the run needs, resolved from one config file with environment overrides.

RESOLUTION ORDER, for every value below:

    environment variable  ->  config.json  ->  the default written here

The environment always wins, and it reaches us from either `.env` (loaded below) or the shell. That
ordering is what lets `docker-compose.yml`'s `env_file:` and every variable named in DEPLOY.md keep
working after the config was consolidated, and what makes a deliberate one-off possible —
`BFMR_MIN_INSURANCE_VALUE=999 python -m sync_tracking` — without editing the file you author.

Each field names BOTH its environment variable and its dotted path in config.json, so the mapping is
readable in one place rather than derived by a naming rule. Explicit on purpose: the variable names
are published in DEPLOY.md, docker-compose.yml and the README, and a derived scheme would silently
rename them the first time a config path moved.

NOT EVERYTHING BELONGS HERE. One-off ops values (a temporary `LOOKBACK_DAYS`, a host-specific
`RUN_INTERVAL_HOURS`) belong in `.env` or on the command line, not written into the file everything
else shares.
"""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

from config.loader import config_value

load_dotenv()


def _export_sdk_env() -> None:
    """Put config-file values into the environment for libraries that read it themselves.

    The Browser-Use SDK reads `BROWSER_USE_API_KEY` out of `os.environ` on its own — we never hand it
    over — so a key living only in config.json would be invisible to it, and config.json alone would
    not be a complete setup. Exporting closes that gap.

    Never overwrites an existing variable: the environment outranks the file everywhere else, and
    this is the one place where getting that backwards would be silent rather than obvious.
    """
    for name in ("BROWSER_USE_API_KEY",):
        if not (os.getenv(name) or "").strip():
            value = config_value(ENV_TO_CONFIG[name])
            if value:
                os.environ[name] = str(value)


#: The one mapping from environment variable to its place in config.json.
#:
#: SINGLE SOURCE, because two things depend on it and they must not drift: the settings below resolve
#: through it, and scripts/migrate_config.py writes the config file using it. A variable missing here
#: raises KeyError the moment this module is imported, which is as loud and as early as a config
#: mistake can be made to fail.
#:
#: The names are the published interface — DEPLOY.md, docker-compose.yml and the README all quote
#: them — so they are spelled out rather than derived from the config path by a rule that would
#: silently rename one the first time a path moved.
ENV_TO_CONFIG = {
    "BROWSER_USE_API_KEY": "browser_use.api_key",
    # Container/scheduling knobs. Read by docker/entrypoint.sh, which resolves them through
    # scripts/container_settings.py rather than reading the environment itself — otherwise
    # docker-compose would interpolate them before Python ever runs and the config file could not
    # reach them.
    "RUN_INTERVAL_HOURS": "container.run_interval_hours",
    "RUN_ON_START": "container.run_on_start",
    "PREFLIGHT_STRICT": "container.preflight_strict",
    "TZ": "container.timezone",
    "LOOKBACK_DAYS": "scraping.lookback_days",
    "DEFAULT_CASHBACK_RATE": "scraping.default_cashback_rate",
    "AMAZON_PROMO_CASHBACK_ENABLED": "scraping.amazon_promo_cashback_enabled",
    "AMAZON_GIFT_CARD_NETTING_ENABLED": "scraping.amazon_gift_card_netting_enabled",
    # Two alert channels, each with its own switch. A channel sends when it is ON and configured.
    "DISCORD_ALERTS_ENABLED": "alerts.discord_enabled",
    "DISCORD_WEBHOOK_URL": "alerts.discord_webhook_url",
    "GMAIL_ALERTS_ENABLED": "alerts.gmail_enabled",
    "GMAIL_ADDRESS": "alerts.gmail_address",
    "GMAIL_APP_PASSWORD": "alerts.gmail_app_password",
    "ALERT_EMAIL_TO": "alerts.email_to",
    "CASHBACK_CAP_WARN_PERCENT": "alerts.cap_warn_percent",
    "CASHBACK_CAP_WARN_DOLLARS": "alerts.cap_warn_dollars",
    "BUYING_GROUP_SYNC_ENABLED": "buying_groups.sync_enabled",
    "BFMR_ENABLED": "buying_groups.bfmr.enabled",
    "BFMR_API_BASE_URL": "buying_groups.bfmr.api_base_url",
    "BFMR_API_KEY": "buying_groups.bfmr.api_key",
    "BFMR_API_SECRET": "buying_groups.bfmr.api_secret",
    "BFMR_MIN_INSURANCE_VALUE": "buying_groups.bfmr.min_insurance_value",
    "BFMR_COSTCO_TV_ORDER_NUMBER_AS_TRACKING": "buying_groups.bfmr.costco_tv_order_number_as_tracking",
    "BFMR_COSTCO_TV_ITEM_PATTERN": "buying_groups.bfmr.costco_tv_item_pattern",
    "BFMR_COMBINED_PACKAGE_AUTOREPLY_ENABLED": "buying_groups.bfmr.combined_package_autoreply_enabled",
    "BFMR_COMBINED_PACKAGE_SENDER_DOMAINS": "buying_groups.bfmr.combined_package_sender_domains",
    "BFMR_COMBINED_PACKAGE_REPLY_CC": "buying_groups.bfmr.combined_package_reply_cc",
    "BFMR_COMBINED_PACKAGE_GMAIL_ADDRESS": "buying_groups.bfmr.combined_package_gmail_address",
    "BFMR_COMBINED_PACKAGE_GMAIL_APP_PASSWORD": "buying_groups.bfmr.combined_package_gmail_app_password",
    "MAXOUTDEALS_ENABLED": "buying_groups.mod.enabled",
    "MAXOUTDEALS_API_BASE_URL": "buying_groups.mod.api_base_url",
    "MAXOUTDEALS_API_KEY": "buying_groups.mod.api_key",
    "MAXOUTDEALS_USER_ID": "buying_groups.mod.user_id",
    "MAXOUTDEALS_EMAIL": "buying_groups.mod.email",
    "RECEIPT_CAPTURE_ENABLED": "receipts.capture_enabled",
    # Where the receipt files live (receipts/store.py): a directory beside the ledger, inside every
    # backup, served by the dashboard at /receipts/. OCI Object Storage was removed 2026-09-18.
    "RECEIPTS_DIR": "receipts.dir",
    # The web dashboard (web/). None of these is read by the scheduler; see
    # docs/operations.md, "The web dashboard".
    "WEB_ENABLED": "web.enabled",
    "WEB_LEDGER_SOURCE": "web.ledger_source",
    "WEB_SNAPSHOT_PATH": "web.snapshot_path",
    "WEB_LEDGER_CACHE_TTL_SECONDS": "web.ledger_cache_ttl_seconds",
    "WEB_BIND_HOST": "web.bind_host",
    "WEB_PORT": "web.port",
    # How long the Tools page keeps a profile-login browser session open after the user walks
    # away before closing it (closing = saving the cookies).
    "WEB_TOOL_SESSION_MINUTES": "web.tool_session_minutes",
    "WEB_HEARTBEAT_STALE_HOURS": "web.heartbeat_stale_hours",
    # Where YOU open the dashboard from (e.g. over WireGuard): alerts link a failure dossier to its
    # Activity page through it. Blank = alerts name the local path only.
    "WEB_PUBLIC_URL": "web.public_url",
    # Host names the dashboard answers to besides an IP address, localhost and public_url's host
    # (web/guard.py: a name nobody listed is refused, which stops DNS rebinding).
    "WEB_ALLOWED_HOSTS": "web.allowed_hosts",
    # The dashboard's sign-in (web/auth.py): a password (blank = no sign-in),
    # how long a plain sign-in and a "remember me" one last, and the brake on guessing.
    "WEB_PASSWORD": "web.password",
    "WEB_SESSION_HOURS": "web.session_hours",
    "WEB_REMEMBER_DAYS": "web.remember_days",
    "WEB_LOGIN_ATTEMPTS": "web.login_attempts",
    "WEB_LOGIN_LOCKOUT_MINUTES": "web.login_lockout_minutes",
    # THE LEDGER: the SQLite file (ledger_db/) every writer and reader runs off.
    "LEDGER_DB_PATH": "database.path",
    # Scheduled backups (scripts/backup.py --scheduled, on the container's own cron): whether,
    # how often, when, on which days, and how many zips to keep. Read at container start.
    "BACKUP_ENABLED": "backups.enabled",
    "BACKUP_FREQUENCY": "backups.frequency",
    "BACKUP_TIME": "backups.time",
    "BACKUP_DAYS": "backups.days",
    "BACKUP_KEEP": "backups.keep",
}


def _config_path(name: str) -> str:
    """Where in config.json this environment variable's value lives."""
    return ENV_TO_CONFIG[name]


def _get_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is not None and value.strip():
        return value
    return str(config_value(_config_path(name), default))


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name) or config_value(_config_path(name))
    return int(value) if value not in (None, "") else default


def _get_float(name: str, default: float) -> float:
    value = os.getenv(name) or config_value(_config_path(name))
    return float(value) if value not in (None, "") else default


#: Which settings are flags, recorded by `_get_bool` itself as `Settings`' field defaults are
#: evaluated at import. Derived rather than hand-listed on purpose: anything that has to be kept in
#: step by hand eventually is not, and the consumers here are a migrator that would write the wrong
#: JSON type and a README table that would document the wrong spelling — both silent.
BOOLEAN_SETTINGS: set[str] = set()


def _get_bool(name: str, default: bool) -> bool:
    """Read a flag. Only the affirmative spellings are true — anything else, including a typo, is
    false. A switch that guards spending money should fail closed.

    WRITE THESE AS `true` / `false`, in config.json and in the environment alike, so a flag reads the
    same in both places and nobody has to remember which way round 1 and 0 went. `1`, `yes` and `on`
    are still accepted so an older script or shell alias keeps working.

    JSON has a real boolean, so config.json can say `true`; an environment variable is always a
    string. Both go through the same affirmative-spellings test, so a typo fails closed in EITHER
    source rather than only in the one that happens to be a string.
    """
    BOOLEAN_SETTINGS.add(name)
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        raw = config_value(_config_path(name))
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _get_rate(name: str, default: float) -> float:
    """Read a rate that may be written either as a decimal fraction (0.02) or a percentage ("2%").

    Both spellings are natural to write, and getting it wrong by 100x would misstate every profit
    number on the ledger, so the "%" suffix is honoured explicitly rather than guessed at. A bare
    value > 1 is rejected for the same reason models.card.Card rejects it: "2" is equally readable
    as 2% or 200%.
    """
    value = os.getenv(name)
    if value is None or not value.strip():
        raw = config_value(_config_path(name))
        value = "" if raw is None else str(raw)
    value = value.strip()
    if not value:
        return default
    rate = float(value[:-1].strip()) / 100 if value.endswith("%") else float(value)
    if not 0 <= rate <= 1:
        raise ValueError(
            f"{name}={value!r} is outside 0-1. Write 2% as either 0.02 or \"2%\", not 2."
        )
    return rate


_export_sdk_env()


@dataclass(frozen=True)
class Settings:
    """Every credential the run needs.

    SECRETS ARE `repr=False`. This object is reachable from almost every module, so any unhandled
    exception that carries it into `logs/run.log` — or into an alert email, which is worse, since
    that leaves the host — would otherwise print every API key, the Best Buy password and the PAR
    URL in full. Found 2026-08-21 when a test traceback printed the PAR secret verbatim.
    A masked repr costs nothing; the values are still read normally as attributes.
    """

    # The Browser-Use SDK reads this out of os.environ itself (_export_sdk_env above puts the
    # config value there); the field exists so the setting is visible where every other one is --
    # the browser Settings page derives its form from these fields (web/settings_form.py).
    browser_use_api_key: str = field(default=_get_str("BROWSER_USE_API_KEY"), repr=False)

    # How many CALENDAR days back to include, counting today as 0. Default 1 = "today and
    # yesterday". Retailers expose only an order date (no time), so the window is date-based
    # (see BaseRetailerScraper._date_window).
    lookback_days: int = _get_int("LOOKBACK_DAYS", 1)

    # Cashback rate applied to a row whose card isn't listed in cards.json (or is listed without its
    # own rate) — a decimal fraction, so 0.02 = 2%; "2%" is also accepted. Per-card overrides live in
    # cards.json (see config/cards.py). 0 = assume no cashback unless a card says otherwise.
    default_cashback_rate: float = _get_rate(
        "DEFAULT_CASHBACK_RATE", 0.0)

    # Amazon prints the paying card's earn line under the payment method, e.g. "Earn 5% back (cap
    # applies) plus an extra 1% back on select items". ON = that EXTRA percentage is added to the
    # card's cards.json rate for that order (Amazon AND Amazon Business). Turn it OFF to fall back to
    # the cards.json rate alone — the escape hatch if that text turns out to be card-level marketing
    # rather than a per-order promo, since it would then inflate every order on that card.
    amazon_promo_cashback_enabled: bool = _get_bool(
        "AMAZON_PROMO_CASHBACK_ENABLED", True)
    # A gift card earns NO cashback, so when one pays part of an Amazon order the recorded cost is
    # scaled down to what the card actually paid — which makes both Total Cost and the cashback it
    # drives reflect card spend only. Applies to Amazon AND Amazon Business. OFF = record the full
    # sticker cost (and thus cashback on money the card never spent).
    amazon_gift_card_netting_enabled: bool = _get_bool(
        "AMAZON_GIFT_CARD_NETTING_ENABLED", True)

    # Discord: a webhook, and a switch. OFF keeps the URL and sends nothing there.
    discord_alerts_enabled: bool = _get_bool("DISCORD_ALERTS_ENABLED", True)
    discord_webhook_url: str = field(
        default=_get_str("DISCORD_WEBHOOK_URL"), repr=False)

    # Gmail: the account alerts are sent FROM (an app password, not the account password), who
    # they go to (defaults to the same address), and a switch. This account is ONLY for alerts:
    # the BFMR combined-package auto-reply has its own pair below and never borrows this one.
    gmail_alerts_enabled: bool = _get_bool("GMAIL_ALERTS_ENABLED", True)
    gmail_address: str = _get_str("GMAIL_ADDRESS")
    gmail_app_password: str = field(
        default=_get_str("GMAIL_APP_PASSWORD"), repr=False)
    alert_email_to: str = (_get_str("ALERT_EMAIL_TO")
                           or _get_str("GMAIL_ADDRESS"))
    # A card's spend cap getting close: alert -- and a card on the overview --
    # once this much of a limit is spent in the period, or once this little is left. Either
    # triggers; 0 switches that one off. Alerted once per state (close, reached) per period.
    cap_warn_percent: int = _get_int("CASHBACK_CAP_WARN_PERCENT", 80)
    cap_warn_dollars: float = _get_float("CASHBACK_CAP_WARN_DOLLARS", 0)

    # --- Buying groups (see buying_groups/) -----------------------------------------------------
    # Master switch for the scheduled buying-group sync in main.run_buying_group_sync. OFF by
    # default because that path submits to third parties and files BFMR insurance, which spends
    # real money per shipment — it should only run unattended after a manual dry run and a
    # one-package live test have been done. `python -m sync_tracking` ignores this: an explicit
    # command is already an explicit decision.
    buying_group_sync_enabled: bool = _get_bool(
        "BUYING_GROUP_SYNC_ENABLED", False)

    # These were the last credentials in the repo still read by a bare os.getenv() inside the client
    # itself, which also meant they depended on something else having imported this module first to
    # get load_dotenv() called. They're read here like every other credential now.

    # BFMR authenticates with TWO headers, API-KEY and API-SECRET — not a bearer token. Spec:
    # https://api.bfmr.com/storage/api-docs.json (the page at https://api.bfmr.com/ just renders it).
    # Each group's own switch under the sync's: off, the sync leaves that group alone -- nothing submitted,
    # insured or read back for it -- while the other runs. `--group` on the command line overrides.
    bfmr_enabled: bool = _get_bool("BFMR_ENABLED", True)
    mod_enabled: bool = _get_bool("MAXOUTDEALS_ENABLED", True)
    bfmr_api_base_url: str = _get_str(
        "BFMR_API_BASE_URL", "https://api.bfmr.com")
    bfmr_api_key: str = field(
        default=_get_str("BFMR_API_KEY"), repr=False)
    bfmr_api_secret: str = field(
        default=_get_str("BFMR_API_SECRET"), repr=False)
    # Only insure shipments worth at least this much. 0 (the default) insures every shipment, which
    # is the intended behaviour — the knob exists so cheap boxes can be excluded later without a
    # code change, since filing costs a real premium on every unattended run.
    bfmr_min_insurance_value: float = _get_float(
        "BFMR_MIN_INSURANCE_VALUE", 0.0)
    # --- BFMR combined-package auto-reply (respond_bfmr.py) -------------------------------------
    # Answers ONE kind of email: BFMR's "Combined Best Buy Package <tracking>" request for the
    # units' serial numbers and the receipt PDF. THE MAILBOX IS ITS OWN SETTING PAIR
    # (combined_package_gmail_address / _app_password below): requests are read over IMAP and
    # replies sent over SMTP with those credentials, and `bfmr_reply_account()` is the one place
    # they are resolved. It does NOT fall back to the alerts account.
    #
    # Master switch for the scheduled run (main.py -> respond_bfmr.run). OFF by default for the
    # same reason as sync_enabled: it sends outward-facing mail to a third party unattended, and
    # should only run after a manual dry run and a supervised first send. `python -m
    # respond_bfmr` ignores this — an explicit command is already an explicit decision.
    # BFMR's rule for Costco TVs: the ORDER NUMBER is the tracking number --
    # freight TVs carry no carrier number BFMR can use. On, a Costco row routed to BFMR whose item
    # name matches the pattern carries its order number as Tracking Number from `ordered` on, so
    # the sync submits and insures it the run after it is ordered, and keeps that identity when
    # Costco later reports a carrier number (the row's progress still follows Costco's packages).
    bfmr_costco_tv_order_number_as_tracking: bool = _get_bool(
        "BFMR_COSTCO_TV_ORDER_NUMBER_AS_TRACKING", True)
    # Which Costco item names count as a TV for that rule -- a regular expression, case-insensitive.
    bfmr_costco_tv_item_pattern: str = _get_str("BFMR_COSTCO_TV_ITEM_PATTERN", r"\bTV\b")
    bfmr_combined_package_autoreply_enabled: bool = _get_bool(
        "BFMR_COMBINED_PACKAGE_AUTOREPLY_ENABLED", False)
    # Which From-domains count as BFMR when scanning that inbox for combined-package requests.
    # They arrive from support@/deals@buyformeretail.com (observed 2026-09-11); bfmr.com is kept
    # as the brand's other domain. Comma-separated. Never matched on subject — this repo's own
    # "ACTION NEEDED" alerts land in the same inbox.
    bfmr_combined_package_sender_domains: str = _get_str(
        "BFMR_COMBINED_PACKAGE_SENDER_DOMAINS", "buyformeretail.com,bfmr.com")
    # Optional Cc on every combined-package reply. Useful when BFMR addresses the requests to a
    # DIFFERENT account than the alerts Gmail that answers them (live case: requests go to the
    # main address and forward to the track/alerts inbox) — without the Cc, the reply is only
    # visible in the alerts account's Sent Mail. Blank = none.
    bfmr_combined_package_reply_cc: str = _get_str("BFMR_COMBINED_PACKAGE_REPLY_CC", "")
    # The Gmail account for the combined-package mailbox: the inbox BFMR's requests actually
    # arrive in, with its own app password. Required for the auto-reply to run at all; setting
    # only ONE of the pair is a loud error — mixing one account's address with another's
    # password can never be what was meant.
    bfmr_combined_package_gmail_address: str = _get_str(
        "BFMR_COMBINED_PACKAGE_GMAIL_ADDRESS", "")
    bfmr_combined_package_gmail_app_password: str = field(
        default=_get_str("BFMR_COMBINED_PACKAGE_GMAIL_APP_PASSWORD", ""), repr=False)

    def bfmr_reply_account(self) -> tuple[str, str]:
        """(address, app password) the combined-package auto-reply signs in with — resolved in
        ONE place so IMAP reading and SMTP sending can never disagree about the mailbox. The
        alerts account is never used here, even when it is the same account."""
        address = self.bfmr_combined_package_gmail_address.strip()
        password = self.bfmr_combined_package_gmail_app_password.strip()
        if address and password:
            return address, password
        if address or password:
            raise RuntimeError(
                "buying_groups.bfmr.combined_package_gmail_address and _app_password must be "
                "set TOGETHER — one without the other would mix two accounts' credentials."
            )
        raise RuntimeError(
            "the combined-package auto-reply needs its own Gmail account: set "
            "buying_groups.bfmr.combined_package_gmail_address and _app_password (the alerts "
            "account is not used for it, even if it is the same account)."
        )

    # MaxOutDeals authenticates with a bearer token AND an IP allowlist (its profile has a firewall
    # tab). `user` and `email` are required in the BODY of every request, not just the headers.
    maxoutdeals_api_base_url: str = _get_str(
        "MAXOUTDEALS_API_BASE_URL", "https://www.maxoutdeals.com")
    maxoutdeals_api_key: str = field(
        default=_get_str("MAXOUTDEALS_API_KEY"), repr=False)
    maxoutdeals_user_id: str = _get_str("MAXOUTDEALS_USER_ID")
    maxoutdeals_email: str = _get_str("MAXOUTDEALS_EMAIL")

    # --- Receipt capture (see receipts/) ------------------------------------------------------------
    # Every newly-seen order's receipt is rendered to PDF and kept under `receipts_dir` beside the
    # ledger, linked from Receipt Link with a dashboard-relative URL. The switch keeps the feature
    # off without removing anything; off = orders record with a blank Receipt Link, nothing raises.
    receipt_capture_enabled: bool = _get_bool(
        "RECEIPT_CAPTURE_ENABLED", True)
    # Relative to the repo root (in Docker, the mounted data/ volume, so a rebuild keeps them).
    receipts_dir: str = _get_str("RECEIPTS_DIR", "data/receipts")

    # --- container scheduling (read by docker/entrypoint.sh) --------------------------------------
    # How many hours between scheduled runs. Each run spends one of MaxOutDeals' 10 daily
    # received-items calls, so preflight warns when the interval implies more than that.
    container_run_interval_hours: int = _get_int("RUN_INTERVAL_HOURS", 6)
    container_run_on_start: bool = _get_bool("RUN_ON_START", False)
    # Refuse to start when preflight fails, instead of alerting and running degraded.
    container_preflight_strict: bool = _get_bool("PREFLIGHT_STRICT", False)
    container_timezone: str = _get_str("TZ", "UTC")

    # --- the read-only web dashboard (web/; `python -m web` / the `web` compose profile) ----------
    # Which backend web.ledger_reader serves: "db" (the ledger file, database.path) or "snapshot"
    # (a data/ledger_backup_*.csv -- the newest, or `web_snapshot_path`). The scheduler never reads
    # any of these. Anything else is refused at startup, not defaulted.
    # In the container, docker/entrypoint.sh starts the dashboard beside the scheduler when this is
    # true (and healthcheck.sh probes it). Off = the container is a pure scheduler, as before.
    web_enabled: bool = _get_bool("WEB_ENABLED", True)
    # "db" | "snapshot" -- the first serves data/ledger.sqlite3 (the ledger), re-read from the
    # file on the cache interval below; the second a CSV export, for offline work.
    web_ledger_source: str = _get_str("WEB_LEDGER_SOURCE", "db")
    web_snapshot_path: str = _get_str("WEB_SNAPSHOT_PATH", "")
    # The ledger (ledger_db/). Relative paths are under the repo root; in the container that is
    # the mounted data/ volume, so the file survives a rebuild.
    ledger_db_path: str = _get_str("LEDGER_DB_PATH", "data/ledger.sqlite3")
    # How long the dashboard serves a ledger read from memory before the next request re-reads
    # the file (a cheap local read; the name dates from when the read was remote).
    web_ledger_cache_ttl_seconds: int = _get_int("WEB_LEDGER_CACHE_TTL_SECONDS", 300)
    # Loopback by default. Binding wider (0.0.0.0 behind Tailscale, or the compose service's
    # published port) is a deliberate choice, and one to make with `web_password` set: without a
    # password there is no sign-in at all.
    web_bind_host: str = _get_str("WEB_BIND_HOST", "127.0.0.1")
    web_port: int = _get_int("WEB_PORT", 8765)
    # The Tools page's profile-login session: closed (cookies saved) after this many minutes if
    # the user leaves it open. web/tools.ProfileSessions.
    web_tool_session_minutes: int = _get_int("WEB_TOOL_SESSION_MINUTES", 60)
    # Hours without a completed run before the header's heartbeat pill turns stale. 0 = twice the run interval, the
    # container healthcheck's own rule.
    web_heartbeat_stale_hours: float = _get_float("WEB_HEARTBEAT_STALE_HOURS", 0)
    # The dashboard's address as the user reaches it (e.g. http://192.0.2.10:8765 over WireGuard),
    # for the links alerts carry. Blank = no link, just the path on the host.
    web_public_url: str = _get_str("WEB_PUBLIC_URL", "")
    # Names the dashboard answers to besides an IP address, localhost and public_url's host,
    # comma-separated (".example.com" = any subdomain). Anything else is refused (web/guard.py).
    web_allowed_hosts: str = _get_str("WEB_ALLOWED_HOSTS", "")
    # The sign-in (web/auth.py). Blank password = no sign-in. A plain sign-in ends after
    # `web_session_hours`; one with "remember me" ticked after `web_remember_days` (730 = 2
    # years). `web_login_attempts` wrong passwords in a row from one address lock it out for
    # `web_login_lockout_minutes` (0 = never lock). 
    web_password: str = field(default=_get_str("WEB_PASSWORD"), repr=False)
    web_session_hours: float = _get_float("WEB_SESSION_HOURS", 6)
    web_remember_days: float = _get_float("WEB_REMEMBER_DAYS", 730)
    web_login_attempts: int = _get_int("WEB_LOGIN_ATTEMPTS", 5)
    web_login_lockout_minutes: float = _get_float("WEB_LOGIN_LOCKOUT_MINUTES", 15)

    # --- scheduled backups (docker/entrypoint.sh adds the cron line; scripts/backup.py) ---------
    # A zip of config.json / .state.json / .env / data/ (the ledger) into backups/, on the
    # container's clock: daily | weekly | monthly, at HH:MM, on `days` (weekly: mon,thu; monthly:
    # 1,15; blank = Sunday / the 1st), keeping the newest `keep` (0 = all).
    backups_enabled: bool = _get_bool("BACKUP_ENABLED", True)
    backups_frequency: str = _get_str("BACKUP_FREQUENCY", "daily")
    backups_time: str = _get_str("BACKUP_TIME", "03:30")
    backups_days: str = _get_str("BACKUP_DAYS", "")
    backups_keep: int = _get_int("BACKUP_KEEP", 14)


settings = Settings()
