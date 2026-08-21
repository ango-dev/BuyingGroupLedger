from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, Field


class ProxyConfig(BaseModel):
    host: str
    port: int
    username: str = ""
    # repr=False so a proxy password cannot ride out in a traceback, a log line or an alert email.
    # A ProfileConfig is handed to almost everything here, so its repr surfaces in a lot of places —
    # and an alert leaves the host entirely. The value is still read normally by as_url().
    password: str = Field(default="", repr=False)

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
    """How to re-authenticate this retailer when a run lands on a sign-in page.

    **USERNAME + PASSWORD IS THE ONLY SUPPORTED METHOD**. Google SSO, Apple and
    authenticator-app TOTP were all removed rather than left as options: each was a second login path
    that had to keep working, and every one of them is exercised only when a session happens to die,
    so a break in one would surface days later as a mystery logout.

    **YOU MUST DISABLE 2-STEP VERIFICATION ON THE BEST BUY ACCOUNT.** There is no code path that
    answers a 2FA challenge any more. Best Buy's own prompt calls it "Sign in with a verification
    code"; turn it off under Account Settings → Sign-in & Security. If it is on, the login stops at
    the challenge screen, the run alerts as logged out, and no order is ever read.

    The password reaches Best Buy two ways, and they are not equally exposed:

    - **The deterministic path (`scrapers/bestbuy_api.py`, primary)** types it into a CDP browser on
      this machine. It never builds a prompt, so the password never leaves the host.
    - **The agent fallback (`scrapers/bestbuy.py`)** puts it in the task prompt, because Browser-Use
      v4 has no secret-injection channel — unknown kwargs to `runs.create` 422. So it is visible to
      the LLM and stored in the cloud run history.

    That second exposure is the accepted cost of dropping SSO, and it is much smaller than it was
    when SSO was chosen: since the deterministic path landed (2026-08-10) the agent only runs when
    that path fails outright.
    """

    method: Literal["password"] = "password"
    username: str = ""
    # repr=False: this is the retailer account password. It is already exposed to the agent
    # fallback by necessity (v4 has no secret channel), so it must not ALSO leak everywhere a
    # profile happens to be printed.
    password: str = Field(default="", repr=False)


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
