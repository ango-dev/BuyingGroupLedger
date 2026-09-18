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
    # The third sender (2026-09-11): respond_bfmr replies to BFMR with a real MIME message
    # through this name. Same rule, same boundary.
    monkeypatch.setattr("alerts.notifier.send_message",
                        lambda msg, recipients, account=None: None)


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


@pytest.fixture(autouse=True)
def _block_real_sheet_reads(monkeypatch):
    """Safety net: no test may open the REAL Google Sheet, even read-only.

    The dashboard's sheet backend, the DB mirror script and the end-of-run mirror
    (main.run_db_mirror, 2026-09-17) all reach the live worksheet through one function,
    scripts.audit_sheet.open_worksheet_readonly. `settings` is built from the developer's own .env,
    so a test that reaches it un-stubbed would issue a genuine Sheets API read from a unit test.
    Same rule as the alerts and browsers above: block at the boundary, automatically. Tests of
    those readers inject their own `opener`, or patch this same name with a fake (a later patch in
    the test wins).
    """
    def _refuse():
        raise AssertionError(
            "A test tried to open the real Google Sheet. Inject a fake opener (see SheetReader's "
            "`opener` argument), patch scripts.audit_sheet.open_worksheet_readonly, or patch "
            "main.run_db_mirror."
        )

    monkeypatch.setattr("scripts.audit_sheet.open_worksheet_readonly", _refuse)


@pytest.fixture(autouse=True)
def _isolate_ledger_db(tmp_path_factory, monkeypatch):
    """Safety net: no test may touch the REAL data/ledger.sqlite3.

    `ledger.backend` defaults to `db` (2026-09-18), so an un-fixtured call into anything that opens
    the ledger -- sheets.ledger_sync._get_worksheet, the dashboard's writer, the read-only opener --
    lands on the SQLite file, and every settings object names it by the RELATIVE path
    `data/ledger.sqlite3`, resolved against ledger_db.store.ROOT. Pointing that root at a per-test
    temp directory sends every such path into it, whichever settings reference the caller holds.
    """
    from ledger_db import store

    monkeypatch.setattr(store, "ROOT", tmp_path_factory.mktemp("ledger-db"))


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path_factory, monkeypatch):
    """Safety net: no test may read the developer's REAL config.json or .state.json.

    Same class of bug as the two above, and the same shape of fix. config.json now holds every
    credential and every profile, so a test calling load_profiles() or load_cards() without a fixture
    would silently read the real one — passing on a configured machine, failing on CI, and shaping
    assertions around whatever happens to be in the author's own setup. Worse, `save_profiles` WRITES
    that file, so a test exercising create_profile could rewrite it.

    Points both files at a per-session temp directory that does not exist, so an un-fixtured test
    sees an empty config rather than a real one. Tests that need content use the `config_file`
    fixture below, which writes into a per-test path.
    """
    from config import loader

    empty = tmp_path_factory.mktemp("no-config")
    monkeypatch.setattr(loader, "CONFIG_FILE", empty / "config.json")
    monkeypatch.setattr(loader, "STATE_FILE", empty / ".state.json")
    loader.reload_config()
    yield
    loader.reload_config()


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """Write a config.json for this test and point the loader at it.

    Returns a callable: `config_file(cards=[...], profiles=[...])`. Call it more than once to
    rewrite, which is what a save/load round-trip test needs.
    """
    import json

    from config import loader

    path = tmp_path / "config.json"
    monkeypatch.setattr(loader, "CONFIG_FILE", path)
    monkeypatch.setattr(loader, "STATE_FILE", tmp_path / ".state.json")

    def write(**sections):
        path.write_text(json.dumps(sections, indent=2), encoding="utf-8")
        loader.reload_config()
        return path

    write()
    return write
