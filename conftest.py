# Presence of this file at the project root puts the root on sys.path for pytest, so tests can
# `import models.order`, `import sheets.ledger_sync`, etc. the same way main.py does.

import pytest


@pytest.fixture(autouse=True)
def _block_real_alerts(monkeypatch):
    """Safety net: no test may ever send a REAL email or Discord message.

    Found live (2026-08-12): a test exercising the undisclosed-split path forgot to mock
    alerts.notifier.alert and fired a real alert — with fabricated fixture data ("Order B1 / HP - 14
    Laptop") — through the user's actual configured email/Discord. alert() is called from many
    production code paths (main.py, every scraper, sheets/ledger_sync.py), and remembering to mock it
    in every test that happens to reach one is exactly the kind of thing that gets missed once and then
    forgotten again. So this mocks at the NETWORK boundary — send_email/send_discord, the two functions
    that actually touch SMTP/webhooks — for every test, automatically, with no opt-in required.

    A test that wants to assert alert() was CALLED (see TestUndisclosedSplit's `alerts` fixture) still
    monkeypatches alert() itself on top of this; the two targets don't conflict, since this fixture
    never touches alert() — only what it calls.
    """
    monkeypatch.setattr("alerts.notifier.send_email", lambda subject, body: None)
    monkeypatch.setattr("alerts.notifier.send_discord", lambda message: None)


@pytest.fixture(autouse=True)
def _block_paid_cloud_browsers(monkeypatch):
    """Safety net: no test may ever create a REAL Browser-Use cloud browser.

    Found live (2026-08-21), exactly like the alerts leak above. Receipt capture reads its OCI
    settings from the real .env, so the moment that bucket was configured, a test in
    test_run_isolation.py that exercised run_scrape without stubbing receipts sailed past
    `store.is_configured()` and issued a genuine `POST /browsers` against the Browser-Use API. It
    only failed because the fixture's fake profile_id isn't a UUID — with a plausible-looking id it
    would have started a BILLING cloud browser from a unit test.

    That is the same class of bug as the alerts one, and it has the same shape of fix: mock at the
    NETWORK boundary for every test automatically, rather than relying on each new test to remember.
    Note what changed here was CONFIGURATION, not code — the suite was offline right up until a
    credential landed in .env, which is precisely why this cannot be left to per-test discipline.

    tests/test_cdp_browser.py drives the real CdpBrowser and patches this same name itself; a
    module-level patch wins over this one, so those tests are unaffected.
    """
    def _refuse(*args, **kwargs):
        raise AssertionError(
            "A test tried to create a real Browser-Use cloud browser, which costs money. Inject a "
            "fake (see attach_receipts' browser_factory argument) or patch scrapers.cdp.BrowserUseV2."
        )

    monkeypatch.setattr("scrapers.cdp.BrowserUseV2", _refuse)


@pytest.fixture(autouse=True)
def _receipts_inert_by_default(monkeypatch):
    """Receipt capture reads live OCI credentials from .env, so it must be switched OFF for tests.

    Without this the suite's behaviour depends on whether the machine running it happens to have a
    bucket configured — tests would pass on CI and hit real object storage on the developer's box.
    Patches the SETTINGS rather than is_configured(), so everything downstream follows naturally and
    the tests that DO exercise storage (which replace store.settings or is_configured themselves)
    still win.
    """
    import dataclasses

    from receipts import store

    monkeypatch.setattr(store, "settings", dataclasses.replace(store.settings, oci_bucket=""))
