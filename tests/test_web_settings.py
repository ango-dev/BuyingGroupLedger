"""The browser Settings page (web/settings_form.py + /settings): derived from config/settings.py,
so THIS file is what fails when a setting is added without the page following.

The standard: a settings change is not done until the browser edits it. The
derivation covers scalars automatically; structured sections are listed in SECTIONS by hand, and
the tests below pin that every top-level list/object in config.example.json has a home there.
"""

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
from config.settings import BOOLEAN_SETTINGS, ENV_TO_CONFIG, Settings  # noqa: E402
from web import settings_form  # noqa: E402
from web.app import create_app  # noqa: E402
from web.ledger_reader import SnapshotReader  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def config(config_file):
    """A config.json with one value of every kind, and the loader pointed at it."""
    config_file(
        scraping={"lookback_days": 3, "default_cashback_rate": "2%",
                  "amazon_promo_cashback_enabled": True},
        google={"sheet_id": "SHEET", "service_account": {"client_email": "x@y"}},
        alerts={"gmail_address": "me@example.com", "gmail_app_password": "hunter2"},
        buying_groups={"sync_enabled": False, "bfmr": {"api_key": "K", "api_secret": "S"}},
        web={"port": 8765},
        profiles=[{"label": "p1", "profile_id": "", "retailers": ["costco"],
                   "// note": "kept"}],
        cards=[{"last4": "0315", "name": "USB Prime Business", "cashback_rate": 0.05}],
    )
    return config_file


@pytest.fixture
def client(config, tmp_path, monkeypatch):
    monkeypatch.delenv("LOOKBACK_DAYS", raising=False)
    monkeypatch.setenv("ALERT_EMAIL_TO", "from-the-environment")
    restarts = []
    container_restarts = []
    app = create_app(SnapshotReader(data_dir=tmp_path), logs_dir=tmp_path, failures_dir=tmp_path,
                     backup_dir=tmp_path / "backups", repo_root_dir=tmp_path, clock=lambda: NOW,
                     settings=dataclasses.replace(Settings(), container_run_interval_hours=6),
                     restarter=lambda: restarts.append(1),
                     container_restarter=lambda: container_restarts.append(1), in_container=True)
    test_client = TestClient(app)
    test_client.restarts = restarts
    test_client.container_restarts = container_restarts
    test_client.logs_dir = tmp_path
    return test_client


# --------------------------------------------------------------------------------------------------
# The derivation, and the drift it refuses
# --------------------------------------------------------------------------------------------------


class TestDerivation:
    def test_every_variable_resolves_to_exactly_one_settings_field(self):
        """Adding a variable to ENV_TO_CONFIG without a `Settings` field -- or with a field whose
        declaration the parser cannot read -- must fail HERE, not render a page with a hole."""
        mapping = settings_form.env_to_field()
        fields = {f.name for f in dataclasses.fields(Settings)}
        missing = [env for env in ENV_TO_CONFIG if env not in mapping]
        assert not missing, f"no Settings field reads {missing}"
        for env, (field, _getter) in mapping.items():
            assert field in fields, f"{env} maps to {field!r}, not a Settings field"

    def test_schema_covers_the_table_in_order_with_the_right_kinds(self):
        schema = settings_form.schema()
        assert [s.env for s in schema] == list(ENV_TO_CONFIG)
        by_env = {s.env: s for s in schema}
        assert {s.env for s in schema if s.kind == "bool"} == BOOLEAN_SETTINGS
        assert by_env["LOOKBACK_DAYS"].kind == "int"
        assert by_env["BFMR_MIN_INSURANCE_VALUE"].kind == "float"
        assert by_env["DEFAULT_CASHBACK_RATE"].kind == "rate"
        assert by_env["GOOGLE_SHEET_ID"].kind == "str"

    def test_secrets_are_the_fields_with_repr_false(self):
        secrets = {s.env for s in settings_form.schema() if s.secret}
        for env in ("GMAIL_APP_PASSWORD", "BFMR_API_KEY", "BFMR_API_SECRET", "MAXOUTDEALS_API_KEY",
                    "OCI_S3_SECRET_ACCESS_KEY", "OCI_PAR_URL_PREFIX", "OCI_FAILURES_PAR_URL_PREFIX",
                    "DISCORD_WEBHOOK_URL"):
            assert env in secrets, env
        assert "GOOGLE_SHEET_ID" not in secrets and "LOOKBACK_DAYS" not in secrets

    def test_help_text_comes_from_the_example_files_comments(self):
        by_env = {s.env: s for s in settings_form.schema()}
        assert by_env["LOOKBACK_DAYS"].help.startswith("LOOKBACK_DAYS")
        assert "CALENDAR days" in by_env["LOOKBACK_DAYS"].help

    def test_every_structured_section_in_the_example_has_a_home(self):
        """The one thing the derivation cannot see: a new list/object section. If one appears in
        config.example.json, SECTIONS must name it (with its model) in the same change."""
        example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        scalar_sections = {path.split(".")[0] for path in ENV_TO_CONFIG.values()}
        # A nested dict that only GROUPS scalars (buying_groups.bfmr, receipts.oci) is covered by
        # the table; a structured section is a list, or a dict the table never reaches into.
        scalar_parents = {path.rsplit(".", 1)[0] for path in ENV_TO_CONFIG.values()}
        structured = {key for key, value in example.items()
                      if not key.startswith("//") and isinstance(value, list)}
        structured |= {f"{key}.{sub}" for key, value in example.items()
                       if isinstance(value, dict) and not key.startswith("//")
                       for sub, subvalue in value.items()
                       if not sub.startswith("//") and isinstance(subvalue, (dict, list))
                       and f"{key}.{sub}" not in scalar_parents}
        homes = {path for path, _shape, _model, _help in settings_form.SECTIONS}
        assert structured <= homes, f"structured section(s) not editable in the browser: {structured - homes}"
        assert not (homes & scalar_sections)


# --------------------------------------------------------------------------------------------------
# Applying a form
# --------------------------------------------------------------------------------------------------


class TestApplyScalars:
    def test_round_trips_every_kind_and_keeps_comment_keys(self, config):
        form = {s.env: "" for s in settings_form.schema()}
        form.update({"LOOKBACK_DAYS": "7", "DEFAULT_CASHBACK_RATE": "3%",
                     "BFMR_MIN_INSURANCE_VALUE": "450.5", "AMAZON_PROMO_CASHBACK_ENABLED": "on",
                     "GOOGLE_SHEET_ID": "NEW-SHEET", "RUN_INTERVAL_HOURS": "4"})

        changes = settings_form.apply_scalars(form)

        assert changes["scraping.lookback_days"] == 7
        assert changes["scraping.default_cashback_rate"] == "3%"
        assert changes["buying_groups.bfmr.min_insurance_value"] == 450.5
        assert changes["google.sheet_id"] == "NEW-SHEET"
        assert config_value("scraping.lookback_days") == 7
        assert config_value("scraping.amazon_promo_cashback_enabled") is True
        # An unticked box is false, and a flag that was true flips.
        assert config_value("buying_groups.sync_enabled") is False
        raw = json.loads(loader.CONFIG_FILE.read_text(encoding="utf-8"))
        assert raw["profiles"][0]["// note"] == "kept"
        assert raw["container"]["run_interval_hours"] == 4  # a section created on the way

    def test_a_blank_secret_keeps_the_stored_value_and_clear_blanks_it(self, config):
        form = {s.env: "" for s in settings_form.schema()}
        settings_form.apply_scalars(form)
        assert config_value("alerts.gmail_app_password") == "hunter2"
        assert config_value("buying_groups.bfmr.api_key") == "K"

        settings_form.apply_scalars({**form, "BFMR_API_KEY": "K2", "GMAIL_APP_PASSWORD__clear": "1"})
        assert config_value("buying_groups.bfmr.api_key") == "K2"
        assert config_value("alerts.gmail_app_password") is None  # blank reads as unset

    def test_a_bad_value_writes_nothing_and_names_the_field(self, config):
        form = {s.env: "" for s in settings_form.schema()}
        before = loader.CONFIG_FILE.read_text(encoding="utf-8")

        with pytest.raises(settings_form.SettingsError) as info:
            settings_form.apply_scalars({**form, "LOOKBACK_DAYS": "three",
                                         "DEFAULT_CASHBACK_RATE": "2", "GOOGLE_SHEET_ID": "X"})

        assert any(e.startswith("LOOKBACK_DAYS:") for e in info.value.errors)
        assert any(e.startswith("DEFAULT_CASHBACK_RATE:") and "0-1" in e for e in info.value.errors)
        assert loader.CONFIG_FILE.read_text(encoding="utf-8") == before
        assert config_value("google.sheet_id") == "SHEET"  # the cache was discarded too


class TestApplySection:
    def test_a_valid_list_is_validated_by_its_model_and_saved(self, config):
        count = settings_form.apply_section("cards", json.dumps([
            {"last4": "1234", "name": "New Card", "cashback_rate": "2%"},
            {"last4": "5678", "name": "Other", "cashback_rate": 0.01},
        ]))
        assert count == 2
        assert [c["last4"] for c in config_value("cards")] == ["1234", "5678"]

    def test_invalid_json_and_invalid_entries_write_nothing(self, config):
        with pytest.raises(settings_form.SettingsError, match="not valid JSON"):
            settings_form.apply_section("cards", "[{")
        with pytest.raises(settings_form.SettingsError, match=r"cards\[0\]"):
            settings_form.apply_section("cards", json.dumps([{"name": "no last4"}]))
        with pytest.raises(settings_form.SettingsError, match="must be a JSON list"):
            settings_form.apply_section("profiles", "{}")
        with pytest.raises(settings_form.SettingsError, match="unknown section"):
            settings_form.apply_section("alerts", "[]")
        assert [c["last4"] for c in config_value("cards")] == ["0315"]

    def test_an_object_section(self, config):
        assert settings_form.apply_section("google.service_account",
                                           '{"client_email": "new@y", "private_key": "k"}') == 2
        assert config_value("google.service_account")["client_email"] == "new@y"
        with pytest.raises(settings_form.SettingsError, match="must be a JSON object"):
            settings_form.apply_section("google.service_account", "[]")


# --------------------------------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------------------------------


class TestSettingsPage:
    def test_every_variable_is_on_the_page_and_no_secret_value_is(self, client):
        response = client.get("/settings")
        assert response.status_code == 200
        body = response.text
        hidden = settings_form.hidden_envs()  # the Sheet-only settings, since the backend is db
        assert hidden and all(env in ENV_TO_CONFIG for env in hidden)
        for env in ENV_TO_CONFIG:
            if env in hidden:
                assert f'name="{env}"' not in body, f"{env} is Sheet-only and the backend is db"
            else:
                assert f'name="{env}"' in body, f"{env} is not on the Settings page"
        for secret in ("hunter2", '"K"', '"S"'):
            assert secret not in body
        assert 'type="password"' in body and "blank keeps it" in body
        assert 'value="3"' in body  # lookback_days
        assert "CALENDAR days" in body  # the help text from the example's comment
        assert 'href="/settings"' in body  # in the nav

    def test_env_override_is_marked(self, client):
        body = client.get("/settings").text
        # ALERT_EMAIL_TO is exported in the fixture; LOOKBACK_DAYS is not.
        sheet_row = body[body.index('for="f-ALERT_EMAIL_TO"'):body.index('for="f-DISCORD_WEBHOOK_URL"')]
        assert "env override" in sheet_row
        lookback_row = body[body.index('for="f-LOOKBACK_DAYS"'):body.index('for="f-DEFAULT_CASHBACK_RATE"')]
        assert "env override" not in lookback_row

    def test_saving_redirects_and_writes(self, client):
        # What a browser submits: every current value, with one edited.
        form = {}
        for row in settings_form.view(settings_form.schema(), {}):
            s = row["setting"]
            if s.kind == "bool":
                if row["value"] is True:
                    form[s.env] = "on"
            elif not s.secret:
                form[s.env] = str(row["value"])
        form["LOOKBACK_DAYS"] = "9"
        response = client.post("/settings", data=form, follow_redirects=False)
        assert response.status_code == 303 and "lookback_days" in response.headers["location"]
        assert config_value("scraping.lookback_days") == 9
        assert "Saved 1 changed setting" in client.get(response.headers["location"]).text

    def test_a_bad_save_is_400_with_the_errors_and_nothing_written(self, client):
        form = {s.env: "" for s in settings_form.schema()}
        response = client.post("/settings", data={**form, "LOOKBACK_DAYS": "nope"})
        assert response.status_code == 400
        assert "Nothing was saved" in response.text and "LOOKBACK_DAYS:" in response.text
        assert config_value("scraping.lookback_days") == 3

    def test_structured_sections_render_and_save(self, client):
        body = client.get("/settings").text
        assert 'action="/settings/section/profiles"' in body and '&#34;p1&#34;' in body or '"p1"' in body
        response = client.post("/settings/section/cards", data={"text": json.dumps(
            [{"last4": "9999", "name": "Card", "cashback_rate": 0.02}])}, follow_redirects=False)
        assert response.status_code == 303
        assert config_value("cards")[0]["last4"] == "9999"

        bad = client.post("/settings/section/cards", data={"text": "[{"})
        assert bad.status_code == 400 and "not valid JSON" in bad.text
        assert "[{" in bad.text  # the submitted text is kept for correction

    def test_restart_calls_the_restarter(self, client):
        response = client.post("/settings/restart")
        assert response.status_code == 200 and client.restarts == [1]

    def test_the_container_can_be_restarted_from_the_page_but_never_mid_run(self, client):
        """a container restart without ssh. It signals PID 1 (the compose
        policy brings the container back) and is refused while the run lock is live."""
        from diagnostics import activity

        body = client.get("/settings").text
        assert 'action="/settings/restart-container"' in body and "Restart container" in body
        (client.logs_dir / ".run.lock").write_text("pid 1", encoding="utf-8")
        refused = client.post("/settings/restart-container")
        assert refused.status_code == 423 and "run is in progress" in refused.text
        assert client.container_restarts == []
        (client.logs_dir / ".run.lock").unlink()
        response = client.post("/settings/restart-container")
        assert response.status_code == 200 and "Restarting the container" in response.text
        assert client.container_restarts == [1]
        assert activity.read(client.logs_dir / "activity.jsonl")[0]["summary"].startswith("Container restart requested")

    def test_outside_a_container_the_button_is_absent_and_the_route_refuses(self, config, tmp_path):
        app = create_app(SnapshotReader(data_dir=tmp_path), logs_dir=tmp_path, failures_dir=tmp_path,
                         backup_dir=tmp_path / "b", repo_root_dir=tmp_path, clock=lambda: NOW,
                         settings=dataclasses.replace(Settings(), container_run_interval_hours=6),
                         restarter=lambda: None, container_restarter=lambda: None, in_container=False)
        desktop = TestClient(app)
        assert 'action="/settings/restart-container"' not in desktop.get("/settings").text
        assert desktop.post("/settings/restart-container").status_code == 400

    def test_the_container_prompt_offers_the_button(self, client):
        body = client.get("/settings", params={"restart": "container"}).text
        banner = body[body.index("restart-prompt"):body.index("</div>", body.index("restart-prompt"))]
        assert "Restart the container now" in banner and 'action="/settings/restart-container"' in banner

    def test_the_compose_mount_is_writable_for_this_page(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        assert "- ./config.json:/app/config.json\n" in compose
        assert "config.json:/app/config.json:ro" not in compose


class TestRestartPrompts:
    """A save says what has to happen for it to take effect -- derived from the entrypoint's own
    export list, so a container knob added there is prompted for without a second list."""

    @staticmethod
    def _form():
        form = {}
        for row in settings_form.view(settings_form.schema(), {}):
            s = row["setting"]
            if s.kind == "bool":
                if row["value"] is True:
                    form[s.env] = "on"
            elif not s.secret:
                form[s.env] = str(row["value"])
        return form

    def test_scopes_are_derived_from_the_entrypoints_exports(self):
        from scripts.container_settings import EXPORTS

        by_env = {s.env: s for s in settings_form.schema()}
        for name, _read in EXPORTS:
            assert settings_form.restart_scope(by_env[name]) == "container", name
        assert settings_form.restart_scope(by_env["WEB_PORT"]) == "dashboard"
        assert settings_form.restart_scope(by_env["LEDGER_DB_PATH"]) == "dashboard"
        assert settings_form.restart_scope(by_env["LOOKBACK_DAYS"]) == "next run"
        assert settings_form.restart_scope(by_env["BFMR_API_KEY"]) == "next run"

    def test_restart_needed_picks_the_strongest_scope(self):
        assert settings_form.restart_needed({"scraping.lookback_days": 2}) == ""
        assert settings_form.restart_needed({"web.port": 1, "scraping.lookback_days": 2}) == "dashboard"
        assert settings_form.restart_needed({"web.port": 1, "container.timezone": "UTC"}) == "container"

    def test_changing_the_schedule_prompts_for_a_container_restart(self, client):
        form = {**self._form(), "RUN_INTERVAL_HOURS": "4"}
        response = client.post("/settings", data=form, follow_redirects=False)
        assert response.status_code == 303 and "restart=container" in response.headers["location"]
        body = client.get(response.headers["location"]).text
        assert "Restart the container to apply this change" in body
        assert "docker compose restart" in body

    def test_changing_a_dashboard_setting_prompts_for_the_button(self, client):
        form = {**self._form(), "WEB_PORT": "9000"}
        response = client.post("/settings", data=form, follow_redirects=False)
        assert "restart=dashboard" in response.headers["location"]
        body = client.get(response.headers["location"]).text
        assert "Restart the dashboard to apply this change" in body
        assert "Restart the container to apply" not in body

    def test_an_ordinary_setting_prompts_for_nothing(self, client):
        form = {**self._form(), "LOOKBACK_DAYS": "5"}
        response = client.post("/settings", data=form, follow_redirects=False)
        assert "restart=" not in response.headers["location"]
        body = client.get(response.headers["location"]).text
        assert "restart-prompt" not in body

    def test_fields_carry_their_scope(self, client):
        body = client.get("/settings").text
        row = body[body.index('for="f-RUN_INTERVAL_HOURS"'):body.index('for="f-RUN_ON_START"')]
        assert "container restart" in row
        row = body[body.index('for="f-WEB_PORT"'):body.index('for="f-LEDGER_DB_PATH"')]
        assert "dashboard restart" in row
        row = body[body.index('for="f-LOOKBACK_DAYS"'):body.index('for="f-DEFAULT_CASHBACK_RATE"')]
        assert "restart" not in row


# --------------------------------------------------------------------------------------------------
# The revamped page: panels, a side index, and ENTRY CARDS for profiles / warehouses / cards

# --------------------------------------------------------------------------------------------------


class TestTheSheetIsDeprecated:
    """`ledger.backend` defaults to db: the Sheet settings are hidden; under `sheet` they show,
    tagged and bannered as deprecated."""

    def test_under_db_the_sheet_settings_are_hidden_and_a_save_leaves_them_alone(self, client):
        body = client.get("/settings").text
        assert settings_form.effective_backend() == "db" and settings_form.sheet_mode() is False
        assert 'id="s-google"' not in body and 'id="s-google.service_account"' not in body
        assert 'href="#s-google"' not in body and "Service Account" not in body
        assert 'name="WEB_LEDGER_SOURCE"' not in body and 'name="LEDGER_DB_MIRROR_AFTER_RUN"' not in body
        assert "are hidden while the backend is" in body
        assert 'value="db"' in body  # the Ledger section shows the (defaulted) backend
        # a full save with the hidden fields absent must not blank / untick them
        form = {}
        for row in settings_form.view(settings_form.schema(), {}):
            s = row["setting"]
            if s.env in settings_form.hidden_envs():
                continue
            if s.kind == "bool":
                if row["value"] is True:
                    form[s.env] = "on"
            elif not s.secret:
                form[s.env] = str(row["value"])
        response = client.post("/settings", data=form, follow_redirects=False)
        assert response.status_code == 303
        assert config_value("google.sheet_id") == "SHEET"
        assert config_value("google.service_account") == {"client_email": "x@y"}
        assert config_value("database.mirror_after_run") in (None, True)

    def test_under_sheet_they_show_with_a_deprecation_warning(self, client, config):
        config(scraping={"lookback_days": 3}, ledger={"backend": "sheet"},
               google={"sheet_id": "SHEET", "service_account": {"client_email": "x@y"}})
        body = client.get("/settings").text
        assert settings_form.sheet_mode() is True and settings_form.hidden_envs() == set()
        assert 'id="s-google"' in body and 'id="s-google.service_account"' in body
        assert body.count("Deprecated.") >= 2  # the Google Sheet panel and the service-account panel
        assert '<section class="panel deprecated" id="s-google">' in body
        google = body[body.index('id="s-google"'):body.index('id="s-alerts"')]
        assert ">deprecated<" in google
        for env in ("WEB_LEDGER_SOURCE", "WEB_SHEET_CACHE_TTL_SECONDS", "LEDGER_DB_MIRROR_AFTER_RUN"):
            field = body[body.index(f'for="f-{env}"'):]
            assert ">deprecated<" in field[:field.index("</div>\n            </div>")]
        # switching back to db from the page hides them again
        form = {s.env: "" for s in settings_form.schema() if not s.secret and s.kind != "bool"}
        form.update({"LOOKBACK_DAYS": "3", "LEDGER_BACKEND": "db"})
        client.post("/settings", data=form)
        assert config_value("ledger.backend") == "db"
        assert 'id="s-google"' not in client.get("/settings").text

    def test_the_default_is_db(self):
        assert settings_form.code_defaults()["LEDGER_BACKEND"] == "db"
        assert Settings().ledger_backend == "db"


class TestEntryCards:
    def test_the_page_is_panels_with_an_index_and_a_card_per_entry(self, client):
        body = client.get("/settings").text
        assert 'class="settings-nav"' in body and 'href="#s-scraping"' in body
        assert 'id="s-scraping"' in body and "<h2>Scraping</h2>" in body
        assert 'id="s-profiles"' in body and 'id="s-cards"' in body and 'id="s-warehouses"' in body
        # one card per entry, its own form; a dashed card to add one; the JSON editor kept behind
        assert 'action="/settings/section/profiles/entry/0"' in body
        assert 'action="/settings/section/cards/entry/0"' in body
        assert 'action="/settings/section/cards/entry"' in body and "+ Add card" in body
        assert 'action="/settings/section/cards/entry/0/delete"' in body
        assert 'action="/settings/section/cards"' in body and "Edit cards as JSON" in body
        # entries are collapsible, keyed for the cookie, with a readable summary line
        assert '<details class="entry-card" data-key="cards:0315">' in body
        assert '<details class="entry-card" data-key="profiles:p1">' in body
        assert 'data-expand="cards"' in body and 'data-collapse="cards"' in body
        assert '<span class="chip">costco</span>' in body  # the profile's retailers, at a glance
        assert "settings-open" in client.get("/static/settings.js").text
        assert "USB Prime Business" in body and 'value="0315"' in body
        assert "No warehouses yet." in body
        assert 'src="/static/settings.js' in body and 'id="settings-confirm"' in body
        # the scalar form's save bar
        assert 'id="scalar-form"' in body and 'id="dirty"' in body

    def test_the_remove_button_sits_in_the_card_footer_and_the_delete_form_is_hidden(self, client):
        body = client.get("/settings").text
        card = body[body.index('action="/settings/section/cards/entry/0"'):body.index("</article>") if "</article>" in body else len(body)]
        card = card[:card.index("</form>")]
        assert 'form="del-cards-0"' in card and "\u2715 Remove" in card  # inside the card's own form
        assert 'id="del-cards-0"' in body and 'action="/settings/section/cards/entry/0/delete"' in body
        assert 'class="entry-delete" hidden' in body and "display: contents" not in body

    def test_a_key_the_file_omits_shows_its_code_default(self, client):
        body = client.get("/settings").text
        # the fixture's config.json has no container section and no gift-card flag
        interval = body[body.index('for="f-RUN_INTERVAL_HOURS"'):body.index('for="f-RUN_ON_START"')]
        assert 'value="6"' in interval and ">default<" in interval
        netting = body[body.index('for="f-AMAZON_GIFT_CARD_NETTING_ENABLED"'):body.index('for="f-GMAIL_ADDRESS"')]
        assert 'value="on" checked' in netting and ">default<" in netting
        # a key the file DOES set carries no tag
        lookback = body[body.index('for="f-LOOKBACK_DAYS"'):body.index('for="f-DEFAULT_CASHBACK_RATE"')]
        assert ">default<" not in lookback
        defaults = settings_form.code_defaults()
        assert defaults["RUN_INTERVAL_HOURS"] == 6 and defaults["WEB_ENABLED"] is True
        assert defaults["GOOGLE_SHEET_WORKSHEET_NAME"] == "Orders" and defaults["GOOGLE_SHEET_ID"] is None

    def test_the_combined_package_account_shows_the_alerts_account_as_its_fallback(self, client, config):
        # the fixture sets alerts.gmail_address / gmail_app_password and no combined-package pair
        body = client.get("/settings").text
        address = body[body.index('for="f-BFMR_COMBINED_PACKAGE_GMAIL_ADDRESS"'):body.index('for="f-BFMR_COMBINED_PACKAGE_GMAIL_APP_PASSWORD"')]
        assert 'value=""' in address and 'placeholder="me@example.com (the alerts account, used while this is blank)"' in address
        assert ">falls back<" in address and ">default<" not in address
        password = body[body.index('for="f-BFMR_COMBINED_PACKAGE_GMAIL_APP_PASSWORD"'):body.index('for="f-MAXOUTDEALS_API_BASE_URL"')]
        assert 'placeholder="uses the alerts app password (set; blank keeps it that way)"' in password
        assert "hunter2" not in password and ">falls back<" in password
        # saving with the field left blank writes NOTHING into it (the placeholder is not a value)
        form = {s.env: "" for s in settings_form.schema() if not s.secret and s.kind != "bool"}
        form["LOOKBACK_DAYS"] = "3"
        client.post("/settings", data=form)
        assert config_value("buying_groups.bfmr.combined_package_gmail_address") in (None, "")
        # its own value, once set, is shown as the value and the fallback tag goes
        config(alerts={"gmail_address": "me@example.com", "gmail_app_password": "hunter2"},
               buying_groups={"bfmr": {"combined_package_gmail_address": "bfmr@example.com"}})
        body = client.get("/settings").text
        address = body[body.index('for="f-BFMR_COMBINED_PACKAGE_GMAIL_ADDRESS"'):body.index('for="f-BFMR_COMBINED_PACKAGE_GMAIL_APP_PASSWORD"')]
        assert 'value="bfmr@example.com"' in address and ">falls back<" not in address
        # both blank: no placeholder at all
        config(alerts={}, buying_groups={"bfmr": {}})
        body = client.get("/settings").text
        address = body[body.index('for="f-BFMR_COMBINED_PACKAGE_GMAIL_ADDRESS"'):body.index('for="f-BFMR_COMBINED_PACKAGE_GMAIL_APP_PASSWORD"')]
        assert "placeholder=" not in address and ">falls back<" not in address

    def test_saving_the_shown_defaults_is_not_a_change_but_does_write_them(self, client):
        form = {}
        for row in settings_form.view(settings_form.schema(), {}):
            s = row["setting"]
            if s.kind == "bool":
                if row["value"] is True:
                    form[s.env] = "on"
            elif not s.secret:
                form[s.env] = str(row["value"])
        response = client.post("/settings", data=form, follow_redirects=False)
        assert response.status_code == 303 and "nothing+had+changed" in response.headers["location"]
        assert config_value("container.run_interval_hours") == 6  # materialised in the file now
        assert config_value("scraping.amazon_gift_card_netting_enabled") is True

    def test_a_card_can_be_added_edited_and_removed(self, client):
        response = client.post("/settings/section/cards/entry", data={
            "name": "Citi Double Cash", "last4": "8765", "cashback_rate": "2%", "profile": "",
            "rate.0.retailer": "amazon", "rate.0.rate": "5%", "rate.1.retailer": "", "rate.1.rate": ""},
            follow_redirects=False)
        assert response.status_code == 303 and "Added+card" in response.headers["location"]
        assert config_value("cards")[1] == {"last4": "8765", "name": "Citi Double Cash",
                                            "cashback_rate": "2%", "retailer_rates": {"amazon": "5%"}}
        response = client.post("/settings/section/cards/entry/1", data={
            "name": "Citi Double Cash", "last4": "8765", "cashback_rate": "0.02", "profile": "p1",
            "rate.0.retailer": "", "rate.0.rate": "5%"}, follow_redirects=False)
        assert response.status_code == 303 and "Saved+card" in response.headers["location"]
        assert config_value("cards")[1] == {"last4": "8765", "name": "Citi Double Cash",
                                            "cashback_rate": 0.02, "profile": "p1"}
        response = client.post("/settings/section/cards/entry/0/delete", follow_redirects=False)
        assert response.status_code == 303 and "Removed+card+USB" in response.headers["location"]
        assert [c["last4"] for c in config_value("cards")] == ["8765"]

    def test_an_invalid_entry_is_400_and_writes_nothing(self, client):
        bad = client.post("/settings/section/cards/entry", data={"name": "Bare", "last4": "1111",
                                                                  "cashback_rate": "2"})
        assert bad.status_code == 400 and "Nothing was saved" in bad.text and "outside 0-1" in bad.text
        assert len(config_value("cards")) == 1
        gone = client.post("/settings/section/cards/entry/7/delete")
        assert gone.status_code == 400 and "does not exist" in gone.text
        jig = client.post("/settings/section/warehouses/entry", data={"buying_group": "BFMR",
                                                                       "jig.0.label": "no fields"})
        assert jig.status_code == 400 and "no match fields" in jig.text
        assert config_value("warehouses") in (None, [])

    def test_a_profile_keeps_its_secrets_and_comment_keys_across_an_edit(self, client, config):
        config(profiles=[{"label": "p1", "profile_id": "", "retailers": ["costco"], "// note": "kept",
                          "proxy": {"host": "h", "port": 1, "username": "u", "password": "pw"},
                          "auth": {"bestbuy": {"method": "password", "username": "me",
                                               "password": "secret", "totp_secret": "SEED"}}}])
        body = client.get("/settings").text
        for secret in ("pw", "secret", "SEED"):
            assert f'value="{secret}"' not in body
        assert 'name="proxy_password__clear"' in body and 'name="auth.bestbuy.totp_secret"' in body
        # a browser submit: blank passwords, the retailers ticked, a second sign-in added
        response = client.post("/settings/section/profiles/entry/0", data={
            "label": "p1", "profile_id": "BU-1", "retailers": ["costco", "bestbuy"],
            "proxy_host": "h", "proxy_port": "1", "proxy_username": "u", "proxy_password": "",
            "auth.bestbuy.username": "me", "auth.bestbuy.password": "", "auth.bestbuy.totp_secret": "",
            "auth_new_retailer": "amazon-business", "auth_new_username": "ab", "auth_new_password": "x"},
            follow_redirects=False)
        assert response.status_code == 303
        saved = config_value("profiles")[0]
        assert saved["// note"] == "kept" and saved["profile_id"] == "BU-1"
        assert saved["retailers"] == ["bestbuy", "costco"]
        assert saved["proxy"]["password"] == "pw"
        assert saved["auth"]["bestbuy"] == {"method": "password", "username": "me",
                                            "password": "secret", "totp_secret": "SEED"}
        assert saved["auth"]["amazon-business"]["password"] == "x"
        # clear the proxy password, remove the bestbuy sign-in, drop the proxy entirely next
        client.post("/settings/section/profiles/entry/0", data={
            "label": "p1", "retailers": "costco", "proxy_host": "h", "proxy_port": "1",
            "proxy_password__clear": "1", "auth.bestbuy.__remove": "1",
            "auth.amazon-business.username": "ab", "auth.amazon-business.password": ""})
        saved = config_value("profiles")[0]
        assert saved["proxy"]["password"] == "" and list(saved["auth"]) == ["amazon-business"]
        client.post("/settings/section/profiles/entry/0", data={"label": "p1", "proxy_host": ""})
        assert "proxy" not in config_value("profiles")[0]

    def test_a_warehouse_form_carries_a_blank_jig_row_that_is_ignored(self, client):
        response = client.post("/settings/section/warehouses/entry", data={
            "buying_group": "BFMR", "jig.0.label": "BFMR-A", "jig.0.street": "13 Sample",
            "jig.0.zip": "03050", "jig.0.contains": "suite 4, dock",
            "jig.1.label": "", "jig.1.street": "", "jig.1.zip": "", "jig.1.name_contains": "",
            "jig.1.contains": ""}, follow_redirects=False)
        assert response.status_code == 303
        assert config_value("warehouses") == [{"buying_group": "BFMR", "jigs": [
            {"label": "BFMR-A", "street": "13 Sample", "zip": "03050",
             "contains": ["suite 4", "dock"]}]}]
        body = client.get("/settings").text
        assert 'name="jig.1.label"' in body and 'placeholder="new jig"' in body
        assert '<table class="entry-table">' in body and "BFMR-A" in body  # jigs are a table
        client.post("/settings/section/warehouses/entry/0", data={
            "buying_group": "BFMR", "jig.0.label": "BFMR-A", "jig.0.street": "13 Sample",
            "jig.0.__remove": "1", "jig.1.label": "B", "jig.1.zip": "99999"})
        assert config_value("warehouses")[0]["jigs"] == [{"label": "B", "zip": "99999"}]

    def test_titles_and_labels_fall_back_to_the_key(self):
        assert settings_form.section_title("scraping")[0] == "Scraping"
        assert settings_form.section_title("brand_new") == ("Brand new", "")
        by_env = {s.env: s for s in settings_form.schema()}
        assert settings_form.field_label(by_env["LOOKBACK_DAYS"]) == ("Lookback days", "")
        assert settings_form.field_label(by_env["BFMR_API_KEY"]) == ("Api key", "bfmr")
        assert settings_form.RETAILER_KEYS == ("amazon", "amazon-business", "bestbuy", "costco")
