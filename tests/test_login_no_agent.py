"""A login/auth failure on the deterministic API path must NOT fall back to the (paid) agent.

The agent exists to handle DOM/page-shape changes; it cannot fix a logged-out session or a dead token
(and Amazon/Best Buy/Costco logins involve OTP/2FA/passkeys the agent can't do), so a login failure
alerts and skips (raises LoggedOutError) instead of running the agent. Any OTHER failure still falls
back to the agent — covered by test_login_failure_is_the_only_thing_that_skips_the_agent.
"""

import pytest

from models.profile import ProfileConfig, ProxyConfig
from scrapers import amazon, amazon_business, bestbuy, costco
from scrapers.base import (
    ApiLoginError,
    BaseRetailerScraper,
    LoggedOutError,
    ScrapeUnavailableError,
)

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


class TestCostcoSharedProxyFailure:
    """A transport failure through the profile's proxy must not reach the agent either.

    OBSERVED LIVE. The proxy failed one upstream CONNECT to signin.costco.com during a
    token refresh. That surfaced as curl's Timeout rather than ApiLoginError, so it took the
    fall-back-to-the-agent branch — and the agent egresses through THAT SAME PROXY, so it couldn't
    load the page either and reported the session as LOGGED OUT. The resulting alert pointed at the
    token, which was fine: sixty seconds later the API authenticated on the first try.

    So the cost of one proxy blip was a paid agent run plus an alert aimed at the wrong component.
    """

    @staticmethod
    def _scraper(proxy):
        profile = ProfileConfig(
            label="p", profile_id="x", retailers=["costco"], proxy=proxy,
        )
        return costco.CostcoScraper(profile, lookback_days=1)

    @staticmethod
    def _proxy():
        return ProxyConfig(host="203.0.113.10", port=50100)

    @pytest.mark.parametrize("exc", [
        ConnectionError("Failed to connect over proxy"),
        TimeoutError("Failed to perform, curl: (28) Failed to connect to signin.costco.com:443"),
    ], ids=["connection", "timeout"])
    def test_a_proxy_transport_failure_skips_the_agent(self, monkeypatch, exc):
        scraper = self._scraper(self._proxy())
        monkeypatch.setattr(scraper, "_scrape_via_api", lambda: (_ for _ in ()).throw(exc))
        monkeypatch.setattr(BaseRetailerScraper, "scrape",
                            lambda self: pytest.fail("agent must NOT run — it shares the proxy"))
        alerts = []
        monkeypatch.setattr(costco, "alert", lambda subject, body: alerts.append((subject, body)))

        with pytest.raises(ScrapeUnavailableError):
            scraper.scrape()

        subject, body = alerts[0]
        assert "proxy unreachable" in subject and "agent NOT run" in subject
        # The alert has to actively steer AWAY from the token, because the symptom that reached the
        # user last time was "session is logged out".
        assert "NOT A LOGIN PROBLEM" in body

    def test_it_is_not_reported_as_a_logout(self, monkeypatch):
        """ScrapeUnavailableError must not be a LoggedOutError. main.run_scrape branches on the two
        separately, and collapsing them puts "session is logged out" back in the log for a healthy
        account — the exact false signal this exists to remove."""
        assert not issubclass(ScrapeUnavailableError, LoggedOutError)

    def test_the_same_failure_WITHOUT_a_proxy_still_uses_the_agent(self, monkeypatch):
        """Without a proxy the agent uses an entirely different transport — a remote browser, not
        this host's curl — so it genuinely might succeed where the API path could not."""
        scraper = self._scraper(None)
        monkeypatch.setattr(scraper, "_scrape_via_api",
                            lambda: (_ for _ in ()).throw(ConnectionError("network down")))
        monkeypatch.setattr(BaseRetailerScraper, "scrape", lambda self: ["AGENT_RAN"])
        monkeypatch.setattr(costco, "alert", lambda subject, body: None)

        assert scraper.scrape() == ["AGENT_RAN"]

    def test_an_http_error_through_a_proxy_still_uses_the_agent(self, monkeypatch):
        """An HTTP status means the transport DID get through, so the proxy is fine and the fault is
        Costco-side page/schema — precisely what the agent fallback is for."""
        scraper = self._scraper(self._proxy())
        monkeypatch.setattr(scraper, "_scrape_via_api",
                            lambda: (_ for _ in ()).throw(RuntimeError("HTTP 500 from GraphQL")))
        monkeypatch.setattr(BaseRetailerScraper, "scrape", lambda self: ["AGENT_RAN"])
        monkeypatch.setattr(costco, "alert", lambda subject, body: None)

        assert scraper.scrape() == ["AGENT_RAN"]
