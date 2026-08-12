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
