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


settings = Settings()
