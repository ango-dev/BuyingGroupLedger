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
    "GOOGLE_SERVICE_ACCOUNT_FILE": "google.service_account_file",
    "GOOGLE_SHEET_ID": "google.sheet_id",
    "GOOGLE_SHEET_WORKSHEET_NAME": "google.worksheet_name",
    "GMAIL_ADDRESS": "alerts.gmail_address",
    "GMAIL_APP_PASSWORD": "alerts.gmail_app_password",
    "ALERT_EMAIL_TO": "alerts.email_to",
    "DISCORD_WEBHOOK_URL": "alerts.discord_webhook_url",
    "BUYING_GROUP_SYNC_ENABLED": "buying_groups.sync_enabled",
    "BFMR_API_BASE_URL": "buying_groups.bfmr.api_base_url",
    "BFMR_API_KEY": "buying_groups.bfmr.api_key",
    "BFMR_API_SECRET": "buying_groups.bfmr.api_secret",
    "BFMR_MIN_INSURANCE_VALUE": "buying_groups.bfmr.min_insurance_value",
    "MAXOUTDEALS_API_BASE_URL": "buying_groups.mod.api_base_url",
    "MAXOUTDEALS_API_KEY": "buying_groups.mod.api_key",
    "MAXOUTDEALS_USER_ID": "buying_groups.mod.user_id",
    "MAXOUTDEALS_EMAIL": "buying_groups.mod.email",
    "RECEIPT_CAPTURE_ENABLED": "receipts.capture_enabled",
    "OCI_BUCKET": "receipts.oci.bucket",
    "OCI_S3_ENDPOINT_URL": "receipts.oci.s3_endpoint_url",
    "OCI_S3_REGION": "receipts.oci.s3_region",
    "OCI_S3_ACCESS_KEY_ID": "receipts.oci.s3_access_key_id",
    "OCI_S3_SECRET_ACCESS_KEY": "receipts.oci.s3_secret_access_key",
    "OCI_PAR_URL_PREFIX": "receipts.oci.par_url_prefix",
    "DOSSIER_UPLOAD_ENABLED": "receipts.dossier_upload_enabled",
    "OCI_FAILURES_PAR_URL_PREFIX": "receipts.oci.failures_par_url_prefix",
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
    number on the sheet, so the "%" suffix is honoured explicitly rather than guessed at. A bare
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

    google_service_account_file: str = _get_str(
        "GOOGLE_SERVICE_ACCOUNT_FILE", "")
    google_sheet_id: str = _get_str("GOOGLE_SHEET_ID")
    google_sheet_worksheet_name: str = _get_str(
        "GOOGLE_SHEET_WORKSHEET_NAME", "Orders")

    gmail_address: str = _get_str("GMAIL_ADDRESS")
    gmail_app_password: str = field(
        default=_get_str("GMAIL_APP_PASSWORD"), repr=False)
    alert_email_to: str = (_get_str("ALERT_EMAIL_TO")
                           or _get_str("GMAIL_ADDRESS"))

    discord_webhook_url: str = field(
        default=_get_str("DISCORD_WEBHOOK_URL"), repr=False)

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

    # MaxOutDeals authenticates with a bearer token AND an IP allowlist (its profile has a firewall
    # tab). `user` and `email` are required in the BODY of every request, not just the headers.
    maxoutdeals_api_base_url: str = _get_str(
        "MAXOUTDEALS_API_BASE_URL", "https://www.maxoutdeals.com")
    maxoutdeals_api_key: str = field(
        default=_get_str("MAXOUTDEALS_API_KEY"), repr=False)
    maxoutdeals_user_id: str = _get_str("MAXOUTDEALS_USER_ID")
    maxoutdeals_email: str = _get_str("MAXOUTDEALS_EMAIL")

    # --- Receipt capture -> OCI Object Storage (see receipts/) ----------------------------------
    # Uploads go through OCI's S3 COMPATIBILITY API, so the credentials are an OCI "customer secret
    # key" (an access-key/secret pair minted under your user), NOT the API signing key.
    #
    # OCI_BUCKET IS THE MASTER SWITCH. Blank = receipt capture is completely inert: no browser is
    # opened, no object is written, the Receipt Link column stays blank, and nothing raises. That is
    # deliberately the default, so a host that has not been given a bucket degrades to "no receipts"
    # rather than failing runs. RECEIPT_CAPTURE_ENABLED is the separate off switch, for turning the
    # feature off WITHOUT deleting working credentials.
    receipt_capture_enabled: bool = _get_bool(
        "RECEIPT_CAPTURE_ENABLED", True)
    oci_bucket: str = _get_str("OCI_BUCKET")
    oci_s3_endpoint_url: str = _get_str("OCI_S3_ENDPOINT_URL")
    oci_s3_region: str = _get_str("OCI_S3_REGION")
    oci_s3_access_key_id: str = field(
        default=_get_str("OCI_S3_ACCESS_KEY_ID"), repr=False)
    oci_s3_secret_access_key: str = field(
        default=_get_str("OCI_S3_SECRET_ACCESS_KEY"),
        repr=False)
    # A Pre-Authenticated Request URL, created ONCE by hand in the OCI console — Target: Bucket,
    # Access type: Permit object reads, object listing left OFF. PARs are NOT part of the S3
    # compatibility API — minting one needs OCI's native API and a different credential set
    # entirely — and boto3's generate_presigned_url caps at 7 days, which is not long-lived. One
    # manual PAR sidesteps both: every object's link is this prefix + the object key, and revoking
    # it is one click.
    #
    # TREAT IT AS A SECRET. Anyone holding this URL can read every receipt under the prefix, and a
    # receipt carries your name, delivery address, card last 4 and order totals.
    oci_par_url_prefix: str = field(
        default=_get_str("OCI_PAR_URL_PREFIX"), repr=False)
    # Failure dossiers ride the same bucket under `failures/`, so the alert can carry a link instead
    # of a path on a host you then have to SSH into. Same PII class as the receipts already there (a
    # page DOM can hold names and addresses), so the same rule: private bucket, PAR without listing.
    # Turn it OFF if an alert channel is shared with people who should not see order pages. Inert
    # whenever the receipt bucket is unconfigured; a failed upload falls back to the local path.
    dossier_upload_enabled: bool = _get_bool("DOSSIER_UPLOAD_ENABLED", True)
    # A SEPARATE PAR for the dossiers, scoped to the `failures/` prefix (Target: Objects with prefix
    # `failures/`, Permit object reads, listing OFF). Kept apart from the receipt PAR on purpose: the
    # two can be revoked independently, and the receipt PAR stays scoped to receipts. Blank = dossiers
    # are not uploaded (the alert names the local path). Either the `/o` form or the full
    # `/o/failures/` form the console hands out is accepted. Treat as a secret, like the other PAR.
    oci_failures_par_url_prefix: str = field(
        default=_get_str("OCI_FAILURES_PAR_URL_PREFIX"), repr=False)

    # --- container scheduling (read by docker/entrypoint.sh) --------------------------------------
    # How many hours between scheduled runs. Each run spends one of MaxOutDeals' 10 daily
    # received-items calls, so preflight warns when the interval implies more than that.
    container_run_interval_hours: int = _get_int("RUN_INTERVAL_HOURS", 6)
    container_run_on_start: bool = _get_bool("RUN_ON_START", False)
    # Refuse to start when preflight fails, instead of alerting and running degraded.
    container_preflight_strict: bool = _get_bool("PREFLIGHT_STRICT", False)
    container_timezone: str = _get_str("TZ", "UTC")

    def google_credentials(self, scopes):
        """Google service-account credentials, from the config file or a standalone JSON file.

        The credential is normally INLINED into config.json under `google.service_account` — it is
        the one credential Google issues as a whole JSON object rather than a string, and keeping it
        inline is what makes config.json genuinely self-contained.

        `GOOGLE_SERVICE_ACCOUNT_FILE` still points at a standalone file and still WINS when set, so
        an existing deployment keeps working and anyone who would rather mount the file Google hands
        them can. Same override rule as every other setting.

        Imported lazily: google.oauth2 is a heavy import, and this module is pulled in by nearly
        everything — including offline paths that never touch a sheet.
        """
        from google.oauth2.service_account import Credentials

        if self.google_service_account_file:
            return Credentials.from_service_account_file(
                self.google_service_account_file, scopes=scopes
            )
        info = config_value("google.service_account")
        if isinstance(info, dict) and info:
            return Credentials.from_service_account_info(info, scopes=scopes)
        raise ValueError(
            "No Google credentials. Put the service-account JSON object Google gave you under "
            '`google.service_account` in config.json, or set GOOGLE_SERVICE_ACCOUNT_FILE to a path.'
        )



settings = Settings()
