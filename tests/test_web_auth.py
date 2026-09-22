"""The dashboard's sign-in (web/auth.py: "add a password with a rate limit and a
remember me button; remember me lasts 2 years, editable in the settings, and if not pressed 6
hours, also editable")."""

from __future__ import annotations

import dataclasses
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from config import loader
from config.settings import settings
from diagnostics import activity as activity_module
from ledger.sync import HEADER
from web import auth
from web.app import create_app
from web.ledger_reader import SnapshotReader

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
PASSWORD = "open sesame"


@pytest.fixture
def clock():
    """A clock the tests move: {"now": datetime}."""
    return {"now": NOW}


def _app(tmp_path: Path, clock: dict, **overrides):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "ledger_backup_20260917T000000Z.csv").write_text(",".join(HEADER) + "\n", encoding="utf-8")
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "failures").mkdir(exist_ok=True)
    (logs / ".last_run").write_text("2026-09-17T09:00:00Z", encoding="utf-8")
    values = {"container_run_interval_hours": 6, "web_password": PASSWORD, "web_session_hours": 6,
              "web_remember_days": 730, "web_login_attempts": 3, "web_login_lockout_minutes": 15,
              **overrides}
    app = create_app(SnapshotReader(data_dir=data), logs_dir=logs, failures_dir=logs / "failures",
                     backup_dir=tmp_path / "backups", repo_root_dir=tmp_path,
                     clock=lambda: clock["now"], settings=dataclasses.replace(settings, **values),
                     session_secret="a-test-secret")
    client = TestClient(app)
    client.logs_dir = logs
    return client


@pytest.fixture
def client(tmp_path, clock):
    return _app(tmp_path, clock)


def _sign_in(client, remember=False, password=PASSWORD, nxt="/"):
    data = {"password": password, "next": nxt}
    if remember:
        data["remember"] = "on"
    return client.post("/login", data=data, follow_redirects=False)


def _max_age(response) -> int:
    match = re.search(r"Max-Age=(\d+)", response.headers.get("set-cookie", ""), re.I)
    assert match, response.headers.get("set-cookie")
    return int(match.group(1))


# --------------------------------------------------------------------------------------------------
# The pieces
# --------------------------------------------------------------------------------------------------


class TestDescribe:
    def test_whole_units_and_the_settings_defaults(self):
        assert auth.describe(730 * 86400) == "2 years"
        assert auth.describe(6 * 3600) == "6 hours"
        assert auth.describe(1 * 3600) == "1 hour"
        assert auth.describe(15 * 60) == "15 minutes"
        assert auth.describe(3 * 86400) == "3 days"
        assert auth.describe(90 * 60) == "90 minutes"
        assert auth.describe(547.5 * 86400) == "13140 hours"  # only whole units: a year and a half is not "1.5 years"


class TestSafeNext:
    def test_only_a_path_on_this_dashboard(self):
        assert auth.safe_next("/orders?page=2") == "/orders?page=2"
        assert auth.safe_next("") == "/" and auth.safe_next(None) == "/"
        assert auth.safe_next("//evil.example/x") == "/"
        assert auth.safe_next("http://evil.example/") == "/"
        assert auth.safe_next("/\\evil.example") == "/"
        assert auth.safe_next("/login?next=/") == "/"  # never back to the login page itself


class TestSessions:
    def test_a_token_lives_for_the_configured_time_then_expires(self):
        now = {"t": 1_000_000.0}
        sessions = auth.Sessions(b"secret", "pw", session_hours=6, remember_days=730, clock=lambda: now["t"])
        token, lifetime = sessions.issue(remember=False)
        assert lifetime == 6 * 3600 and sessions.verify(token) == "session"
        now["t"] += 6 * 3600 - 1
        assert sessions.verify(token) == "session"
        now["t"] += 2
        assert sessions.verify(token) == ""

        remembered, lifetime = sessions.issue(remember=True)
        assert lifetime == 730 * 86400 and sessions.verify(remembered) == "remembered"
        now["t"] += 700 * 86400
        assert sessions.verify(remembered) == "remembered"

    def test_a_tampered_or_foreign_token_is_refused(self):
        sessions = auth.Sessions(b"secret", "pw", clock=lambda: 1_000_000.0)
        token, _ = sessions.issue(remember=True)
        kind, expires, signature = token.split(".")
        assert sessions.verify(f"{kind}.{int(expires) + 86400}.{signature}") == ""  # a longer life
        assert sessions.verify(f"s.{expires}.{signature}") == ""  # another kind
        assert sessions.verify(token[:-1] + ("0" if token[-1] != "0" else "1")) == ""
        assert sessions.verify("") == "" and sessions.verify(None) == "" and sessions.verify("junk") == ""
        assert sessions.verify(f"{kind}.{'9' * 5000}.{signature}") == ""  # past int()'s digit limit: not ours (2026-09-22)
        assert auth.Sessions(b"other", "pw", clock=lambda: 1_000_000.0).verify(token) == ""

    def test_changing_the_password_signs_every_token_out(self):
        clock = lambda: 1_000_000.0  # noqa: E731
        token, _ = auth.Sessions(b"secret", "pw", clock=clock).issue(remember=True)
        assert auth.Sessions(b"secret", "pw", clock=clock).verify(token) == "remembered"
        assert auth.Sessions(b"secret", "new pw", clock=clock).verify(token) == ""


class TestLoginLimiter:
    def test_locks_after_the_attempts_and_unlocks_after_the_lockout(self):
        now = {"t": 0.0}
        limiter = auth.LoginLimiter(max_attempts=3, lockout_minutes=15, clock=lambda: now["t"])
        assert limiter.failed("a") == 2 and limiter.failed("a") == 1
        assert limiter.retry_after("a") == 0
        assert limiter.failed("a") == 0
        assert limiter.retry_after("a") == 15 * 60
        assert limiter.failed("a") == 0  # a try while locked counts for nothing
        assert limiter.retry_after("b") == 0  # another address is untouched
        now["t"] += 15 * 60 - 1
        assert limiter.retry_after("a") > 0
        now["t"] += 2
        assert limiter.retry_after("a") == 0
        assert limiter.failed("a") == 2  # the count started over

    def test_a_right_password_and_a_stale_failure_reset_the_count(self):
        now = {"t": 0.0}
        limiter = auth.LoginLimiter(max_attempts=3, lockout_minutes=15, clock=lambda: now["t"])
        limiter.failed("a")
        limiter.failed("a")
        limiter.succeeded("a")
        assert limiter.failed("a") == 2
        now["t"] += 16 * 60  # older than the window: forgotten
        assert limiter.failed("a") == 2


class TestSessionSecret:
    def test_made_once_and_kept_in_the_state_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(loader, "STATE_FILE", tmp_path / ".state.json")
        first = auth.session_secret()
        assert len(first) >= 64
        assert auth.session_secret() == first
        state = json.loads((tmp_path / ".state.json").read_text(encoding="utf-8"))
        assert state["web"]["session_secret"] == first.decode("ascii")

    def test_keeps_what_the_state_file_already_holds(self, tmp_path, monkeypatch):
        monkeypatch.setattr(loader, "STATE_FILE", tmp_path / ".state.json")
        (tmp_path / ".state.json").write_text(json.dumps({"costco": {"p": {"token": "t"}},
                                                          "web": {"session_secret": "x" * 64}}), encoding="utf-8")
        assert auth.session_secret() == b"x" * 64
        assert json.loads((tmp_path / ".state.json").read_text(encoding="utf-8"))["costco"] == {"p": {"token": "t"}}


# --------------------------------------------------------------------------------------------------
# The dashboard
# --------------------------------------------------------------------------------------------------


class TestNoPassword:
    def test_blank_password_means_the_dashboard_is_open_as_before(self, tmp_path, clock):
        client = _app(tmp_path, clock, web_password="")
        assert client.get("/", follow_redirects=False).status_code == 200
        assert "Sign out" not in client.get("/").text
        assert client.get("/login", follow_redirects=False).headers["location"] == "/"
        assert client.get("/health").json()["password_protected"] is False


class TestTheGate:
    def test_every_page_but_the_open_ones_goes_to_the_login_page(self, client):
        response = client.get("/orders?page=2", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login?next=%2Forders%3Fpage%3D2"
        assert client.get("/", follow_redirects=False).headers["location"] == "/login?next=%2F"
        assert client.get("/settings", follow_redirects=False).status_code == 303
        assert client.get("/receipts/x.pdf", follow_redirects=False).status_code == 303  # PII: gated
        assert client.get("/static/style.css").status_code == 200
        health = client.get("/health")
        assert health.status_code == 200 and health.json()["password_protected"] is True
        login = client.get("/login")
        assert login.status_code == 200
        assert "Remember me" in login.text and "2 years" not in login.text  # terse
        assert "<nav>" not in login.text  # no header for a visitor who is not signed in

    def test_an_htmx_fragment_request_sends_the_window_to_the_login_page(self, client):
        response = client.get("/orders?page=2", headers={"HX-Request": "true",
                                                         "HX-Current-URL": "http://testserver/orders?view=cards"})
        assert response.status_code == 401
        assert response.headers["HX-Redirect"] == "/login?next=%2Forders%3Fview%3Dcards"


class TestSigningIn:
    def test_the_right_password_signs_in_for_six_hours(self, client, clock):
        response = _sign_in(client, nxt="/orders?page=2")
        assert response.status_code == 303 and response.headers["location"] == "/orders?page=2"
        assert _max_age(response) == 6 * 3600
        assert "HttpOnly" in response.headers["set-cookie"]
        page = client.get("/", follow_redirects=False)
        assert page.status_code == 200 and "Sign out" in page.text
        assert client.get("/login", follow_redirects=False).headers["location"] == "/"  # already in
        clock["now"] += timedelta(hours=6, seconds=1)
        assert client.get("/", follow_redirects=False).status_code == 303  # the session ended
        events = activity_module.read(client.logs_dir / "activity.jsonl")
        assert [e["kind"] for e in events] == ["signin"]
        assert events[0]["summary"] == "Dashboard sign-in from testclient"

    def test_remember_me_lasts_two_years(self, client, clock):
        response = _sign_in(client, remember=True)
        assert _max_age(response) == 730 * 86400
        clock["now"] += timedelta(days=700)
        assert client.get("/", follow_redirects=False).status_code == 200
        events = activity_module.read(client.logs_dir / "activity.jsonl")
        assert events[0]["summary"] == "Dashboard sign-in from testclient, remembered for 2 years"

    def test_the_lengths_come_from_the_settings(self, tmp_path, clock):
        client = _app(tmp_path, clock, web_session_hours=1, web_remember_days=30)
        assert _max_age(_sign_in(client)) == 3600
        assert _max_age(_sign_in(client, remember=True)) == 30 * 86400

    def test_next_never_leaves_the_dashboard(self, client):
        assert _sign_in(client, nxt="//evil.example/").headers["location"] == "/"
        assert _sign_in(client, nxt="http://evil.example/").headers["location"] == "/"

    def test_sign_out_ends_the_session(self, client):
        _sign_in(client, remember=True)
        assert client.get("/", follow_redirects=False).status_code == 200
        response = client.post("/logout", follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/login"
        assert client.get("/", follow_redirects=False).status_code == 303

    def test_changing_the_password_signs_every_device_out(self, tmp_path, clock):
        client = _app(tmp_path, clock)
        _sign_in(client, remember=True)
        cookie = client.cookies.get(auth.COOKIE)
        assert cookie
        changed = _app(tmp_path, clock, web_password="another one")
        changed.cookies.set(auth.COOKIE, cookie)
        assert changed.get("/", follow_redirects=False).status_code == 303
        same = _app(tmp_path, clock)  # a restart with the same password and secret keeps it
        same.cookies.set(auth.COOKIE, cookie)
        assert same.get("/", follow_redirects=False).status_code == 200


class TestTheRateLimit:
    def test_wrong_passwords_lock_the_address_and_record_an_alert(self, client, clock):
        first = _sign_in(client, password="nope")
        assert first.status_code == 401
        assert "Wrong password. 2 attempts left before this address is locked for 15 minutes." in first.text
        assert "1 attempt left" in _sign_in(client, password="nope").text
        locked = _sign_in(client, password="nope")
        assert locked.status_code == 429 and locked.headers["Retry-After"] == str(15 * 60)
        assert "This address is locked for 15 minutes." in locked.text
        assert 'name="password" autocomplete="current-password" autofocus required disabled' in locked.text
        # the right password is refused while the lock lasts, and the page says so
        assert _sign_in(client).status_code == 429
        page = client.get("/login")
        assert page.status_code == 429 and "Try again in 15 minutes." in page.text
        assert client.get("/", follow_redirects=False).status_code == 303
        events = activity_module.read(client.logs_dir / "activity.jsonl")
        assert [e["kind"] for e in events] == ["alert"]
        assert events[0]["summary"] == ("Dashboard sign-in locked for testclient: 3 wrong passwords in a row "
                                        "(locked for 15 minutes)")
        assert events[0]["details"] == {"address": "testclient", "attempts": 3, "lockout_minutes": 15}
        # once the lock ends and the owner signs in, the overview's Alerts card and the Activity
        # page show it
        clock["now"] += timedelta(minutes=15, seconds=1)
        assert _sign_in(client).status_code == 303
        overview = client.get("/").text
        assert '<div class="label">Alerts</div>' in overview and "unacknowledged, last 7 days" in overview
        assert "Dashboard sign-in locked for testclient" in client.get("/activity?type=alert&days=7&unacked=1").text

    def test_a_right_password_resets_the_count(self, client):
        _sign_in(client, password="nope")
        _sign_in(client, password="nope")
        assert _sign_in(client).status_code == 303
        client.post("/logout")
        assert "2 attempts left" in _sign_in(client, password="nope").text

    def test_the_limit_comes_from_the_settings_and_zero_never_locks(self, tmp_path, clock):
        client = _app(tmp_path, clock, web_login_attempts=2, web_login_lockout_minutes=0)
        for _ in range(5):
            response = _sign_in(client, password="nope")
            assert response.status_code == 401 and "Wrong password." in response.text
            assert "locked" not in response.text
        assert _sign_in(client).status_code == 303
