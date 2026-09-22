"""The first-time setup wizard (/setup; web/setup_wizard.py)."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from config import loader  # noqa: E402
from config.loader import config_value  # noqa: E402
from config.settings import Settings  # noqa: E402
from ledger.sync import HEADER  # noqa: E402
from scripts import backup as backup_module  # noqa: E402
from web import auth, setup_wizard  # noqa: E402
from web.app import create_app  # noqa: E402
from web.ledger_reader import SnapshotReader  # noqa: E402

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
PROFILE = {"label": "alpha", "profile_id": "", "retailers": ["costco"]}


def _client(tmp_path, *, password="", in_container=True):
    restarts, container_restarts = [], []
    (tmp_path / "snap").mkdir(exist_ok=True)
    (tmp_path / "snap" / "ledger_backup_20260917T000000Z.csv").write_text(",".join(HEADER) + chr(10), encoding="utf-8")
    app = create_app(SnapshotReader(data_dir=tmp_path / "snap"), logs_dir=tmp_path, failures_dir=tmp_path,
                     backup_dir=tmp_path / "backups", repo_root_dir=tmp_path, clock=lambda: NOW,
                     settings=dataclasses.replace(Settings(), container_run_interval_hours=6, web_password=password),
                     restarter=lambda: restarts.append(1), container_restarter=lambda: container_restarts.append(1),
                     in_container=in_container, session_secret="a-test-secret")
    client = TestClient(app)
    client.restarts, client.container_restarts, client.root = restarts, container_restarts, tmp_path
    return client


@pytest.fixture
def fresh(config_file, tmp_path):
    """No profiles, no record: a fresh install."""
    config_file()
    return _client(tmp_path)


@pytest.fixture
def configured(config_file, tmp_path):
    config_file(profiles=[PROFILE], web={"port": 8765})
    return _client(tmp_path)


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch):
    monkeypatch.setattr(setup_wizard, "GATE_ENABLED", True)  # conftest turns it off for every other test


def state_record():
    return loader.load_state().get("setup", {})


# --------------------------------------------------------------------------------------------------
# The gate and the record
# --------------------------------------------------------------------------------------------------


class TestGate:
    def test_a_fresh_install_lands_on_the_wizard_from_anywhere_but_the_open_paths(self, fresh):
        assert fresh.get("/orders", follow_redirects=False).headers["location"] == "/setup"
        assert fresh.get("/", follow_redirects=False).status_code == 303
        for path in ("/setup/restore", "/settings", "/health", "/static/style.css", "/login"):
            response = fresh.get(path, follow_redirects=False)
            assert response.status_code in (200, 303) and response.headers.get("location") != "/setup", path
        assert fresh.get("/setup", follow_redirects=False).headers["location"] == "/setup/restore"
        hx = fresh.get("/orders", headers={"HX-Request": "true"})
        assert hx.status_code == 401 and hx.headers["HX-Redirect"] == "/setup"

    def test_a_configured_install_is_never_interrupted_and_is_stamped_complete(self, configured):
        assert configured.get("/", follow_redirects=False).status_code == 200
        assert state_record()["completed_at"] == NOW.isoformat(timespec="seconds")  # stamped lazily, once
        assert configured.get("/setup/restore").status_code == 200  # still reachable, for a re-run

    def test_a_finished_setup_whose_profiles_were_deleted_is_not_gated(self, config_file, tmp_path):
        config_file()
        loader.save_state({"setup": {"completed_at": "2026-09-01T00:00:00", "version": 1}})
        client = _client(tmp_path)
        assert client.get("/", follow_redirects=False).status_code == 200

    def test_a_restored_password_protects_the_wizard_too(self, config_file, tmp_path):
        config_file(web={"password": "pw"})  # a config with a password and no profiles
        client = _client(tmp_path, password="pw")
        response = client.get("/setup/restore", follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"].startswith("/login?next=%2Fsetup")

    def test_a_fresh_docker_host_starts_from_empty_stubs(self, config_file, tmp_path):
        """The 2026-09-21 review's blocker: docker-compose bind-mounts config.json and .state.json
        as files, so DEPLOY.md now has a new host `touch` both before its first start. Zero-byte
        files must read as a fresh install and take every save -- the password, a profile, the
        Done record -- where a missing-file start used to promise a wizard it could not finish."""
        path = config_file()
        path.write_text("", encoding="utf-8")
        (tmp_path / ".state.json").write_text("", encoding="utf-8")
        loader.reload_config()
        client = _client(tmp_path)

        assert client.get("/", follow_redirects=False).headers["location"] == "/setup"
        saved = client.post("/setup/password", data={"password": "pw", "confirm": "pw"}, follow_redirects=False)
        assert saved.status_code == 303 and config_value("web.password") == "pw"
        assert state_record()["restart"] == "dashboard"  # the record landed in the stub
        client.post("/setup/profiles/entry", data={"label": "alpha", "retailers": ["costco"]}, follow_redirects=False)
        assert client.get("/setup/done").status_code == 200
        assert json.loads(path.read_text(encoding="utf-8"))["profiles"][0]["label"] == "alpha"
        assert state_record()["completed_at"] == NOW.isoformat(timespec="seconds")

    def test_needs_setup_is_the_one_decision(self, config_file):
        config_file()
        assert setup_wizard.needs_setup(lambda: NOW) is True and state_record() == {}
        config_file(profiles=[PROFILE])
        assert setup_wizard.needs_setup(lambda: NOW) is False and state_record()["completed_at"]
        config_file()
        assert setup_wizard.needs_setup(lambda: NOW) is False  # the record stands


# --------------------------------------------------------------------------------------------------
# The steps
# --------------------------------------------------------------------------------------------------


class TestSteps:
    def test_the_step_strip_and_the_unknown_step(self, fresh):
        body = fresh.get("/setup/restore").text
        assert [s.title for s in setup_wizard.STEPS] == ["Restore a backup", "Dashboard password", "Browser-Use key", "Profiles",
                                                        "Buying groups", "Cards", "Alerts", "Schedule and backups", "Import history", "Done"]
        for title in ("1. Restore a backup", "10. Done"):
            assert title in body
        assert 'class="current"' in body and "<nav>" in body and "Settings instead" in body
        assert 'href="/setup/password"' in body  # skip
        assert fresh.get("/setup/nope").status_code == 404

    def test_a_scalar_step_writes_only_its_own_settings(self, fresh):
        page = fresh.get("/setup/browser_use").text
        assert 'for="f-BROWSER_USE_API_KEY"' in page and 'for="f-LOOKBACK_DAYS"' not in page
        response = fresh.post("/setup/browser_use", data={"BROWSER_USE_API_KEY": "bu-key"}, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/setup/profiles"
        assert config_value("browser_use.api_key") == "bu-key"
        assert config_value("scraping.lookback_days") is None  # nothing else was touched
        assert fresh.post("/setup/alerts", data={"GMAIL_ADDRESS": "me@example.com", "GMAIL_ALERTS_ENABLED": "on"},
                          follow_redirects=False).headers["location"] == "/setup/schedule"
        assert config_value("alerts.gmail_address") == "me@example.com" and config_value("browser_use.api_key") == "bu-key"
        bad = fresh.post("/setup/schedule", data={"RUN_INTERVAL_HOURS": "six"})
        assert bad.status_code == 400 and "Nothing was saved" in bad.text and config_value("container.run_interval_hours") is None
        good = fresh.post("/setup/schedule", data={"RUN_INTERVAL_HOURS": "4", "BACKUP_ENABLED": "on"}, follow_redirects=False)
        assert good.headers["location"] == "/setup/import" and config_value("container.run_interval_hours") == 4
        assert state_record()["restart"] == "container"  # the schedule is read at container start

    def test_the_entry_steps_add_and_remove_cards_and_the_groups_step_saves_its_keys(self, fresh):
        page = fresh.get("/setup/profiles").text
        assert 'action="/setup/profiles/entry"' in page and "Edit profiles as JSON" not in page
        added = fresh.post("/setup/profiles/entry", data={"label": "alpha", "retailers": ["costco"]}, follow_redirects=False)
        assert added.status_code == 303 and added.headers["location"].startswith("/setup/profiles?message=Added+profile")
        assert [p["label"] for p in config_value("profiles")] == ["alpha"]
        assert fresh.get("/", follow_redirects=False).status_code == 200  # a profile: configured now, the gate opens
        groups = fresh.get("/setup/groups").text
        assert 'action="/setup/groups/entry"' in groups and 'for="f-BFMR_API_KEY"' in groups
        assert fresh.post("/setup/groups/entry", data={"buying_group": "BFMR"}, follow_redirects=False).status_code == 303
        assert [w["buying_group"] for w in config_value("warehouses")] == ["BFMR"]
        keys = fresh.post("/setup/groups", data={"BFMR_API_KEY": "K", "BFMR_API_SECRET": "S"}, follow_redirects=False)
        assert keys.headers["location"].startswith("/setup/groups?message=Saved") and config_value("buying_groups.bfmr.api_key") == "K"
        assert fresh.post("/setup/cards/entry", data={"name": "Prime Visa", "last4": "0315", "cashback_rate": "5%"},
                          follow_redirects=False).status_code == 303
        assert config_value("cards")[0]["last4"] == "0315"
        assert fresh.post("/setup/cards/entry/0/delete", follow_redirects=False).status_code == 303
        assert config_value("cards") == []
        refused = fresh.post("/setup/cards/entry", data={"name": "no last 4"})
        assert refused.status_code == 400 and "Nothing was saved" in refused.text

    def test_the_password_step_signs_the_browser_in_for_the_new_password(self, fresh):
        mismatch = fresh.post("/setup/password", data={"password": "a", "confirm": "b"})
        assert mismatch.status_code == 400 and "differ" in mismatch.text
        blank = fresh.post("/setup/password", data={"password": "", "confirm": ""}, follow_redirects=False)
        assert blank.headers["location"] == "/setup/browser_use" and config_value("web.password") is None
        response = fresh.post("/setup/password", data={"password": "open sesame", "confirm": "open sesame"}, follow_redirects=False)
        assert response.headers["location"] == "/setup/browser_use" and config_value("web.password") == "open sesame"
        cookie = fresh.cookies.get(auth.COOKIE)
        assert cookie and auth.Sessions(b"a-test-secret", "open sesame", clock=lambda: NOW.timestamp()).verify(cookie) == "remembered"
        assert state_record()["restart"] == "dashboard"

    def test_changing_the_password_on_a_protected_dashboard_says_to_sign_in_again(self, config_file, tmp_path):
        config_file(profiles=[PROFILE], web={"password": "old"})
        client = _client(tmp_path, password="old")
        client.post("/login", data={"password": "old", "next": "/"})
        before = client.cookies.get(auth.COOKIE)
        response = client.post("/setup/password?again=1", data={"password": "new", "confirm": "new"}, follow_redirects=False)
        assert response.headers["location"] == "/setup/browser_use?again=1&signin=1"
        assert client.cookies.get(auth.COOKIE) == before  # the running sessions still verify the old cookie
        assert config_value("web.password") == "new"


class TestRestoreAndDone:
    def test_a_restored_config_ends_the_wizard_with_the_container_restart(self, fresh, tmp_path):
        source = tmp_path / "other-host"
        source.mkdir()
        (source / "config.json").write_text(json.dumps({"profiles": [PROFILE]}), encoding="utf-8")
        (source / "data").mkdir()
        archive = backup_module.create_backup(source, tmp_path / "made")
        response = fresh.post("/setup/restore", files={"archive": (archive.name, archive.read_bytes(), "application/zip")},
                              data={"force": "1"}, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"].startswith("/setup/done?restored=1")
        assert json.loads((fresh.root / "config.json").read_text(encoding="utf-8"))["profiles"] == [PROFILE]
        done = fresh.get(response.headers["location"])
        assert done.status_code == 200 and "The backup is restored" in done.text
        assert "Restart the container to apply" in done.text and 'class="danger attention"' in done.text
        assert state_record()["completed_at"] and fresh.get("/", follow_redirects=False).status_code == 200

    def test_a_zip_without_a_config_stays_on_the_step(self, fresh, tmp_path):
        source = tmp_path / "other-host"
        (source / "data").mkdir(parents=True)
        archive = backup_module.create_backup(source, tmp_path / "made")
        response = fresh.post("/setup/restore", files={"archive": (archive.name, archive.read_bytes(), "application/zip")},
                              follow_redirects=False)
        assert response.headers["location"].startswith("/setup/restore?message=")
        assert fresh.get("/orders", follow_redirects=False).headers["location"] == "/setup"

    def test_done_records_the_setup_and_offers_what_needs_a_restart(self, fresh):
        fresh.post("/setup/schedule", data={"RUN_INTERVAL_HOURS": "4"}, follow_redirects=False)
        fresh.post("/setup/profiles/entry", data={"label": "alpha", "retailers": ["costco"]}, follow_redirects=False)
        done = fresh.get("/setup/done").text
        assert "Setup is complete" in done and "1 entered" in done and "every 4 h" in done
        assert 'class="danger attention"' in done  # the container restart, for the schedule
        assert state_record()["completed_at"] == NOW.isoformat(timespec="seconds")
        assert fresh.get("/", follow_redirects=False).status_code == 200
        assert fresh.post("/settings/restart-container", follow_redirects=False).status_code == 200 and fresh.container_restarts == [1]


class TestRunAgain:
    def test_settings_offers_it_and_every_step_shows_what_is_set_with_keep(self, configured):
        settings = configured.get("/settings").text
        assert 'action="/setup/restart"' in settings and "Run the setup wizard again" in settings
        response = configured.post("/setup/restart", follow_redirects=False)
        assert response.headers["location"] == "/setup/restore?again=1" and state_record()["rerun"]
        page = configured.get("/setup/browser_use?again=1").text
        assert "keep it and continue" in page and 'href="/setup/profiles?again=1"' in page and "Change and continue" in page
        profiles = configured.get("/setup/profiles?again=1").text
        assert "alpha" in profiles and 'href="/setup/groups?again=1"' in profiles
        assert configured.get("/setup/done").status_code == 200
        assert state_record()["rerun"] is None  # Done clears it
        assert configured.get("/setup/alerts").text.count("keep it and continue") == 0


class TestSettingsPageStillWhole:
    def test_the_macros_moved_to_a_partial_render_as_before(self, configured):
        body = configured.get("/settings").text
        for marker in ('id="scalar-form"', 'action="/settings/section/profiles/entry"', 'action="/settings/section/profiles/entry/0"',
                       'action="/settings/section/profiles/entry/0/delete"', "Edit profiles as JSON", 'id="s-advanced"',
                       'for="f-LOOKBACK_DAYS"', 'id="s-setup"'):
            assert marker in body, marker


# --------------------------------------------------------------------------------------------------
# Trust: Done means done, the strip tells the truth, a
# refused form keeps what was typed, errors read as sentences, twins and blanks are refused
# --------------------------------------------------------------------------------------------------


class TestTrust:
    def test_done_needs_a_profile_and_the_strip_never_links_ahead_on_a_fresh_install(self, fresh):
        """A GET of /setup/done used to stamp the install complete with zero profiles (and every step
        chip was a link, so a click on "10. Done" did it); now it bounces to Profiles with the reason."""
        done = fresh.get("/setup/done", follow_redirects=False)
        assert done.status_code == 303 and done.headers["location"].startswith("/setup/profiles?message=")
        assert "profile" in done.headers["location"].lower() and state_record().get("completed_at") is None
        assert fresh.get("/orders", follow_redirects=False).headers["location"] == "/setup"
        strip = fresh.get("/setup/password").text
        strip = strip[strip.index('<ol class="setup-steps">'):strip.index("</ol>")]
        assert 'href="/setup/restore"' in strip and 'href="/setup/password"' in strip  # behind and current: links
        assert 'href="/setup/browser_use"' not in strip and 'href="/setup/done"' not in strip  # ahead: not links
        assert "<span>3. Browser-Use key</span>" in strip and 'aria-current="step"' in strip
        fresh.post("/setup/profiles/entry", data={"label": "alpha", "retailers": ["costco"]}, follow_redirects=False)
        assert fresh.get("/setup/done").status_code == 200 and state_record()["completed_at"]
        strip = fresh.get("/setup/password").text
        assert 'href="/setup/done"' in strip  # configured: every step reachable

    def test_the_step_ticks_mean_has_a_value_not_was_visited(self, fresh):
        import re

        def done_titles(key):
            body = fresh.get(f"/setup/{key}").text
            strip = body[body.index('<ol class="setup-steps">'):body.index("</ol>")]
            chips = re.findall(r'<li class="([^"]*)"[^>]*>(?:<a[^>]*>|<span>)(\d+\. [^<]+)', strip)
            assert len(chips) == 10
            return [title for classes, title in chips if "done" in classes.split()]
        assert done_titles("schedule") == ["1. Restore a backup"]  # skipping ahead ticks nothing but the optional step passed
        fresh.post("/setup/password", data={"password": "pw", "confirm": "pw"}, follow_redirects=False)
        fresh.post("/setup/cards/entry", data={"name": "Prime Visa", "last4": "0315", "cashback_rate": "5%"}, follow_redirects=False)
        assert done_titles("restore") == ["2. Dashboard password", "6. Cards"]
        fresh.post("/setup/alerts", data={"GMAIL_ADDRESS": "me@example.com", "GMAIL_ALERTS_ENABLED": "on"}, follow_redirects=False)
        fresh.post("/setup/schedule", data={"RUN_INTERVAL_HOURS": "4"}, follow_redirects=False)
        assert done_titles("restore") == ["2. Dashboard password", "6. Cards", "7. Alerts", "8. Schedule and backups"]

    def test_a_refused_form_keeps_what_was_typed_and_says_it_in_words(self, fresh):
        bad = fresh.post("/setup/schedule", data={"RUN_INTERVAL_HOURS": "6", "BACKUP_KEEP": "abc", "BACKUP_TIME": "0400", "TZ": "Mars/Olympus"})
        assert bad.status_code == 400 and "Nothing was saved" in bad.text
        assert 'value="Mars/Olympus"' in bad.text and 'value="0400"' in bad.text and 'value="abc"' in bad.text  # typed, not the file's
        assert "is not a whole number" in bad.text and "invalid literal" not in bad.text
        assert config_value("container.timezone") is None
        card = fresh.post("/setup/cards/entry", data={"name": "Prime Visa", "last4": "0315", "cashback_rate": "abc"})
        assert card.status_code == 400 and 'value="Prime Visa"' in card.text and 'value="abc"' in card.text  # the add card keeps its fields
        assert "pydantic" not in card.text and "[type=" not in card.text and "is not a number" in card.text
        assert 'id="add-cards" open' in card.text
        percent = fresh.post("/setup/cards/entry", data={"name": "Prime Visa", "last4": "0315", "cashback_rate": "2"})
        assert percent.status_code == 400 and "outside 0-1" in percent.text and "For further information" not in percent.text

    def test_twins_and_blank_identities_are_refused(self, fresh):
        assert fresh.post("/setup/profiles/entry", data={"label": "alpha", "retailers": ["costco"]}, follow_redirects=False).status_code == 303
        twin = fresh.post("/setup/profiles/entry", data={"label": "Alpha", "retailers": ["amazon"]})
        assert twin.status_code == 400 and "already exists (entry 1)" in twin.text and len(config_value("profiles")) == 1
        blank = fresh.post("/setup/profiles/entry", data={"label": "   ", "retailers": ["amazon"]})
        assert blank.status_code == 400 and "Label is required" in blank.text and len(config_value("profiles")) == 1
        fresh.post("/setup/cards/entry", data={"name": "Prime Visa", "last4": "0315"}, follow_redirects=False)
        assert fresh.post("/setup/cards/entry", data={"name": "Other", "last4": "0315"}).status_code == 400
        assert fresh.post("/setup/cards/entry", data={"name": "", "last4": "1234"}).status_code == 400
        assert "four digits" in fresh.post("/setup/cards/entry", data={"name": "Short", "last4": "12"}).text
        assert [c["last4"] for c in config_value("cards")] == ["0315"]
        fresh.post("/setup/groups/entry", data={"buying_group": "BFMR"}, follow_redirects=False)
        assert "already exists" in fresh.post("/setup/groups/entry", data={"buying_group": "bfmr"}).text
        assert "Buying group is required" in fresh.post("/setup/groups/entry", data={"buying_group": ""}).text
        # an edit of the entry itself is not a twin of itself
        assert fresh.post("/setup/cards/entry/0", data={"name": "Prime Visa Renamed", "last4": "0315"}, follow_redirects=False).status_code == 303
        # a virtual number of a card that does not exist is refused, naming the control
        orphan = fresh.post("/setup/cards/entry", data={"name": "V", "last4": "9999", "kind": "virtual", "virtual_of": "4242"})
        assert orphan.status_code == 400 and "No card ends in 4242" in orphan.text and "Shares limits with" in orphan.text

    def test_the_restart_flag_clears_with_the_restart_and_the_schedule_has_bounds(self, fresh):
        assert fresh.post("/setup/schedule", data={"RUN_INTERVAL_HOURS": "-3"}).status_code == 400
        assert "at least 1" in fresh.post("/setup/schedule", data={"RUN_INTERVAL_HOURS": "0"}).text
        assert "between 1 and 23" in fresh.post("/setup/schedule", data={"RUN_INTERVAL_HOURS": "24"}).text
        assert "at least 1" in fresh.post("/setup/schedule", data={"RUN_INTERVAL_HOURS": "4", "BACKUP_KEEP": "0"}).text
        assert config_value("container.run_interval_hours") is None
        fresh.post("/setup/schedule", data={"RUN_INTERVAL_HOURS": "4"}, follow_redirects=False)
        assert state_record()["restart"] == "container"
        fresh.post("/settings/restart-container", follow_redirects=False)
        assert state_record()["restart"] == "" and fresh.container_restarts == [1]
        fresh.post("/setup/password", data={"password": "pw", "confirm": "pw"}, follow_redirects=False)
        assert state_record()["restart"] == "dashboard"
        fresh.post("/settings/restart", follow_redirects=False)
        assert state_record()["restart"] == "" and fresh.restarts == [1]

    def test_a_file_that_is_not_a_zip_stays_on_the_step_and_is_not_kept(self, fresh):
        response = fresh.post("/setup/restore", files={"archive": ("notes.zip", b"this is not a zip", "application/zip")}, follow_redirects=False)
        assert response.status_code == 303 and "not+a+backup+zip" in response.headers["location"]
        assert not list((fresh.root / "backups").glob("uploaded_*")) and state_record().get("completed_at") is None
        assert "not a backup zip" in fresh.get(response.headers["location"]).text

    def test_saves_say_so_and_the_password_change_notice_shows_on_the_next_step(self, config_file, tmp_path):
        config_file(profiles=[PROFILE], web={"password": "old"})
        client = _client(tmp_path, password="old")
        client.post("/login", data={"password": "old", "next": "/"})
        response = client.post("/setup/password?again=1", data={"password": "new", "confirm": "new"}, follow_redirects=False)
        nxt = client.get(response.headers["location"]).text
        assert "The password changed" in nxt and "sign in with the new one" in nxt
        keys = client.post("/setup/groups?again=1", data={"BFMR_API_KEY": "K"}, follow_redirects=False)
        assert "message=" in keys.headers["location"] and "Saved" in client.get(keys.headers["location"]).text
        added = client.post("/setup/cards/entry?again=1", data={"name": "Prime Visa", "last4": "0315"}, follow_redirects=False)
        assert "Added+card" in added.headers["location"]
