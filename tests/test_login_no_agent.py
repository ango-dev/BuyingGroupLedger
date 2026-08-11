"""A login/auth failure on the deterministic API path must NOT fall back to the (paid) agent.

The agent exists to handle DOM/page-shape changes; it cannot fix a logged-out session or a dead token
(and Amazon/Best Buy/Costco logins involve OTP/2FA/passkeys the agent can't do), so a login failure
alerts and skips (raises LoggedOutError) instead of running the agent. Any OTHER failure still falls
back to the agent — covered by test_login_failure_is_the_only_thing_that_skips_the_agent.
"""

import pytest

from models.profile import ProfileConfig
from scrapers import amazon, amazon_business, bestbuy, costco
from scrapers.base import ApiLoginError, BaseRetailerScraper, LoggedOutError

RETAILERS = [
    (amazon, amazon.AmazonScraper),
    (amazon_business, amazon_business.AmazonBusinessScraper),
    (bestbuy, bestbuy.BestBuyScraper),
    (costco, costco.CostcoScraper),
]


def _scraper(cls):
    profile = ProfileConfig(label="p", profile_id="x", retailers=[cls.retailer_key])
    return cls(profile, lookback_days=1)


@pytest.mark.parametrize("module, cls", RETAILERS, ids=lambda x: getattr(x, "__name__", ""))
def test_login_failure_skips_the_agent(monkeypatch, module, cls):
    scraper = _scraper(cls)

    def _login_fails():
        raise ApiLoginError("session logged out")

    monkeypatch.setattr(scraper, "_scrape_via_api", _login_fails)
    # If the agent (base scrape) is ever reached, fail loudly.
    monkeypatch.setattr(BaseRetailerScraper, "scrape",
                        lambda self: pytest.fail("agent must NOT run on a login failure"))
    alerts = []
    monkeypatch.setattr(module, "alert", lambda subject, body: alerts.append(subject))

    with pytest.raises(LoggedOutError):
        scraper.scrape()
    assert alerts, "a login failure should still alert the user to re-login"


@pytest.mark.parametrize("module, cls", RETAILERS, ids=lambda x: getattr(x, "__name__", ""))
def test_non_login_failure_still_falls_back_to_the_agent(monkeypatch, module, cls):
    scraper = _scraper(cls)

    def _dom_broke():
        raise RuntimeError("page shape changed")

    monkeypatch.setattr(scraper, "_scrape_via_api", _dom_broke)
    monkeypatch.setattr(BaseRetailerScraper, "scrape", lambda self: ["AGENT_RAN"])
    monkeypatch.setattr(module, "alert", lambda subject, body: None)

    assert scraper.scrape() == ["AGENT_RAN"], "a non-login failure must still use the agent fallback"
