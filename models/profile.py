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

        Browser paths hand the fields to Browser-Use separately (see `CdpBrowser`), but a
        retailer that calls an API over plain HTTP
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

    Keyed by `retailer_key` on the profile — **`bestbuy`** (`scrapers/bestbuy_api.py`) and
    **`amazon-business`** (`scrapers/amazon_business_signin.py`) support it today. Absent, the run
    reports logged-out, alerts and skips without trying to sign in.

    **USERNAME + PASSWORD + AUTHENTICATOR-APP 2FA.** Google SSO and Apple remain removed — each was a
    second login path exercised only when a session happened to die, so a break in one surfaced days
    later as a mystery logout.

    **2-STEP VERIFICATION IS REQUIRED ON THE ACCOUNT, reversing the 2026-08-13
    "turn it off" rule.** Leaving it off did not make sign-in reliable: Best Buy kept escalating
    untrusted sessions to an identity challenge that offers only "text me a code", which nothing here
    can answer. Enrolling an authenticator app replaces that with a challenge we CAN answer
    unattended, and both retailers' screens carry a "don't ask on this device" box — so clearing it
    once buys a trusted device rather than a code every run. **Enrol the AUTHENTICATOR APP, not SMS
    or email:** which challenge the site serves follows what the account has enrolled, and a texted
    code is one this cannot receive.

    `totp_secret` is the base32 key from that enrolment (the "can't scan the QR?" key). Store it as
    shown; spacing and case are normalised. Without it the run stops at the 2-step screen and alerts.

    The sign-in types these into a CDP browser session and generates the TOTP code locally with
    `scrapers/totp.py` — no LLM prompt is built, so nothing leaves the host. (The retired agent
    fallback used to carry the password in its task prompt; the TOTP seed was withheld from it even
    then, a permanent key being strictly worse to expose than one 30-second code.)
    """

    method: Literal["password"] = "password"
    username: str = ""
    # repr=False on both: these are account credentials and must not leak anywhere a profile
    # happens to be printed — the TOTP seed especially, being a permanent key rather than a single
    # code.
    password: str = Field(default="", repr=False)
    totp_secret: str = Field(default="", repr=False)


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
    # Absent = a lapsed session alerts and skips instead of signing itself back in.
    auth: dict[str, RetailerAuth] = Field(default_factory=dict)
