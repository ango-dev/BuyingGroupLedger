import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


def _get_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value else default


def _get_bool(name: str, default: bool) -> bool:
    """Read a flag. Only the affirmative spellings are true — anything else, including a typo, is
    false. A switch that guards spending money should fail closed."""
    value = (os.getenv(name) or "").strip().lower()
    return value in {"1", "true", "yes", "on"} if value else default


def _get_rate(name: str, default: float) -> float:
    """Read a rate that may be written either as a decimal fraction (0.02) or a percentage ("2%").

    Both spellings are natural in a .env, and getting it wrong by 100x would misstate every profit
    number on the sheet, so the "%" suffix is honoured explicitly rather than guessed at. A bare
    value > 1 is rejected for the same reason models.card.Card rejects it: "2" is equally readable
    as 2% or 200%.
    """
    value = (os.getenv(name) or "").strip()
    if not value:
        return default
    rate = float(value[:-1].strip()) / 100 if value.endswith("%") else float(value)
    if not 0 <= rate <= 1:
        raise ValueError(
            f"{name}={value!r} is outside 0-1. Write 2% as either 0.02 or \"2%\", not 2."
        )
    return rate


@dataclass(frozen=True)
class Settings:
    # Browser-Use Cloud v4 (browser_use_sdk.v4.BrowserUse reads BROWSER_USE_API_KEY itself).
    # v4 is BYOK-only. The SDK's RunModel enum is stale/incomplete — it doesn't list
    # "gpt-5.6-luna", but the server accepts it and it's the cheapest confirmed-working
    # option ($0.24/$1.44 per 1M tokens). Other verified-working values: minimax-m3,
    # gemini-3-flash, gemini-3.5-flash, grok-4.5, gpt-5.5/5.6, claude-sonnet-5,
    # claude-opus-4.7/4.8, glm-5.2 — but since the model list isn't authoritative, other
    # unlisted names may also work; verify with a cheap test run before trusting one.
    browser_use_llm: str = os.getenv("BROWSER_USE_LLM", "gpt-5.6-luna")
    # Per-run cost circuit breaker (v4's max_cost_usd) — stops a run that's spiraling on retries.
    browser_use_max_cost_usd: float = _get_float("BROWSER_USE_MAX_COST_USD", 0.50)

    # How many CALENDAR days back to include, counting today as 0. Default 1 = "today and
    # yesterday". Retailers expose only an order date (no time), and the agent gets confused
    # reasoning about a rolling 24h clock, so the window is date-based and the exact cutoff date
    # is handed to the agent (see BaseRetailerScraper._date_window).
    lookback_days: int = _get_int("LOOKBACK_DAYS", 1)

    # Cashback rate applied to a row whose card isn't listed in cards.json (or is listed without its
    # own rate) — a decimal fraction, so 0.02 = 2%; "2%" is also accepted. Per-card overrides live in
    # cards.json (see config/cards.py). 0 = assume no cashback unless a card says otherwise.
    default_cashback_rate: float = _get_rate("DEFAULT_CASHBACK_RATE", 0.0)

    google_service_account_file: str = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
    google_sheet_id: str = os.getenv("GOOGLE_SHEET_ID", "")
    google_sheet_worksheet_name: str = os.getenv("GOOGLE_SHEET_WORKSHEET_NAME", "Orders")

    gmail_address: str = os.getenv("GMAIL_ADDRESS", "")
    gmail_app_password: str = os.getenv("GMAIL_APP_PASSWORD", "")
    alert_email_to: str = os.getenv("ALERT_EMAIL_TO", "") or os.getenv("GMAIL_ADDRESS", "")

    discord_webhook_url: str = os.getenv("DISCORD_WEBHOOK_URL", "")

    # --- Buying groups (see buying_groups/) -----------------------------------------------------
    # Master switch for the scheduled buying-group sync in main.run_buying_group_sync. OFF by
    # default because that path submits to third parties and files BFMR insurance, which spends
    # real money per shipment — it should only run unattended after a manual dry run and a
    # one-package live test have been done. `python -m sync_tracking` ignores this: an explicit
    # command is already an explicit decision.
    buying_group_sync_enabled: bool = _get_bool("BUYING_GROUP_SYNC_ENABLED", False)

    # These were the last credentials in the repo still read by a bare os.getenv() inside the client
    # itself, which also meant they depended on something else having imported this module first to
    # get load_dotenv() called. They're read here like every other credential now.

    # BFMR authenticates with TWO headers, API-KEY and API-SECRET — not a bearer token. Spec:
    # https://api.bfmr.com/storage/api-docs.json (the page at https://api.bfmr.com/ just renders it).
    bfmr_api_base_url: str = os.getenv("BFMR_API_BASE_URL", "https://api.bfmr.com")
    bfmr_api_key: str = os.getenv("BFMR_API_KEY", "")
    bfmr_api_secret: str = os.getenv("BFMR_API_SECRET", "")
    # Only insure shipments worth at least this much. 0 (the default) insures every shipment, which
    # is the intended behaviour — the knob exists so cheap boxes can be excluded later without a
    # code change, since filing costs a real premium on every unattended run.
    bfmr_min_insurance_value: float = _get_float("BFMR_MIN_INSURANCE_VALUE", 0.0)

    # MaxOutDeals authenticates with a bearer token AND an IP allowlist (its profile has a firewall
    # tab). `user` and `email` are required in the BODY of every request, not just the headers.
    maxoutdeals_api_base_url: str = os.getenv("MAXOUTDEALS_API_BASE_URL", "https://www.maxoutdeals.com")
    maxoutdeals_api_key: str = os.getenv("MAXOUTDEALS_API_KEY", "")
    maxoutdeals_user_id: str = os.getenv("MAXOUTDEALS_USER_ID", "")
    maxoutdeals_email: str = os.getenv("MAXOUTDEALS_EMAIL", "")

    # --- Receipt capture -> OCI Object Storage (see receipts/) ----------------------------------
    # Uploads go through OCI's S3 COMPATIBILITY API, so the credentials are an OCI "customer secret
    # key" (an access-key/secret pair minted under your user), NOT the API signing key.
    #
    # OCI_BUCKET IS THE MASTER SWITCH. Blank = receipt capture is completely inert: no browser is
    # opened, no object is written, the Receipt Link column stays blank, and nothing raises. That is
    # deliberately the default, so a host that has not been given a bucket degrades to "no receipts"
    # rather than failing runs. RECEIPT_CAPTURE_ENABLED is the separate off switch, for turning the
    # feature off WITHOUT deleting working credentials.
    receipt_capture_enabled: bool = _get_bool("RECEIPT_CAPTURE_ENABLED", True)
    oci_bucket: str = os.getenv("OCI_BUCKET", "")
    oci_s3_endpoint_url: str = os.getenv("OCI_S3_ENDPOINT_URL", "")
    oci_s3_region: str = os.getenv("OCI_S3_REGION", "")
    oci_s3_access_key_id: str = os.getenv("OCI_S3_ACCESS_KEY_ID", "")
    oci_s3_secret_access_key: str = os.getenv("OCI_S3_SECRET_ACCESS_KEY", "")
    # A Pre-Authenticated Request URL, created ONCE by hand in the OCI console — Target: Bucket,
    # Access type: Permit object reads, object listing left OFF. PARs are NOT part of the S3
    # compatibility API — minting one needs OCI's native API and a different credential set
    # entirely — and boto3's generate_presigned_url caps at 7 days, which is not long-lived. One
    # manual PAR sidesteps both: every object's link is this prefix + the object key, and revoking
    # it is one click.
    #
    # TREAT IT AS A SECRET. Anyone holding this URL can read every receipt under the prefix, and a
    # receipt carries your name, delivery address, card last 4 and order totals.
    oci_par_url_prefix: str = os.getenv("OCI_PAR_URL_PREFIX", "")


settings = Settings()
