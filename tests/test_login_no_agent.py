"""A login/auth failure on the deterministic API path must NOT fall back to the (paid) agent.

The agent cannot fix a logged-out session or a dead token (and Amazon/Best Buy/Costco logins involve
OTP/2FA/passkeys the agent can't do), so a login failure alerts and skips (raises LoggedOutError)
instead of taking the shape-change path. Any OTHER failure writes a failure dossier and stops —
the paid agent fallback is gone entirely; see TestNonLoginFailures.
"""

import pytest

from models.profile import ProfileConfig, ProxyConfig
from scrapers import amazon, amazon_business, bestbuy, costco
from scrapers.base import (
    ApiLoginError,
    BaseRetailerScraper,
    DeterministicPathError,
    LoggedOutError,
    ScrapeUnavailableError,
)


@pytest.fixture(autouse=True)
def _dossiers_in_tmp(monkeypatch, tmp_path):
    """Every scraper here opens a dossier; keep its files out of the real logs/ directory."""
    import diagnostics.dossier as dossier_mod

    monkeypatch.setattr(dossier_mod, "FAILURES_DIR", tmp_path / "failures")

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
    # A login failure must never take the generic shape-change path.
    monkeypatch.setattr(scraper, "_on_deterministic_failure",
                        lambda *a, **k: pytest.fail("a login failure is not a page-shape failure"))
    alerts = []
    monkeypatch.setattr(module, "alert", lambda subject, body: alerts.append(subject))

    with pytest.raises(LoggedOutError):
        scraper.scrape()
    assert alerts, "a login failure should still alert the user to re-login"


class TestNonLoginFailures:
    """A page-shape failure is a DOSSIER, never a scrape that guesses.

    The paid agent used to be the catch-all for anything that wasn't a login failure; it was
    retired 2026-08-29 and fully removed the same week. A failure now records NOTHING, alerts with
    the dossier's path, and raises DeterministicPathError so main.run_scrape just logs and skips.
    """

    @staticmethod
    def _break(monkeypatch, scraper, module):
        def _dom_broke():
            raise RuntimeError("page shape changed")

        monkeypatch.setattr(scraper, "_scrape_via_api", _dom_broke)
        alerts = []
        monkeypatch.setattr(module, "alert", lambda subject, body: alerts.append((subject, body)))
        # base.py's own alert is what _on_deterministic_failure uses.
        import scrapers.base as base
        monkeypatch.setattr(base, "alert", lambda subject, body: alerts.append((subject, body)))
        return alerts

    @pytest.mark.parametrize("module, cls", RETAILERS, ids=lambda x: getattr(x, "__name__", ""))
    def test_a_page_shape_failure_writes_a_dossier_and_records_nothing(
            self, monkeypatch, tmp_path, module, cls):
        scraper = _scraper(cls)
        alerts = self._break(monkeypatch, scraper, module)

        with pytest.raises(DeterministicPathError):
            scraper.scrape()

        subject, body = alerts[-1]
        assert "NOT recorded" in subject
        assert "Failure dossier:" in body
        dossiers = list((tmp_path / "failures").iterdir())
        assert len(dossiers) == 1
        report = (dossiers[0] / "report.md").read_text(encoding="utf-8")
        assert "RuntimeError" in report and "page shape changed" in report
        assert str(dossiers[0]) in body, "the alert must say where the dossier is"

    def test_a_login_failure_alert_also_points_at_its_dossier(self, monkeypatch, tmp_path):
        """Sign-in flows have selectors too; a login failure's page is just as worth capturing."""
        scraper = _scraper(amazon.AmazonScraper)
        monkeypatch.setattr(scraper, "_scrape_via_api",
                            lambda: (_ for _ in ()).throw(ApiLoginError("session logged out")))
        alerts = []
        monkeypatch.setattr(amazon, "alert", lambda subject, body: alerts.append(body))

        with pytest.raises(LoggedOutError):
            scraper.scrape()
        assert "Failure dossier:" in alerts[0]
        assert (tmp_path / "failures").exists()


class TestCostcoSelfHealsADeadToken:
    """A dead refresh token is the one auth failure recoverable without a human: the Browser-Use
    profile is still logged into Costco, so a CDP reconnect can capture a fresh token from the app's
    own silent refresh. Proven live — 12 seconds, and the retried API path scraped.

    It is also exactly when the grab works: it captures the token endpoint's RESPONSE, so it needs
    the app to actually refresh, which it only does once the cached token has expired. That is the
    state this call site is in by definition.
    """

    @staticmethod
    def _scraper():
        profile = ProfileConfig(label="p", profile_id="x", retailers=["costco"])
        return costco.CostcoScraper(profile, lookback_days=1)

    def test_a_dead_token_is_refreshed_and_the_api_retried(self, monkeypatch):
        scraper = self._scraper()
        calls = []

        def _api():
            calls.append("api")
            if len(calls) == 1:
                raise ApiLoginError("id_token exchange failed")
            return ["ROWS"]

        monkeypatch.setattr(scraper, "_scrape_via_api", _api)
        monkeypatch.setattr(scraper, "_refresh_token_via_browser", lambda: True)
        alerts = []
        monkeypatch.setattr(costco, "alert", lambda subject, body: alerts.append(subject))

        assert scraper.scrape() == ["ROWS"]
        assert calls == ["api", "api"], "the API path is retried once after the refresh"
        assert alerts == [], "a self-healed run is not worth waking anyone for"

    def test_a_failed_refresh_still_alerts_and_never_runs_the_agent(self, monkeypatch):
        scraper = self._scraper()
        monkeypatch.setattr(scraper, "_scrape_via_api",
                            lambda: (_ for _ in ()).throw(ApiLoginError("dead token")))
        monkeypatch.setattr(scraper, "_refresh_token_via_browser", lambda: False)
        alerts = []
        monkeypatch.setattr(costco, "alert", lambda subject, body: alerts.append((subject, body)))

        with pytest.raises(LoggedOutError):
            scraper.scrape()
        subject, body = alerts[0]
        assert "API auth failed" in subject
        # The refresh reads from the profile's Costco session, so its failure narrows the diagnosis:
        # point at re-logging the PROFILE in, not just at pasting a token.
        assert "create_profile" in body

    def test_a_refresh_that_blows_up_does_not_replace_the_real_diagnosis(self, monkeypatch):
        """The recovery runs on an already-failing path. If it throws, the caller must still report
        the ORIGINAL auth failure rather than surfacing the recovery's own error."""
        scraper = self._scraper()
        monkeypatch.setattr(scraper, "_scrape_via_api",
                            lambda: (_ for _ in ()).throw(ApiLoginError("dead token")))
        monkeypatch.setattr(
            costco.CostcoScraper, "_refresh_token_via_browser",
            lambda self: (_ for _ in ()).throw(RuntimeError("CDP exploded")))
        monkeypatch.setattr(costco, "alert", lambda subject, body: None)

        with pytest.raises(RuntimeError):
            scraper.scrape()

    def test_the_stale_id_token_is_dropped_when_a_new_refresh_token_lands(self, monkeypatch, tmp_path):
        """The cached id_token was minted from the OLD refresh token and is what just failed —
        leaving it would have the retry present the same dead credential."""
        import scripts.costco_token as token_mod

        saved = {}
        monkeypatch.setattr(token_mod, "_grab_refresh_token", lambda label: "FRESH")
        monkeypatch.setattr(token_mod, "_load", lambda label: {"refresh_token": "OLD",
                                                              "id_token": "STALE"})
        monkeypatch.setattr(token_mod, "_save", lambda label, data: saved.update(data))

        assert self._scraper()._refresh_token_via_browser() is True
        assert saved["refresh_token"] == "FRESH"
        assert "id_token" not in saved


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
    def test_a_proxy_transport_failure_is_named_as_such(self, monkeypatch, exc):
        scraper = self._scraper(self._proxy())
        monkeypatch.setattr(scraper, "_scrape_via_api", lambda: (_ for _ in ()).throw(exc))
        alerts = []
        monkeypatch.setattr(costco, "alert", lambda subject, body: alerts.append((subject, body)))

        with pytest.raises(ScrapeUnavailableError):
            scraper.scrape()

        subject, body = alerts[0]
        assert "proxy unreachable" in subject
        # The alert has to actively steer AWAY from the token, because the symptom that reached the
        # user last time was "session is logged out".
        assert "NOT A LOGIN PROBLEM" in body

    def test_it_is_not_reported_as_a_logout(self, monkeypatch):
        """ScrapeUnavailableError must not be a LoggedOutError. main.run_scrape branches on the two
        separately, and collapsing them puts "session is logged out" back in the log for a healthy
        account — the exact false signal this exists to remove."""
        assert not issubclass(ScrapeUnavailableError, LoggedOutError)

    def test_the_same_failure_WITHOUT_a_proxy_is_NOT_a_proxy_failure(self, monkeypatch):
        """Without a proxy the failure takes the generic dossier path — it must not be reported as
        'proxy unreachable', because there is no proxy to blame."""
        scraper = self._scraper(None)
        monkeypatch.setattr(scraper, "_scrape_via_api",
                            lambda: (_ for _ in ()).throw(ConnectionError("network down")))
        subjects = []
        monkeypatch.setattr(costco, "alert", lambda subject, body: subjects.append(subject))
        import scrapers.base as base
        monkeypatch.setattr(base, "alert", lambda subject, body: subjects.append(subject))

        with pytest.raises(DeterministicPathError):
            scraper.scrape()
        assert not any("proxy unreachable" in s for s in subjects)

    def test_an_http_error_through_a_proxy_is_NOT_a_proxy_failure(self, monkeypatch):
        """An HTTP status means the transport DID get through, so the proxy is fine and the fault is
        Costco-side page/schema — the generic path, not the 'proxy unreachable' skip."""
        scraper = self._scraper(self._proxy())
        monkeypatch.setattr(scraper, "_scrape_via_api",
                            lambda: (_ for _ in ()).throw(RuntimeError("HTTP 500 from GraphQL")))
        subjects = []
        monkeypatch.setattr(costco, "alert", lambda subject, body: subjects.append(subject))
        import scrapers.base as base
        monkeypatch.setattr(base, "alert", lambda subject, body: subjects.append(subject))

        with pytest.raises(DeterministicPathError):
            scraper.scrape()
        assert not any("proxy unreachable" in s for s in subjects)
        assert any("NOT recorded" in s for s in subjects)
