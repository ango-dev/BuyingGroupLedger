from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, Field


class ProxyConfig(BaseModel):
    host: str
    port: int
    username: str = ""
    password: str = ""

    def as_url(self, scheme: str = "http") -> str:
        """`http://user:pass@host:port` — the form direct HTTP clients want.

        Browser paths hand the fields to Browser-Use separately (see `BaseRetailerScraper.
        _build_browser_settings` and `CdpBrowser`), but a retailer that calls an API over plain HTTP
        (Costco) needs the single-URL form so its traffic leaves from the same static ISP IP the
        browser paths use. Credentials are percent-encoded: a password containing `@` or `:` would
        otherwise split the URL in the wrong place.
        """
        auth = ""
        if self.username:
            auth = quote(self.username, safe="")
            if self.password:
                auth += f":{quote(self.password, safe='')}"
            auth += "@"
        return f"{scheme}://{auth}{self.host}:{self.port}"


class RetailerAuth(BaseModel):
    """How the agent should re-authenticate this retailer if it lands on a sign-in page.

    Best Buy web sessions die fast (~20-25 min), so hands-off scheduling needs the agent to log
    itself back in. The robust path is "Sign in with Google": the profile stays logged into Gmail
    (a long-lived Google session), so a dead Best Buy session self-heals with one "Continue with
    Google" click — no Best Buy password or 2FA/TOTP code in the prompt. `google_email` only names
    which account to pick in Google's chooser; it is not a secret.

    `method="password"` (with `username`/`password`, and optionally `totp_secret` for authenticator-
    app 2FA) is the fallback for accounts not linked to Google. NOTE: Browser-Use v4 has no
    secret-injection channel, so anything stored here — password AND the TOTP secret — ends up in the
    agent's task prompt (visible to the LLM and stored in the cloud run history). Prefer
    `method="google"`, which stores no secret. `totp_secret` is the base32 seed shown when you set up
    the authenticator app (only works for authenticator-app 2FA, NOT SMS/email codes).
    """

    method: Literal["google", "apple", "password"] = "google"
    google_email: str = ""  # account to pick in Google's chooser (method "google"/"apple" via Google)
    username: str = ""  # method="password" only
    password: str = ""  # method="password" only
    totp_secret: str = ""  # method="password" only: base32 authenticator seed for 2FA (optional)


class ProfileConfig(BaseModel):
    """One Browser-Use cloud profile (browser identity + proxy) and which retailers it's logged into.

    A single profile commonly holds logins for several retailers at once (e.g. the same
    profile is logged into Amazon, Best Buy, and Walmart) — list every retailer it covers
    in `retailers` using each scraper's `retailer_key` (e.g. "amazon", "bestbuy").
    """

    label: str
    profile_id: str = ""
    proxy: ProxyConfig | None = None
    retailers: list[str] = Field(default_factory=list)
    # Optional per-retailer auto-auth config, keyed by retailer_key (e.g. {"bestbuy": {...}}).
    # Absent = the agent reports logged_out and does not try to sign in (today's behavior).
    auth: dict[str, RetailerAuth] = Field(default_factory=dict)
