"""Who may reach the dashboard, and who may send it a write (web/guard.py): the
Host check that stops DNS rebinding, the cross-site write refusal, and the first-password page a
network-reachable dashboard with no password serves instead of everything else."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from config import loader
from config.settings import settings
from ledger.sync import HEADER
from web import auth, guard
from web.app import create_app
from web.ledger_reader import SnapshotReader

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def _app(tmp_path: Path, *, exposure="127.0.0.1", restarts=None, **values):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "ledger_backup_20260922T000000Z.csv").write_text(",".join(HEADER) + "\n", encoding="utf-8")
    logs = tmp_path / "logs"
    (logs / "failures").mkdir(parents=True, exist_ok=True)
    values = {"container_run_interval_hours": 6, "web_password": "", "web_allowed_hosts": "",
              "web_public_url": "", **values}
    restarts = restarts if restarts is not None else []
    app = create_app(SnapshotReader(data_dir=data), logs_dir=logs, failures_dir=logs / "failures",
                     backup_dir=tmp_path / "backups", repo_root_dir=tmp_path, clock=lambda: NOW,
                     settings=dataclasses.replace(settings, **values), session_secret="a-test-secret",
                     restarter=lambda: restarts.append(True), exposure_host=exposure)
    return TestClient(app)


# --------------------------------------------------------------------------------------------------
# The rules
# --------------------------------------------------------------------------------------------------


class TestRules:
    def test_hostname_strips_port_and_brackets(self):
        assert guard.hostname("Ledger.Local:8765") == "ledger.local"
        assert guard.hostname("[::1]:8765") == "::1" and guard.hostname("::1") == "::1"
        assert guard.hostname("10.0.0.5:8765") == "10.0.0.5"

    def test_loopback(self):
        for host in ("127.0.0.1", "127.8.9.1", "::1", "[::1]", "localhost"):
            assert guard.is_loopback(host), host
        for host in ("0.0.0.0", "192.168.1.20", "192.0.2.10", "ledger.local", ""):
            assert not guard.is_loopback(host), host

    def test_a_host_is_answered_by_address_localhost_or_a_listed_name(self):
        allowed = guard.allowed_names("ledger.local, .ts.net", "https://books.example:8443/")
        for host in ("192.168.1.20:8765", "[fe80::1]:8765", "localhost:8765", "testserver",
                     "ledger.local:8765", "pi.tailnet.ts.net", "books.example:8443"):
            assert guard.host_allowed(host, allowed), host
        for host in ("attacker.example", "ledger.local.attacker.example", "evil-ts.net"):
            assert not guard.host_allowed(host, allowed), host

    def test_a_write_from_another_site_is_cross_site_and_a_read_never_is(self):
        assert guard.cross_site("POST", {"sec-fetch-site": "cross-site"})
        assert guard.cross_site("POST", {"sec-fetch-site": "same-site"})  # another port or subdomain
        assert not guard.cross_site("POST", {"sec-fetch-site": "same-origin"})
        assert not guard.cross_site("POST", {"sec-fetch-site": "none"})
        assert not guard.cross_site("GET", {"sec-fetch-site": "cross-site"})
        # no Sec-Fetch-Site (an older browser): the Origin against the Host
        assert guard.cross_site("POST", {"origin": "https://attacker.example", "host": "127.0.0.1:8765"})
        assert guard.cross_site("POST", {"origin": "null", "host": "127.0.0.1:8765"})
        assert not guard.cross_site("POST", {"origin": "http://127.0.0.1:8765", "host": "127.0.0.1:8765"})
        assert not guard.cross_site("POST", {"host": "127.0.0.1:8765"})  # curl, the test client
        # behind a proxy that rewrites the Host, an origin the operator named is theirs
        assert not guard.cross_site("POST", {"origin": "https://ledger.example", "host": "127.0.0.1:8765"},
                                    ("ledger.example",))


# --------------------------------------------------------------------------------------------------
# The app
# --------------------------------------------------------------------------------------------------


class TestTheHostCheck:
    def test_an_unknown_name_is_refused_and_an_address_or_listed_name_answered(self, tmp_path):
        client = _app(tmp_path, web_allowed_hosts="ledger.local")
        assert client.get("/", headers={"host": "attacker.example"}).status_code == 400
        refused = client.get("/", headers={"host": "attacker.example:8765"})
        assert "Allowed hosts" in refused.text
        assert client.get("/", headers={"host": "192.168.1.20:8765"}).status_code == 200
        assert client.get("/", headers={"host": "ledger.local:8765"}).status_code == 200
        assert client.get("/health", headers={"host": "attacker.example"}).status_code == 200  # the probe


class TestTheCrossSiteRefusal:
    def test_a_form_posted_from_another_site_is_refused_and_the_dashboards_own_is_not(self, tmp_path):
        client = _app(tmp_path)
        before = loader.CONFIG_FILE.read_text(encoding="utf-8") if loader.CONFIG_FILE.exists() else None
        forged = client.post("/settings", data={"LOOKBACK_DAYS": "9"},
                             headers={"origin": "https://attacker.example", "sec-fetch-site": "cross-site"})
        assert forged.status_code == 403 and "another site" in forged.text
        after = loader.CONFIG_FILE.read_text(encoding="utf-8") if loader.CONFIG_FILE.exists() else None
        assert after == before  # nothing written
        own = client.post("/tools/run/preflight", data={}, follow_redirects=False,
                          headers={"origin": "http://testserver", "sec-fetch-site": "same-origin"})
        assert own.status_code != 403


class TestTheFirstPassword:
    def test_on_loopback_without_a_password_the_dashboard_is_open_as_before(self, tmp_path):
        client = _app(tmp_path, exposure="127.0.0.1")
        assert client.get("/", follow_redirects=False).status_code == 200
        assert client.get("/first-password", follow_redirects=False).headers["location"] == "/"

    def test_reachable_beyond_loopback_it_serves_only_the_password_page(self, tmp_path, capsys):
        client = _app(tmp_path, exposure="0.0.0.0")
        token = capsys.readouterr().err.split("Setup token: ")[1].split()[0]
        for path in ("/", "/settings", "/backup/anything.zip", "/orders"):
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 303 and response.headers["location"] == "/first-password", path
        assert client.post("/tools/run/preflight", data={}, follow_redirects=False).headers["location"] == "/first-password"
        hx = client.get("/orders", headers={"HX-Request": "true"})
        assert hx.status_code == 401 and hx.headers["HX-Redirect"] == "/first-password"
        assert client.get("/health").status_code == 200
        page = client.get("/first-password")
        assert page.status_code == 200 and "Set a Password" in page.text and token not in page.text

    def test_the_password_is_set_only_with_the_token_and_then_the_dashboard_restarts(self, tmp_path, capsys):
        restarts = []
        client = _app(tmp_path, exposure="192.168.1.20", restarts=restarts)
        token = capsys.readouterr().err.split("Setup token: ")[1].split()[0]
        wrong = client.post("/first-password", data={"token": "guess", "password": "pw-1", "confirm": "pw-1"})
        assert wrong.status_code == 403 and "setup token" in wrong.text and not restarts
        assert not loader.config_value("web.password")
        differ = client.post("/first-password", data={"token": token, "password": "pw-1", "confirm": "pw-2"})
        assert differ.status_code == 400 and "differ" in differ.text
        blank = client.post("/first-password", data={"token": token, "password": " ", "confirm": " "})
        assert blank.status_code == 400 and "required" in blank.text
        done = client.post("/first-password", data={"token": token, "password": "pw-1", "confirm": "pw-1"})
        assert done.status_code == 200 and "Password Set" in done.text and restarts == [True]
        assert loader.config_value("web.password") == "pw-1"
        assert auth.COOKIE in done.headers.get("set-cookie", "")  # this browser is signed in for after
        raw = json.dumps(loader.load_config())
        assert token not in raw
        # still locked until the restart puts the password in force
        assert client.get("/settings", follow_redirects=False).headers["location"] == "/first-password"

    def test_with_a_password_set_nothing_is_locked(self, tmp_path, capsys):
        client = _app(tmp_path, exposure="0.0.0.0", web_password="already set")
        assert "Setup token" not in capsys.readouterr().err
        assert client.get("/", follow_redirects=False).headers["location"].startswith("/login")
