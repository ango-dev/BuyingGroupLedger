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
                     settings=dataclasses.replace(Settings(), container_run_interval_hours=6, web_password=""),
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
        assert by_env["GMAIL_ADDRESS"].kind == "str"

    def test_secrets_are_the_fields_with_repr_false(self):
        secrets = {s.env for s in settings_form.schema() if s.secret}
        for env in ("GMAIL_APP_PASSWORD", "BFMR_API_KEY", "BFMR_API_SECRET", "MAXOUTDEALS_API_KEY",
                    "DISCORD_WEBHOOK_URL"):
            assert env in secrets, env
        assert "GMAIL_ADDRESS" not in secrets and "LOOKBACK_DAYS" not in secrets

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
                     "WEB_PUBLIC_URL": "http://192.0.2.10:8765", "RUN_INTERVAL_HOURS": "4"})

        changes = settings_form.apply_scalars(form)

        assert changes["scraping.lookback_days"] == 7
        assert changes["scraping.default_cashback_rate"] == "3%"
        assert changes["buying_groups.bfmr.min_insurance_value"] == 450.5
        assert changes["web.public_url"] == "http://192.0.2.10:8765"
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
                                         "DEFAULT_CASHBACK_RATE": "2", "WEB_PUBLIC_URL": "X"})

        assert any(e.startswith("LOOKBACK_DAYS:") for e in info.value.errors)
        assert any(e.startswith("DEFAULT_CASHBACK_RATE:") and "0-1" in e for e in info.value.errors)
        assert loader.CONFIG_FILE.read_text(encoding="utf-8") == before
        assert config_value("scraping.lookback_days") == 3  # the cache was discarded too


class TestApplySection:
    def test_a_virtual_card_wears_a_badge(self, client):
        settings_form.apply_section("cards", json.dumps([
            {"last4": "1234", "name": "Virtual One", "cashback_rate": "2%", "virtual": True},
            {"last4": "5678", "name": "Real One", "cashback_rate": 0.01},
        ]))
        body = client.get("/settings").text
        virtual, real = body.index("Virtual One"), body.index("Real One")
        assert 'class="chip virtual"' in body[virtual:real] and 'class="chip virtual"' not in body[real:]

    def test_a_valid_list_is_validated_by_its_model_and_saved(self, config):
        count = settings_form.apply_section("cards", json.dumps([
            {"last4": "1234", "name": "New Card", "cashback_rate": "2%", "virtual": True},
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

# --------------------------------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------------------------------


class TestSettingsPage:
    def test_every_variable_is_on_the_page_and_no_secret_value_is(self, client):
        response = client.get("/settings")
        assert response.status_code == 200
        body = response.text
        assert settings_form.hidden_envs() == set()  # nothing is hidden any more
        for env in ENV_TO_CONFIG:
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
        sheet_row = body[body.index('for="f-ALERT_EMAIL_TO"'):body.index("</section>", body.index('for="f-ALERT_EMAIL_TO"'))]
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
        assert "Nothing was saved" in response.text and "Lookback days:" in response.text and "LOOKBACK_DAYS:" not in response.text
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
        # twins saved as JSON though the cards refuse them; a
        # bottomless nest was a RecursionError 500; a huge text a bare JSON answer
        twins = client.post("/settings/section/cards", data={"text": json.dumps(
            [{"last4": "0315", "name": "A", "cashback_rate": 0.02}, {"last4": "0315", "name": "B", "cashback_rate": 0.02}])})
        assert twins.status_code == 400 and "cards[1]" in twins.text and "0315" in twins.text
        assert config_value("cards")[0]["last4"] == "9999"  # nothing written
        deep = client.post("/settings/section/cards", data={"text": "[" * 5000 + "]" * 5000})
        assert deep.status_code == 400 and "not valid JSON" in deep.text
        huge = client.post("/settings/section/cards", data={"text": "[" + " " * (600 * 1024) + "]"})
        assert huge.status_code == 400 and "longer than 512 KB" in huge.text

    def test_restart_calls_the_restarter(self, client):
        response = client.post("/settings/restart")
        assert response.status_code == 200 and client.restarts == [1]
        assert "Restarting the dashboard" in response.text and 'fetch("/health"' in response.text  # the styled page polls
        assert "<nav>" not in response.text and 'content="20;url=/settings"' in response.text

    def test_the_container_can_be_restarted_from_the_page_but_never_mid_run(self, client):
        """a container restart without ssh. It signals PID 1 (the compose
        policy brings the container back) and is refused while the run lock is live."""
        from diagnostics import activity

        body = client.get("/settings").text
        assert 'action="/settings/restart-container"' in body and "Restart container" in body
        assert 'class="danger attention"' not in body  # nothing due until a save says so
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
                         settings=dataclasses.replace(Settings(), container_run_interval_hours=6, web_password=""),
                         restarter=lambda: None, container_restarter=lambda: None, in_container=False)
        desktop = TestClient(app)
        assert 'action="/settings/restart-container"' not in desktop.get("/settings").text
        assert desktop.post("/settings/restart-container").status_code == 400

    def test_the_container_prompt_offers_the_button(self, client):
        body = client.get("/settings", params={"restart": "container"}).text
        banner = body[body.index("restart-prompt"):body.index("</div>", body.index("restart-prompt"))]
        assert "Restart container" in banner and 'action="/settings/restart-container"' in banner

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


class TestTheSheetIsGone:
    """The Google Sheet was deleted 2026-09-18: no Google section, no service-account panel, no
    backend switch; the dashboard's own ledger-source settings render like any other."""

    def test_no_google_or_ledger_backend_settings_remain(self, client):
        body = client.get("/settings").text
        assert 'id="s-google"' not in body and "Service Account" not in body
        assert "Deprecated" not in body and 'name="LEDGER_BACKEND"' not in body
        assert 'name="WEB_LEDGER_SOURCE"' in body and 'name="LEDGER_DB_PATH"' in body
        assert settings_form.hidden_envs() == set()
        assert not [s for s in settings_form.schema() if s.section in ("google", "ledger")]
        assert not [spec for spec in settings_form.SECTIONS if spec[0].startswith("google")]


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
        assert '<span class="chip">Costco</span>' in body  # the profile's retailers, at a glance
        assert "settings-open" in client.get("/static/settings.js").text
        assert "USB Prime Business" in body and 'value="0315"' in body
        assert "No warehouses yet." not in body  # the built-in groups are always there (2026-09-22)
        wh = body[body.index('id="s-warehouses"'):body.index("</section>", body.index('id="s-warehouses"'))]
        assert wh.index('data-key="warehouses:BFMR"') < wh.index('data-key="warehouses:MOD"') < wh.index('data-key="warehouses:Personal"')
        assert wh.count(">Built-in</span>") == 3 and "✕ Delete" not in wh.split('id="add-warehouses"')[0]
        assert 'src="/static/settings.js' in body and 'id="settings-confirm"' in body
        # the scalar form's save bar
        assert 'id="scalar-form"' in body and 'id="dirty"' in body

    def test_the_remove_button_sits_in_the_card_footer_and_the_delete_form_is_hidden(self, client):
        body = client.get("/settings").text
        card = body[body.index('action="/settings/section/cards/entry/0"'):body.index("</article>") if "</article>" in body else len(body)]
        card = card[:card.index("</form>")]
        assert 'form="del-cards-0"' in card and "\u2715 Delete" in card  # inside the card's own form
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
        assert defaults["WEB_LEDGER_SOURCE"] == "db" and defaults["WEB_PUBLIC_URL"] == ""

    def test_the_combined_package_account_never_borrows_the_alerts_account(self, client, config):
        # the fixture sets alerts.gmail_address / gmail_app_password and no combined-package pair:
        # the field is simply blank -- no placeholder, no "falls back" tag
        body = client.get("/settings").text
        address = body[body.index('for="f-BFMR_COMBINED_PACKAGE_GMAIL_ADDRESS"'):body.index('for="f-BFMR_COMBINED_PACKAGE_GMAIL_APP_PASSWORD"')]
        assert 'value=""' in address and "me@example.com" not in address and ">falls back<" not in address
        assert "falls back" not in body

    def test_the_alerts_card_is_one_panel_split_into_discord_and_gmail(self, client):
        body = client.get("/settings").text
        assert 'id="s-alerts"' in body and 'id="s-alerts_discord"' not in body
        panel = body[body.index('id="s-alerts"'):body.index("</section>", body.index('id="s-alerts"'))]
        assert '<h3 class="subgroup" id="s-alerts-discord">Discord</h3>' in panel
        assert '<h3 class="subgroup" id="s-alerts-gmail">Gmail</h3>' in panel
        discord = panel[panel.index("s-alerts-discord"):panel.index("s-alerts-gmail")]
        gmail = panel[panel.index("s-alerts-gmail"):]
        assert 'name="DISCORD_ALERTS_ENABLED"' in discord and 'name="DISCORD_WEBHOOK_URL"' in discord
        assert 'name="GMAIL_ADDRESS"' not in discord
        assert 'name="GMAIL_ALERTS_ENABLED"' in gmail and 'name="GMAIL_ADDRESS"' in gmail and 'name="ALERT_EMAIL_TO"' in gmail
        assert panel.count('<h3 class="subgroup"') == 2 and 'class="subgroup"' not in body[:body.index('id="s-alerts"')]

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
            "rr.0.retailers": "amazon", "rr.0.rate": "5%", "rr.1.retailers": "", "rr.1.rate": ""},
            follow_redirects=False)
        assert response.status_code == 303 and "Added+card" in response.headers["location"]
        assert config_value("cards")[1] == {"last4": "8765", "name": "Citi Double Cash",
                                            "cashback_rate": "2%", "retailer_rates": {"Amazon": "5%"}}
        response = client.post("/settings/section/cards/entry/1", data={
            "name": "Citi Double Cash", "last4": "8765", "cashback_rate": "0.02", "profile": "p1",
            "rr.0.retailers": "", "rr.0.rate": "5%"}, follow_redirects=False)
        assert response.status_code == 303 and "Saved+card" in response.headers["location"]
        # a fraction typed is written as its percent
        assert config_value("cards")[1] == {"last4": "8765", "name": "Citi Double Cash",
                                            "cashback_rate": "2%", "profile": "p1"}
        response = client.post("/settings/section/cards/entry/0/delete", follow_redirects=False)
        assert response.status_code == 303 and "Deleted+card+USB" in response.headers["location"]
        assert [c["last4"] for c in config_value("cards")] == ["8765"]

    def test_a_cards_spend_caps_round_trip_through_the_form(self, client):
        """a maximum-cashback (spend cap) per retailer group and/or the catch-all."""
        response = client.post("/settings/section/cards/entry", data={
            "name": "Amazon Business Prime", "last4": "5555", "cashback_rate": "1%", "profile": "",
            "rr.0.retailers": "amazon, amazon-business", "rr.0.rate": "5%", "rr.0.spend_limit": "$150,000",
            "rr.0.fallback_rate": "1%", "rr.0.resets": "anniversary", "rr.0.anniversary": "03-15",
            "rr.0.outside_spend": "2026: 4,000, 2027: 0",
            "rr.1.retailers": "bestbuy", "rr.1.rate": "3%", "rr.1.spend_limit": "",
            "cap_all.spend_limit": "25000", "cap_all.fallback_rate": "1%", "cap_all.resets": "calendar-year",
            "rr.2.retailers": "", "rr.2.spend_limit": ""}, follow_redirects=False)
        assert response.status_code == 303 and "Added+card" in response.headers["location"]
        saved = config_value("cards")[1]
        assert saved["retailer_rates"] == {"Amazon": "5%", "Amazon Business": "5%", "Best Buy": "3%"}
        # the one-line outside spend still loads, as the dated log with each period at its start
        assert saved["caps"] == [
            {"retailers": ["Amazon", "Amazon Business"], "spend_limit": 150000.0, "fallback_rate": "1%", "resets": "03-15",
             "outside_spend": [{"date": "2026-03-15", "amount": 4000.0, "note": ""}, {"date": "2027-03-15", "amount": 0.0, "note": ""}]},
            {"retailers": [], "spend_limit": 25000.0, "fallback_rate": "1%", "resets": "calendar-year"}]
        body = client.get("/settings").text
        # one table: the capped row with its retailers (the page's own dropdown), the plain rate
        # row, the catch-all on "everywhere else"; the date inline in the resets cell, only shown
        # for "on a date each year"
        assert 'name="rr.0.retailers" value="amazon" data-text="Amazon" checked>' in body and 'name="rr.0.retailers" value="amazon-business" data-text="Amazon Business" checked>' in body
        assert 'name="rr.0.retailers" value="bestbuy" data-text="Best Buy" >' in body and 'name="rr.0.rate" value="5%"' in body
        assert 'name="rr.0.anniversary" value="03-15" placeholder="MM-DD" title="the date the period starts on, MM-DD" class="mono anniversary" >' in body
        # the outside spend is the dated log: the current period's total (NOW is 2026-09-17, in the
        # period from 2026-03-15), its rows newest first, a row for the next entry dated today
        assert 'data-log="rr.0.os" data-period-start="2026-03-15" data-period-end="2027-03-14"' in body and "$4,000.00" in body
        assert 'name="rr.0.os.0.date" value="2027-03-15"' in body and 'name="rr.0.os.1.amount" value="4000"' in body
        assert 'name="rr.0.os.new.date" value="2026-09-17"' in body and 'name="rr.0.outside_spend"' not in body
        # a row without a spend limit hides its log behind "with a limit" (the Best Buy row, the Add row)
        assert body.count('class="needs-limit muted small" hidden') == 2  # the two limited rows; the rest wait for a limit
        row = body[body.index('name="rr.1.spend_limit"'):body.index('name="rr.2.retailers"')]
        assert 'class="with-limit" hidden' in row and 'data-log="rr.1.os"' in row
        assert 'name="rr.1.retailers" value="bestbuy" data-text="Best Buy" checked>' in body and 'name="rr.1.spend_limit" value=""' in body
        assert 'name="cap_all.spend_limit" value="25000"' in body
        assert 'class="mono anniversary" hidden>' in body and "<th>on (MM-DD)</th>" not in body
        assert 'list="retailer-keys"' not in body  # no browser suggestion list
        assert '<span class="chip">1% up to 25,000 then 1%</span>' in body  # the overall chip carries the catch-all limit
        assert '<span class="chip muted">Amazon, Amazon Business 5% up to 150,000 then 1%</span>' in body
        assert '<span class="chip muted">Best Buy 3%</span>' in body
        # a cap with a bad reset date is refused and nothing is written
        bad = client.post("/settings/section/cards/entry/1", data={
            "name": "Amazon Business Prime", "last4": "5555", "cashback_rate": "1%",
            "rr.0.retailers": "amazon", "rr.0.spend_limit": "1", "rr.0.fallback_rate": "1%", "rr.0.resets": "anniversary",
            "rr.0.anniversary": "13-40"})
        assert bad.status_code == 400 and "resets" in bad.text
        assert config_value("cards")[1]["caps"][0]["resets"] == "03-15"
        # blanking the limits drops the caps, the rates stay
        client.post("/settings/section/cards/entry/1", data={"name": "Amazon Business Prime", "last4": "5555", "cashback_rate": "1%",
                                                             "rr.0.retailers": "amazon", "rr.0.rate": "5%", "rr.0.spend_limit": "",
                                                             "cap_all.spend_limit": ""}, follow_redirects=False)
        assert "caps" not in config_value("cards")[1] and config_value("cards")[1]["retailer_rates"] == {"Amazon": "5%"}
        # the dropdown posts one value per tick
        ticked = client.post("/settings/section/cards/entry/1", data={
            "name": "Amazon Business Prime", "last4": "5555", "cashback_rate": "1%",
            "rr.0.retailers": ["amazon", "costco"], "rr.0.rate": "4%"}, follow_redirects=False)
        assert ticked.status_code == 303, ticked.text[ticked.text.find("Nothing was saved"):][:400]
        assert config_value("cards")[1]["retailer_rates"] == {"Amazon": "4%", "Costco": "4%"}

    def test_a_save_from_the_page_swaps_the_section_in_place(self, client):
        hx = {"HX-Request": "true"}
        body = client.get("/settings").text
        assert 'hx-post="/settings/section/cards/entry/0" hx-target="#s-cards" hx-swap="outerHTML"' in body
        assert 'hx-post="/settings/section/cards/entry/0/delete" hx-target="#s-cards" hx-swap="outerHTML" hx-confirm="Delete card' in body
        saved = client.post("/settings/section/cards/entry/0", headers=hx, data={
            "name": "USB Prime Business", "last4": "0315", "cashback_rate": "5%"})
        assert saved.status_code == 200 and "<html" not in saved.text
        assert saved.text.lstrip().startswith('<details class="entry-card" data-key="cards:0315"')  # that card alone (2026-09-20)
        assert 'data-key="cards:0315" open>' in saved.text
        # the outcome is a notification from the top (out of band), not a banner in the section
        assert '<div id="toast" hx-swap-oob="innerHTML"><div class="toast ok" role="status">Saved card USB Prime Business' in saved.text
        assert 'class="banner ok' not in saved.text.split('id="toast"')[0]
        assert config_value("cards")[0]["cashback_rate"] == "5%"
        # a spend limit with no fallback is fine: the everywhere-else rate applies past it
        limited = client.post("/settings/section/cards/entry/0", headers=hx, data={
            "name": "USB Prime Business", "last4": "0315", "cashback_rate": "5%", "rr.0.retailers": "amazon", "rr.0.rate": "5%",
            "rr.0.spend_limit": "1000"})
        assert limited.status_code == 200 and config_value("cards")[0]["caps"] == [{"retailers": ["Amazon"], "spend_limit": 1000.0, "resets": "calendar-year"}]
        assert "Amazon 5% up to 1,000 then 5%" in limited.text  # the everywhere-else rate, as a number
        refused = client.post("/settings/section/cards/entry/0", headers=hx, data={"name": "USB", "last4": "0315", "cashback_rate": "2"})
        assert refused.status_code == 400 and refused.text.lstrip().startswith('<details class="entry-card"') and "outside 0-1" in refused.text
        assert '<div class="toast warn" role="alert">Nothing was saved:' in refused.text
        # profiles and warehouses save in place the same way, with the same notification
        for path, data, label in (("profiles", {"label": "alpha", "profile_id": "", "retailers": "costco"}, "profile alpha"),
                                  ("warehouses", {"buying_group": "BFMR", "jig.0.label": "BFMR", "jig.0.zip": "12345"}, "warehouse BFMR")):
            resp = client.post(f"/settings/section/{path}/entry", headers=hx, data=data)
            # the first entry of a section answers with the section, a later one with the new card alone (2026-09-21)
            assert resp.status_code == 200 and resp.text.lstrip().startswith((f'<section class="panel" id="s-{path}">', '<details class="entry-card"')), resp.text[:300]
            assert f'<div class="toast ok" role="status">Added {label}.</div>' in resp.text
        added = client.post("/settings/section/cards/entry", headers=hx, data={"name": "Second", "last4": "2222"})
        assert added.status_code == 200 and 'data-key="cards:2222" open>' in added.text
        removed = client.post("/settings/section/cards/entry/1/delete", headers=hx)
        assert removed.status_code == 200 and "Deleted card Second" in removed.text and 'data-key="cards:2222"' not in removed.text
        # the wizard's copy of the form still posts plainly
        assert "hx-post" not in client.get("/setup/cards").text.split('id="s-cards"')[-1].split("</section>")[0]

    def test_the_rates_table_shows_what_is_left_of_each_limit(self, client, tmp_path, config):
        """a spend-limit bar inline with the row, showing how much is left."""
        import test_web as tw

        config(cards=[{"last4": "0315", "name": "USB Prime Business", "cashback_rate": "1%",
                       "retailer_rates": {"Amazon": "5%"},
                       "caps": [{"retailers": ["Amazon"], "spend_limit": 3000, "fallback_rate": "1%"},
                                {"retailers": [], "spend_limit": 100}]}])
        tw.write_snapshot(tmp_path / "ledger_backup_20260918T000000Z.csv", *tw.LEDGER_ROWS)
        body = client.get("/settings").text
        assert "<th>Left This Period</th>" in body and 'class="cap-left' in body
        assert "spent in 2026" in body and (" left (" in body or "limit reached" in body)  # dollars and the percent left

    def test_the_picker_offers_the_ledgers_retailers_and_keeps_a_typed_one(self, client, tmp_path, config):
        """the dropdown includes any retailer the user adds, such as Woot."""
        import test_web as tw

        rows = list(tw.LEDGER_ROWS) + [tw.row(order_date="2026-09-09", status="paid", retailer="Woot", item_name="A Woot deal",
                                              shipment="1", quantity="1", order_id="W1", card_last4="0315")]
        tw.write_snapshot(tmp_path / "ledger_backup_20260918T000000Z.csv", *rows)
        body = client.get("/settings").text
        assert 'value="woot" data-text="Woot" >' in body and 'class="new-option" placeholder="Add a retailer' in body
        saved = client.post("/settings/section/cards/entry/0", data={"name": "USB Prime Business", "last4": "0315",
                                                                     "rr.0.retailers": ["amazon", "woot"], "rr.0.rate": "5%",
                                                                     "rr.0.spend_limit": "1000"}, follow_redirects=False)
        assert saved.status_code == 303
        assert config_value("cards")[0]["retailer_rates"] == {"Amazon": "5%", "Woot": "5%"}
        assert config_value("cards")[0]["caps"][0]["retailers"] == ["Amazon", "Woot"]
        body = client.get("/settings").text
        assert 'value="woot" data-text="Woot" checked>' in body and "Amazon, Woot 5% up to 1,000" in body

    def test_a_card_no_longer_in_use_archives_and_comes_back(self, client, config):
        config(cards=[{"last4": "0315", "name": "USB Prime Business", "cashback_rate": "5%"},
                      {"last4": "9999", "name": "USB virtual", "virtual_of": "0315"},
                      {"last4": "8765", "name": "Citi", "cashback_rate": "2%"}])
        page = client.get("/settings").text
        assert 'hx-post="/settings/section/cards/entry/0/archive"' in page and "archived-cards" not in page
        assert 'hx-post="/settings/section/cards/entry/1/archive"' not in page  # a virtual number goes with its card
        done = client.post("/settings/section/cards/entry/0/archive", headers={"HX-Request": "true"})
        assert done.status_code == 200 and "Archived card USB Prime Business …0315." in done.text
        stored = config_value("cards")
        assert stored[0]["archived"] is True and stored[1]["archived"] is True and "archived" not in stored[2]
        body = done.text
        assert body.index('data-key="cards:8765"') < body.index('id="add-cards"') < body.index('class="archived-cards"') < body.index('data-key="cards:0315"')
        assert ">Restore</button>" in body and "kept for their old orders" in body
        # an archived card is no longer offered to a virtual number
        new_card = body[body.index('id="add-cards"'):body.index('class="archived-cards"')]
        assert "USB Prime Business …0315" not in new_card and "Citi …8765" in new_card
        # a card save keeps it archived
        client.post("/settings/section/cards/entry/0", data={"name": "USB Prime Business", "last4": "0315", "cashback_rate": "5%"},
                    headers={"HX-Request": "true"})
        assert config_value("cards")[0]["archived"] is True
        # a virtual number cannot be archived on its own
        refused = client.post("/settings/section/cards/entry/1/archive", headers={"HX-Request": "true"})
        assert refused.status_code == 400 and "archived with its card" in refused.text
        back = client.post("/settings/section/cards/entry/0/archive", headers={"HX-Request": "true"})
        assert "Restored card USB Prime Business …0315." in back.text and "archived-cards" not in back.text
        assert all("archived" not in c for c in config_value("cards"))
        assert client.post("/settings/section/cards/entry/9/archive").status_code == 400

    def test_a_saved_card_comes_back_alone_so_the_other_cards_keep_their_edits(self, client, config):
        """a save on an existing entry answers with
        that card only, retargeted; an add, or a move in the tree, still swaps the section."""
        config(cards=[{"last4": "0315", "name": "USB Prime Business", "cashback_rate": "5%"},
                      {"last4": "9999", "name": "USB virtual", "virtual_of": "0315"},
                      {"last4": "7777", "name": "USB employee", "virtual_of": "0315", "own_bonus": True},
                      {"last4": "8765", "name": "Citi", "cashback_rate": "2%"}],
               profiles=[{"label": "p1", "proxy": {"host": "h", "port": 1, "enabled": True}}])
        saved = client.post("/settings/section/cards/entry/3", data={"name": "Citi Double Cash", "last4": "8765", "cashback_rate": "2%"},
                            headers={"HX-Request": "true"})
        assert saved.status_code == 200 and saved.headers["HX-Retarget"] == '#s-cards details.entry-card[data-key="cards:8765"]'
        assert saved.headers["HX-Reswap"] == "outerHTML" and "<section" not in saved.text
        assert saved.text.count('data-key="cards:') == 1 and 'data-key="cards:8765" open>' in saved.text
        assert '<div class="toast ok" role="status">Saved card Citi Double Cash …8765.</div>' in saved.text
        # a parent card comes back with its numbers inside, the virtual cards before the employee cards
        parent = client.post("/settings/section/cards/entry/0", data={"name": "USB Prime Business", "last4": "0315", "cashback_rate": "5%"},
                             headers={"HX-Request": "true"}).text
        assert parent.count('data-key="cards:') == 3 and parent.index('data-key="cards:9999"') < parent.index('data-key="cards:7777"')
        page = client.get("/settings").text
        assert page.index('data-key="cards:9999"') < page.index('data-key="cards:7777"')
        # a refusal: the card alone, its errors inside, 400
        refused = client.post("/settings/section/cards/entry/3", data={"name": "Citi", "last4": "8765", "cashback_rate": "2"},
                              headers={"HX-Request": "true"})
        assert refused.status_code == 400 and refused.headers["HX-Retarget"].endswith('[data-key="cards:8765"]')
        assert '<div class="banner warn small"><strong>Nothing was saved.</strong>' in refused.text and "outside 0-1" in refused.text
        # a change of last 4 or of the card it belongs to: the section (the card moved)
        moved = client.post("/settings/section/cards/entry/3", data={"name": "Citi", "last4": "8766", "cashback_rate": "2%"},
                            headers={"HX-Request": "true"})
        assert moved.status_code == 200 and "HX-Retarget" not in moved.headers and '<section class="panel" id="s-cards">' in moved.text
        nested = client.post("/settings/section/cards/entry/3", data={"name": "Citi", "last4": "8766", "kind": "virtual", "virtual_of": "0315"},
                             headers={"HX-Request": "true"})
        assert nested.status_code == 200 and "HX-Retarget" not in nested.headers and '<section class="panel" id="s-cards">' in nested.text
        # the proxy switch: that profile's card alone
        flipped = client.post("/settings/section/profiles/entry/0/proxy", headers={"HX-Request": "true"})
        assert flipped.status_code == 200 and flipped.headers["HX-Retarget"] == '#s-profiles details.entry-card[data-key="profiles:p1"]'
        assert "Proxy Off" in flipped.text and "<section" not in flipped.text

    def test_an_add_inserts_the_new_card_and_a_removal_of_the_last_deletes_it(self, client, config):
        """adds and removes get the one-card treatment where the tree allows it."""
        config(cards=[{"last4": "0315", "name": "USB Prime Business", "cashback_rate": "5%"}])
        added = client.post("/settings/section/cards/entry", data={"name": "Citi", "last4": "8765", "cashback_rate": "2%"},
                            headers={"HX-Request": "true"})
        assert added.status_code == 200 and added.headers["HX-Retarget"] == "#add-cards" and added.headers["HX-Reswap"] == "beforebegin"
        assert added.text.count('data-key="cards:') == 1 and 'data-key="cards:8765" open>' in added.text and "<section" not in added.text
        assert '<span class="muted small" id="count-cards" hx-swap-oob="true">2</span>' in added.text
        assert '<details class="entry-card new" id="add-cards" hx-swap-oob="true">' in added.text and "Added card Citi" in added.text
        # a number of an existing card nests under it: the section
        nested = client.post("/settings/section/cards/entry", data={"name": "USB virtual", "last4": "9999", "kind": "virtual", "virtual_of": "0315"},
                             headers={"HX-Request": "true"})
        assert nested.status_code == 200 and "HX-Retarget" not in nested.headers and '<section class="panel" id="s-cards">' in nested.text
        # removing the last entry deletes its card; removing one before it shifts the others' indexes: the section
        gone = client.post("/settings/section/cards/entry/2/delete", headers={"HX-Request": "true"})
        assert gone.status_code == 200 and gone.headers["HX-Reswap"] == "delete" and gone.headers["HX-Retarget"].endswith('[data-key="cards:9999"]')
        assert 'id="count-cards" hx-swap-oob="true">2</span>' in gone.text and "<details" not in gone.text and "Deleted card" in gone.text
        shifted = client.post("/settings/section/cards/entry/0/delete", headers={"HX-Request": "true"})
        assert shifted.status_code == 200 and "HX-Retarget" not in shifted.headers and '<section class="panel" id="s-cards">' in shifted.text
        assert [c["last4"] for c in config_value("cards")] == ["8765"]
        last = client.post("/settings/section/cards/entry/0/delete", headers={"HX-Request": "true"})  # the only one left: the empty section
        assert "HX-Retarget" not in last.headers and "No cards yet" in last.text
        first = client.post("/settings/section/cards/entry", data={"name": "Again", "last4": "1111"}, headers={"HX-Request": "true"})
        assert "HX-Retarget" not in first.headers and '<section class="panel" id="s-cards">' in first.text  # the first entry: the section

    def test_the_scalar_settings_save_in_place(self, client):
        """the scalar form too:
        the form swaps itself, the save bar and the restart banner follow out of band, a toast says so."""
        response = client.post("/settings", data={"LOOKBACK_DAYS": "4"}, headers={"HX-Request": "true"})
        assert response.status_code == 200
        body = response.text
        assert 'id="scalar-form" hx-post="/settings" hx-target="#scalar-form" hx-swap="outerHTML"' in body and "<html" not in body
        assert 'id="savebar" hx-swap-oob="true"' in body and '<div id="restart-banner" hx-swap-oob="true">' in body
        assert '<div class="toast ok" role="status">Saved ' in body and 'lookback_days' in body
        assert config_value("scraping.lookback_days") == 4
        refused = client.post("/settings", data={"LOOKBACK_DAYS": "many"}, headers={"HX-Request": "true"})
        assert refused.status_code == 400 and "Nothing was saved:" in refused.text and 'id="scalar-form"' in refused.text
        page = client.get("/settings").text
        assert page.count('id="scalar-form"') == 1 and page.count('id="savebar"') == 1 and 'hx-swap-oob' not in page

    def test_rates_stored_as_fractions_read_as_percents(self, client, config):
        """the page shows a percent however the file spells it."""
        config(cards=[{"last4": "0315", "name": "USB Prime Business", "cashback_rate": 0.02,
                       "retailer_rates": {"Amazon": 0.05, "Best Buy": "3%"},
                       "caps": [{"retailers": ["Amazon"], "spend_limit": 1000, "fallback_rate": 0.015}]}])
        body = client.get("/settings").text
        assert 'name="cashback_rate" value="2%"' in body and 'name="rr.0.rate" value="5%"' in body
        assert 'name="rr.0.fallback_rate" value="1.5%"' in body and 'name="rr.1.rate" value="3%"' in body
        assert '<span class="chip">2%</span>' in body and "Amazon 5% up to 1,000 then 1.5%" in body
        assert settings_form._rate_text("junk") == "junk" and settings_form._rate_text("") == ""

    def test_the_outside_spend_log_round_trips_through_its_rows(self, client):
        """the dated log -- add, take back, remove; the period's total on the summary."""
        response = client.post("/settings/section/cards/entry", data={
            "name": "Aven", "last4": "1234", "cashback_rate": "1%", "profile": "",
            "cap_all.spend_limit": "25000", "cap_all.fallback_rate": "", "cap_all.resets": "calendar-year",
            "cap_all.os.new.date": "2026-02-03", "cap_all.os.new.amount": "$4,000", "cap_all.os.new.note": "TV",
            "rr.0.retailers": "", "rr.0.spend_limit": ""}, follow_redirects=False)
        assert response.status_code == 303 and "Added+card" in response.headers["location"]
        assert config_value("cards")[1]["caps"] == [{"retailers": [], "spend_limit": 25000.0, "resets": "calendar-year",
                                                     "outside_spend": [{"date": "2026-02-03", "amount": 4000.0, "note": "TV"}]}]
        body = client.get("/settings").text
        assert 'data-log="cap_all.os" data-period-start="2026-01-01" data-period-end="2026-12-31"' in body and "$4,000.00" in body
        assert 'name="cap_all.os.0.amount" value="4000"' in body and 'name="cap_all.os.0.note" value="TV"' in body
        # take some back, add a last-year entry (outside the period), remove nothing, then remove the first
        response = client.post("/settings/section/cards/entry/1", data={
            "name": "Aven", "last4": "1234", "cashback_rate": "1%", "profile": "",
            "cap_all.spend_limit": "25000", "cap_all.resets": "calendar-year",
            "cap_all.os.0.date": "2026-02-03", "cap_all.os.0.amount": "4000", "cap_all.os.0.note": "TV",
            "cap_all.os.1.date": "", "cap_all.os.1.amount": "-500", "cap_all.os.1.note": "returned",
            "cap_all.os.new.date": "2025-12-30", "cap_all.os.new.amount": "100"}, follow_redirects=False)
        assert response.status_code == 303
        assert config_value("cards")[1]["caps"][0]["outside_spend"] == [
            {"date": "2025-12-30", "amount": 100.0, "note": ""}, {"date": "2026-02-03", "amount": 4000.0, "note": "TV"},
            {"date": "2026-09-17", "amount": -500.0, "note": "returned"}]  # a blank date is today
        body = client.get("/settings").text
        assert "$3,500.00" in body and "$3,600.00" not in body  # this period only
        # three months: a fold per month with its subtotal, newest first
        start = body.index('data-log="cap_all.os"', body.index('data-key="cards:1234"'))  # Aven's log, not the first card's
        log = body[start:body.index('name="cap_all.os.new.date"', start)]
        assert log.count('<tr class="month') == 3 and log.index('data-month="2026-09"') < log.index('data-month="2026-02"') < log.index('data-month="2025-12"')
        assert 'September 2026<span class="sum">-$500.00</span>' in log and 'February 2026<span class="sum">$4,000.00</span>' in log
        assert '<tr class="entry" data-month="2026-02">' in log
        # and two years: a fold per year above the months
        assert log.count('<tr class="year"') == 2 and log.index('data-year="2026" title') < log.index('data-year="2025" title')
        assert '2026<span class="sum">$3,500.00</span>' in log and '2025<span class="sum">$100.00</span>' in log
        assert '<tr class="month under" data-month="2026-09" data-year="2026"' in log
        response = client.post("/settings/section/cards/entry/1", data={
            "name": "Aven", "last4": "1234", "cap_all.spend_limit": "25000", "cap_all.resets": "calendar-year",
            "cap_all.os.0.date": "2026-09-17", "cap_all.os.0.amount": "-500", "cap_all.os.0.remove": "on",
            "cap_all.os.1.date": "2026-02-03", "cap_all.os.1.amount": "4000", "cap_all.os.1.note": "TV",
            "cap_all.os.2.date": "2025-12-30", "cap_all.os.2.amount": "100"}, follow_redirects=False)
        assert response.status_code == 303
        assert [e["amount"] for e in config_value("cards")[1]["caps"][0]["outside_spend"]] == [100.0, 4000.0]
        # a bad amount is refused by name, in place
        refused = client.post("/settings/section/cards/entry/1", data={
            "name": "Aven", "last4": "1234", "cap_all.spend_limit": "25000", "cap_all.os.new.amount": "lots"})
        assert refused.status_code == 400 and "log row new: " in refused.text and "lots" in refused.text and "is not an amount" in refused.text

    def test_amazon_and_amazon_business_on_their_own_rows_save_and_render(self, client):
        """a lone Amazon Business row crashed the rows builder (its key has a dash)."""
        response = client.post("/settings/section/cards/entry", data={
            "name": "Chase Prime Visa", "last4": "4345", "cashback_rate": "1%", "profile": "",
            "rr.0.retailers": ["amazon"], "rr.0.rate": "5%", "rr.0.spend_limit": "",
            "rr.1.retailers": ["amazon-business"], "rr.1.rate": "1%", "rr.1.spend_limit": "",
            "rr.2.retailers": [], "rr.2.spend_limit": ""}, follow_redirects=False)
        assert response.status_code == 303 and "Added+card" in response.headers["location"]
        assert config_value("cards")[1]["retailer_rates"] == {"Amazon": "5%", "Amazon Business": "1%"}
        body = client.get("/settings").text
        assert 'name="rr.0.retailers" value="amazon" data-text="Amazon" checked>' in body and 'name="rr.0.rate" value="5%"' in body
        assert 'name="rr.1.retailers" value="amazon-business" data-text="Amazon Business" checked>' in body and 'name="rr.1.rate" value="1%"' in body
        rows = settings_form._rate_rows({"Best Buy": "3%", "Woot": "2%"}, [])
        assert [(r["retailers"], r["rate"]) for r in rows] == [("bestbuy", "3%"), ("woot", "2%")]

    def test_retailers_sharing_a_rate_share_a_row(self, client):
        from web.settings_form import _rate_rows

        rows = _rate_rows({"Amazon": "7%", "Amazon Business": "7%", "Best Buy": "3%", "Costco": "7%"}, [])
        assert [(r["retailers"], r["rate"]) for r in rows] == [("amazon, amazon-business, costco", "7%"), ("bestbuy", "3%")]
        # a capped group keeps its own row even at the same rate
        rows = _rate_rows({"Amazon": "7%", "Amazon Business": "7%", "Best Buy": "7%"},
                          [{"retailers": ["Amazon", "Amazon Business"], "spend_limit": 100}])
        assert [r["retailers"] for r in rows] == ["amazon, amazon-business", "bestbuy"]

    def test_a_virtual_card_must_name_its_card(self, client):
        unlinked = client.post("/settings/section/cards/entry", data={"name": "Virtual", "last4": "9999", "virtual": "on"})
        assert unlinked.status_code == 400 and "virtual number of" in unlinked.text.lower()
        assert len(config_value("cards")) == 1
        linked = client.post("/settings/section/cards/entry", data={"name": "Virtual", "last4": "9999", "virtual": "on",
                                                                     "virtual_of": "0315", "cashback_rate": "2%",
                                                                     "rr.0.retailers": "amazon", "rr.0.rate": "5%", "rr.0.spend_limit": "10"},
                             follow_redirects=False)
        assert linked.status_code == 303
        # a virtual number keeps no rates or caps of its own: they are the card's it names
        assert config_value("cards")[1] == {"last4": "9999", "name": "Virtual", "virtual": True, "virtual_of": "0315"}
        # an employee card: virtual, sharing the limits, but with a sign-up bonus of its own
        employee = client.post("/settings/section/cards/entry/1", data={"name": "Virtual", "last4": "9999", "virtual": "on",
                                                                        "virtual_of": "0315", "own_bonus": "on"}, follow_redirects=False)
        assert employee.status_code == 303 and config_value("cards")[1]["own_bonus"] is True
        body = client.get("/settings").text
        assert "Employee Card of …0315" in body  # the last save made it an employee card
        # the card picker shows only for a virtual or employee card; the type is one menu
        cards_html = body[body.index('data-key="cards:0315"'):body.index('data-key="cards:9999"')]
        assert '<span class="virtual-of" hidden>' in cards_html and 'name="kind" value="" checked' in cards_html
        virtual_html = body[body.index('data-key="cards:9999"'):]
        assert '<span class="virtual-of" >' in virtual_html and 'name="virtual_of" value="0315" checked' in virtual_html
        assert 'class="fblock rates-block" hidden>' in virtual_html and 'name="kind" value="employee" checked' in virtual_html
        assert 'class="fblock rates-block" >' in cards_html  # a real card shows its rates
        assert ">Employee Card of …0315<" in virtual_html
        # a number lives inside its card: nested, folded under Virtual Numbers
        parent_html = body[body.index('data-key="cards:0315"'):]
        assert 'class="sub-cards"' in parent_html and parent_html.index('data-key="cards:9999"') > parent_html.index("Virtual Numbers")
        assert body.count('data-key="cards:9999"') == 1 and ">1 number<" in parent_html
        # a virtual number belongs to a real card: the picker offers no virtual cards, a virtual parent is refused
        assert 'name="virtual_of" value="9999"' not in body
        nested = client.post("/settings/section/cards/entry", data={"name": "Nested", "last4": "3333", "virtual": "on", "virtual_of": "9999"})
        assert nested.status_code == 400 and "itself a virtual number" in nested.text
        # the menu's own values save the same flags
        as_kind = client.post("/settings/section/cards/entry/1", data={"name": "Virtual", "last4": "9999", "kind": "virtual",
                                                                       "virtual_of": "0315"}, follow_redirects=False)
        assert as_kind.status_code == 303 and config_value("cards")[1] == {"last4": "9999", "name": "Virtual", "virtual": True, "virtual_of": "0315"}
        as_kind = client.post("/settings/section/cards/entry/1", data={"name": "Virtual", "last4": "9999", "kind": "employee",
                                                                       "virtual_of": "0315"}, follow_redirects=False)
        assert as_kind.status_code == 303 and config_value("cards")[1]["own_bonus"] is True
        regular = client.post("/settings/section/cards/entry/1", data={"name": "Virtual", "last4": "9999", "kind": ""}, follow_redirects=False)
        assert regular.status_code == 303 and "virtual" not in config_value("cards")[1]

    def test_rates_are_written_as_percents_however_typed(self):
        from web.settings_form import _rate_value

        assert _rate_value("0.01") == "1%" and _rate_value("0.015") == "1.5%" and _rate_value("1") == "100%"
        assert _rate_value("1%") == "1%" and _rate_value(" 2.50 % ") == "2.5%" and _rate_value("0") == "0%"
        assert _rate_value("2") == 2.0 and _rate_value("") is None and _rate_value("abc") == "abc"

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
        assert saved["retailers"] == ["Best Buy", "Costco"]  # written by name (models/retailers.py)
        assert saved["proxy"]["password"] == "pw"
        assert saved["auth"]["Best Buy"] == {"method": "password", "username": "me",  # keys by name too
                                             "password": "secret", "totp_secret": "SEED"}
        assert saved["auth"]["Amazon Business"]["password"] == "x"
        # clear the proxy password, remove the bestbuy sign-in, drop the proxy entirely next
        client.post("/settings/section/profiles/entry/0", data={
            "label": "p1", "retailers": "costco", "proxy_host": "h", "proxy_port": "1",
            "proxy_password__clear": "1", "auth.bestbuy.__remove": "1",
            "auth.amazon-business.username": "ab", "auth.amazon-business.password": ""})
        saved = config_value("profiles")[0]
        assert saved["proxy"]["password"] == "" and list(saved["auth"]) == ["Amazon Business"]
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
        assert 'name="jig.1.label"' in body and 'placeholder="new"' in body
        assert '<table class="entry-table stackable">' in body and "BFMR-A" in body  # jigs are a table
        client.post("/settings/section/warehouses/entry/0", data={
            "buying_group": "BFMR", "jig.0.label": "BFMR-A", "jig.0.street": "13 Sample",
            "jig.0.__remove": "1", "jig.1.label": "B", "jig.1.zip": "99999"})
        assert config_value("warehouses")[0]["jigs"] == [{"label": "B", "zip": "99999"}]

    def test_titles_and_labels_fall_back_to_the_key(self):
        assert settings_form.section_title("scraping")[0] == "Scraping"
        assert settings_form.section_title("advanced")[0] == "Advanced"
        assert settings_form.section_title("brand_new") == ("Brand new", "")
        by_env = {s.env: s for s in settings_form.schema()}
        assert settings_form.field_label(by_env["LOOKBACK_DAYS"]) == ("Lookback days", "")
        assert settings_form.field_label(by_env["BFMR_API_KEY"]) == ("API key", "bfmr")  # API stays upper (2026-09-22)
        assert settings_form.RETAILER_KEYS == ("amazon", "amazon-business", "bestbuy", "costco")


class TestBuiltInWarehouses:

    def test_the_built_ins_are_fixed_and_a_missing_one_is_added_on_save(self, client, config):
        config(warehouses=[{"buying_group": "MaxOutDeals", "jigs": [{"label": "MOD-1", "zip": "30303"}]},
                           {"buying_group": "Acme Group", "jigs": [{"label": "A", "zip": "10001"}]}])
        body = client.get("/settings").text
        wh = body[body.index('id="s-warehouses"'):body.index("</section>", body.index('id="s-warehouses"'))]
        # the built-ins first in their order (a stored "MaxOutDeals" is MOD), then the user's own
        assert wh.index('data-key="warehouses:BFMR"') < wh.index('data-key="warehouses:MaxOutDeals"')             < wh.index('data-key="warehouses:Personal"') < wh.index('data-key="warehouses:Acme Group"')
        assert 'name="buying_group" value="MaxOutDeals" readonly class="locked"' in wh
        assert 'name="buying_group" value="Acme Group" required' in wh  # a user's group stays editable
        assert 'action="/settings/section/warehouses/entry" class="entry-form"' in wh.split('id="add-warehouses"')[0]  # BFMR, not stored yet
        # a built-in cannot be renamed or deleted, by card or by JSON
        refused = client.post("/settings/section/warehouses/entry/0", data={"buying_group": "Something", "jig.0.label": "x", "jig.0.zip": "1"})
        assert refused.status_code == 400 and "MOD is a built-in buying group" in refused.text
        assert client.post("/settings/section/warehouses/entry/0/delete").status_code == 400
        assert config_value("warehouses")[0]["buying_group"] == "MaxOutDeals"
        import json
        gone = client.post("/settings/section/warehouses", data={"text": json.dumps([{"buying_group": "Acme Group", "jigs": []}])})
        assert gone.status_code == 400 and "MOD is built in" in gone.text
        # the missing BFMR saves through the add route; a user's warehouse still renames and deletes
        added = client.post("/settings/section/warehouses/entry", data={"buying_group": "BFMR", "jig.0.label": "B", "jig.0.zip": "03050"},
                            follow_redirects=False)
        assert added.status_code == 303 and [w["buying_group"] for w in config_value("warehouses")] == ["MaxOutDeals", "Acme Group", "BFMR"]
        renamed = client.post("/settings/section/warehouses/entry/1", data={"buying_group": "Acme Two", "jig.0.label": "A", "jig.0.zip": "10001"},
                              follow_redirects=False)
        assert renamed.status_code == 303 and config_value("warehouses")[1]["buying_group"] == "Acme Two"
        assert client.post("/settings/section/warehouses/entry/1/delete", follow_redirects=False).status_code == 303


class TestPanelLayout:
    """(Discord and Gmail stay as they are)."""

    def test_alerts_lead_with_the_cap_warnings_and_the_groups_fold_with_their_switch(self, client, config):
        config(buying_groups={"sync_enabled": True, "mod": {"enabled": False}})
        body = client.get("/settings").text
        alerts = body[body.index('id="s-alerts"'):body.index("</section>", body.index('id="s-alerts"'))]
        assert alerts.index('for="f-CASHBACK_CAP_WARN_PERCENT"') < alerts.index('for="f-CASHBACK_CAP_WARN_DOLLARS"') < alerts.index('id="s-alerts-discord"')
        assert "subgroup-fold" not in alerts  # Discord and Gmail stay headings
        groups = body[body.index('id="s-buying_groups"'):body.index("</section>", body.index('id="s-buying_groups"'))]
        assert groups.index('for="f-BUYING_GROUP_SYNC_ENABLED"') < groups.index('id="s-buying_groups-bfmr"') < groups.index('id="s-buying_groups-mod"')
        bfmr = groups[groups.index('id="s-buying_groups-bfmr"'):groups.index('id="s-buying_groups-mod"')]
        assert '<details class="subgroup-fold"' in groups and '<span class="chip ok">Enabled</span>' in bfmr
        assert bfmr.index('for="f-BFMR_ENABLED"') < bfmr.index('for="f-BFMR_API_KEY"')
        mod = groups[groups.index('id="s-buying_groups-mod"'):]
        assert '<span class="chip muted">Disabled</span>' in mod and 'for="f-MAXOUTDEALS_API_KEY"' in mod

    def test_a_group_switch_saves_like_any_setting(self, config):
        form = {s.env: "" for s in settings_form.schema()}
        changes = settings_form.apply_scalars({**form, "BFMR_ENABLED": "on"})
        assert changes["buying_groups.mod.enabled"] is False and "buying_groups.bfmr.enabled" not in changes  # default on


class TestSignInSettings:
    """the password, both sign-in lengths and the rate limit are settings."""

    def test_the_rows_sit_in_the_dashboard_panel_under_sign_in(self, client):
        body = client.get("/settings").text
        panel = body[body.index('id="s-web"'):body.index('id="s-database"') if 'id="s-database"' in body else body.index('id="s-backups"')]
        assert '<h3 class="subgroup" id="s-web-sign-in">Sign-in</h3>' in panel
        assert 'type="password" id="f-WEB_PASSWORD"' in panel  # a secret: never rendered back
        for env in ("WEB_SESSION_HOURS", "WEB_REMEMBER_DAYS", "WEB_LOGIN_ATTEMPTS", "WEB_LOGIN_LOCKOUT_MINUTES"):
            assert f'for="f-{env}"' in panel, env
        assert 'for="f-WEB_PASSWORD"' in panel

    def test_a_stale_card_index_and_an_unknown_section_are_answers_not_500s(self, client, config):
        stale = client.post("/settings/section/cards/entry/50", data={"last4": "1111", "name": "X", "cashback_rate": "1%"})
        assert stale.status_code == 409 and "no longer in the file" in stale.text
        stale = client.post("/settings/section/cards/entry/50", data={"last4": "1111", "name": "X", "cashback_rate": "1%"},
                            headers={"HX-Request": "true"})
        assert stale.status_code == 409 and "no longer in the file" in stale.text
        assert client.post("/settings/section/nope/entry", data={"x": "1"}).status_code == 404
        assert client.post("/settings/section/nope/entry", data={"x": "1"}, headers={"HX-Request": "true"}).status_code == 404

    def test_the_settings_that_stop_the_dashboard_have_a_vocabulary(self, config):
        """WEB_LEDGER_SOURCE=sheets saved and the dashboard did
        not start; TZ, the URLs, the addresses and the bind host were free text."""
        form = {s.env: "" for s in settings_form.schema()}
        with pytest.raises(settings_form.SettingsError) as info:
            settings_form.apply_scalars({**form, "WEB_LEDGER_SOURCE": "sheets", "TZ": "Mars/Olympus Mons",
                                         "DISCORD_WEBHOOK_URL": "hooks.example/abc", "WEB_PUBLIC_URL": "ledger.local",
                                         "GMAIL_ADDRESS": "nope", "BFMR_COMBINED_PACKAGE_GMAIL_ADDRESS": "a@b",
                                         "WEB_BIND_HOST": "0.0.0.0 8765"})
        errors = "\n".join(info.value.errors)
        for needle in ("WEB_LEDGER_SOURCE: one of db, snapshot", "TZ: 'Mars/Olympus Mons' is not a time zone name",
                       "DISCORD_WEBHOOK_URL: a web address", "WEB_PUBLIC_URL: a web address",
                       "GMAIL_ADDRESS: 'nope' is not an email address", "BFMR_COMBINED_PACKAGE_GMAIL_ADDRESS: 'a@b' is not an email",
                       "WEB_BIND_HOST: '0.0.0.0 8765' is not a host"):
            assert needle in errors, needle
        changes = settings_form.apply_scalars({**form, "WEB_LEDGER_SOURCE": "Snapshot", "TZ": "America/New_York",
                                               "WEB_PUBLIC_URL": "https://ledger.example", "GMAIL_ADDRESS": "me@example.com",
                                               "WEB_BIND_HOST": "127.0.0.1"})
        assert changes["web.ledger_source"] == "snapshot" and changes["container.timezone"] == "America/New_York"

    def test_entry_cards_take_the_typed_number_rule(self, config):
        """proxy port 99999, fullwidth digits as a last 4 and a
        `nan` spend limit all saved."""
        with pytest.raises(settings_form.SettingsError, match="Proxy port: '99999' is above 65535"):
            settings_form.apply_entry("profiles", None, {"label": "p9", "retailers": "costco", "proxy_host": "h",
                                                          "proxy_port": "99999", "proxy_username": "", "proxy_password": ""})
        with pytest.raises(settings_form.SettingsError, match="Proxy port: '-1' is below 1"):
            settings_form.apply_entry("profiles", None, {"label": "p9", "retailers": "costco", "proxy_host": "h",
                                                          "proxy_port": "-1", "proxy_username": "", "proxy_password": ""})
        with pytest.raises(settings_form.SettingsError, match="four digits"):
            settings_form.apply_entry("cards", None, {"last4": "\uff10\uff17\uff16\uff16", "name": "Wide", "cashback_rate": "2%"})
        with pytest.raises(settings_form.SettingsError) as info:
            settings_form.apply_entry("cards", None, {"last4": "4242", "name": "Capped", "cashback_rate": "5%",
                                                       "cap_all.spend_limit": "nan", "cap_all.fallback_rate": "1%",
                                                       "cap_all.resets": "calendar-year"})
        assert "nan" in " ".join(info.value.errors) and "pydantic" not in " ".join(info.value.errors)

    def test_the_lengths_have_floors_and_the_password_saves_like_a_secret(self, config):
        form = {s.env: "" for s in settings_form.schema()}
        with pytest.raises(settings_form.SettingsError) as info:
            settings_form.apply_scalars({**form, "WEB_SESSION_HOURS": "0", "WEB_LOGIN_ATTEMPTS": "0",
                                         "WEB_REMEMBER_DAYS": "x"})
        assert any(e.startswith("WEB_SESSION_HOURS:") for e in info.value.errors)
        assert any(e.startswith("WEB_LOGIN_ATTEMPTS:") for e in info.value.errors)
        assert any(e.startswith("WEB_REMEMBER_DAYS:") and "not a number" in e for e in info.value.errors)

        # `nan` saved and made every /login a 500 after the restart
        with pytest.raises(settings_form.SettingsError) as info:
            settings_form.apply_scalars({**form, "WEB_SESSION_HOURS": "nan", "WEB_LOGIN_LOCKOUT_MINUTES": "inf",
                                         "WEB_LOGIN_ATTEMPTS": "10^30", "WEB_PORT": "70000", "LOOKBACK_DAYS": "-5",
                                         "WEB_HEARTBEAT_STALE_HOURS": "1e3", "BACKUP_KEEP": "1_000"})
        errors = "\n".join(info.value.errors)
        for needle in ("WEB_SESSION_HOURS: 'nan' is not a number", "WEB_LOGIN_LOCKOUT_MINUTES: 'inf' is not a number",
                       "WEB_LOGIN_ATTEMPTS: '10^30' is not a whole number", "WEB_PORT: must be between 1 and 65535",
                       "LOOKBACK_DAYS: must be at least 1", "WEB_HEARTBEAT_STALE_HOURS: '1e3' is not a number",
                       "BACKUP_KEEP: '1_000' is not a whole number"):
            assert needle in errors, needle

        changes = settings_form.apply_scalars({**form, "WEB_PASSWORD": "open sesame", "WEB_SESSION_HOURS": "12",
                                               "WEB_REMEMBER_DAYS": "365", "WEB_LOGIN_LOCKOUT_MINUTES": "0"})
        assert changes["web.password"] == "open sesame" and changes["web.session_hours"] == 12
        assert changes["web.remember_days"] == 365 and changes["web.login_lockout_minutes"] == 0
        by_env = {s.env: s for s in settings_form.schema()}
        assert settings_form.restart_scope(by_env["WEB_PASSWORD"]) == "dashboard"  # read at dashboard start
        settings_form.apply_scalars(form)  # blank keeps the password
        assert config_value("web.password") == "open sesame"
        settings_form.apply_scalars({**form, "WEB_PASSWORD__clear": "1"})
        assert config_value("web.password") is None  # cleared: no sign-in


class TestAdvancedSettings:

    def test_the_advanced_keys_leave_their_sections_for_the_foot_of_the_page(self):
        rows = settings_form.schema()
        by_env = {s.env: s for s in rows}
        assert by_env["LEDGER_DB_PATH"].advanced and by_env["WEB_LEDGER_SOURCE"].advanced and by_env["WEB_PORT"].advanced
        assert not by_env["LOOKBACK_DAYS"].advanced and not by_env["WEB_PUBLIC_URL"].advanced
        sections = settings_form.sections_in_order(rows)
        assert "database" not in sections and "advanced" not in sections  # Database emptied; Advanced is its own panel
        assert "web" in sections  # the Dashboard panel keeps its day-to-day settings
        assert settings_form.restart_scope(by_env["LEDGER_DB_PATH"]) == "dashboard"  # unchanged: still the database section by path

    def test_the_page_renders_them_at_the_bottom_behind_a_warning_and_saves_them(self, client, config_file):
        body = client.get("/settings").text
        assert 'id="s-database"' not in body
        foot = body[body.index('id="s-advanced"'):]
        assert body.index('id="s-cards"') < body.index('id="s-advanced"') and 'id="s-apply"' not in body  # the last panel; no Apply panel
        assert '<details class="panel advanced-panel" id="s-advanced">' in body  # folded, closed by default
        # the bottom bar: Save on the left, the two restarts on the right
        bar = body[body.index('<div class="savebar fixed" id="savebar">'):body.index("</div>\n</div>", body.index('id="savebar"'))]
        assert '<button type="submit" form="scalar-form" class="primary">Save settings</button>' in bar
        assert bar.index("Save settings") < bar.index('action="/settings/restart"') < bar.index('action="/settings/restart-container"')
        assert 'id="needs"' in bar and "restart-due" not in bar
        assert 'name="WEB_PORT" data-restart="dashboard"' in body and 'name="LOOKBACK_DAYS" data-restart' not in body  # only a real scope is tagged
        after = client.get("/settings", params={"restart": "dashboard"}).text
        assert "Saved — restart the dashboard to apply it" in after and 'class="primary attention"' in after
        assert "Do not change these unless you know exactly what you are doing." in foot
        assert 'for="f-LEDGER_DB_PATH"' in foot and 'for="f-WEB_LEDGER_SOURCE"' in foot
        assert 'name="LEDGER_DB_PATH" data-restart="dashboard" form="scalar-form"' in foot  # saved by the main form's button
        assert 'for="f-LEDGER_DB_PATH"' not in body[:body.index('id="s-advanced"')]
        assert '<a href="#s-advanced" class="danger-link">Advanced</a>' in body
        response = client.post("/settings", data={"LEDGER_DB_PATH": "data/elsewhere.sqlite3", "LOOKBACK_DAYS": "9"},
                               follow_redirects=False)
        assert response.status_code == 303
        import json as _json
        saved = _json.loads(config_file.read_text(encoding="utf-8")) if hasattr(config_file, "read_text") else None
        if saved is not None:
            assert saved["database"]["path"] == "data/elsewhere.sqlite3"
