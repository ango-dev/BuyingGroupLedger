"""The activity log (diagnostics/activity.py), the places that record into it, and the dashboard's
Activity page."""

from __future__ import annotations

import csv
import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from diagnostics import activity
from models.order import FIELDNAMES
from ledger.sync import HEADER

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


class TestTheLog:
    def test_record_appends_json_lines_and_read_is_newest_first(self, tmp_path):
        path = tmp_path / "a.jsonl"
        first = activity.record("alert", "one", {"message": "m"}, path=path,
                                at=datetime(2026, 9, 1, tzinfo=timezone.utc))
        activity.record("edit", "two", path=path, at=datetime(2026, 9, 2, tzinfo=timezone.utc))
        assert first["run_id"] is None and first["details"] == {"message": "m"}
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2 and json.loads(lines[0])["summary"] == "one"
        assert [e["summary"] for e in activity.read(path)] == ["two", "one"]

    def test_a_bad_line_is_skipped_and_a_missing_file_is_empty(self, tmp_path):
        path = tmp_path / "a.jsonl"
        assert activity.read(path) == []
        path.write_text('{"at": "2026-09-01T00:00:00+00:00", "kind": "alert", "summary": "ok"}\n'
                        "not json\n{\"no\": \"kind\"}\n", encoding="utf-8")
        assert [e["summary"] for e in activity.read(path)] == ["ok"]

    def test_record_never_raises(self, tmp_path):
        blocked = tmp_path / "dir"
        blocked.mkdir()
        assert activity.record("alert", "x", path=blocked) is None  # a directory, not a file

    def test_run_ids_group_a_runs_events(self, tmp_path):
        path = tmp_path / "a.jsonl"
        activity.ACTIVITY_FILE = path
        run_id = activity.begin_run("costco")
        assert run_id.startswith("run-") and activity.current_run_id() == run_id
        activity.record("scrape", "during")
        activity.end_run()
        assert activity.current_run_id() is None
        activity.record("edit", "after")
        events = activity.read(path)
        assert [(e["kind"], e["run_id"] == run_id) for e in events] == [
            ("edit", False), ("run", True), ("scrape", True), ("run", True)]
        assert activity.run_label("run-20260918T140501Z") == "09-18 14:05"
        assert activity.run_started_at("run-20260918T140501Z") == "2026-09-18T14:05:01+00:00"
        assert activity.run_started_at(None) == ""

    def test_filters(self, tmp_path):
        path = tmp_path / "a.jsonl"
        activity.record("alert", "Costco: scrape failed", {"message": "boom"}, path=path,
                        at=datetime(2026, 9, 1, tzinfo=timezone.utc), run_id="run-A")
        activity.record("edit", "Status on 111-1 changed", {"order_id": "111-1"}, path=path,
                        at=datetime(2026, 9, 17, tzinfo=timezone.utc))
        activity.record("ledger", "Costco [p]: 2 rows scraped", path=path,
                        at=datetime(2026, 9, 18, tzinfo=timezone.utc), run_id="run-B")
        events = activity.read(path)
        assert [e["kind"] for e in activity.filter_events(events, kinds=("alert", "edit"))] == ["edit", "alert"]
        assert [e["kind"] for e in activity.filter_events(events, q="costco")] == ["ledger", "alert"]
        assert [e["kind"] for e in activity.filter_events(events, q="111-1")] == ["edit"]
        assert [e["kind"] for e in activity.filter_events(events, days=7, now=NOW)] == ["ledger", "edit"]
        assert [e["kind"] for e in activity.filter_events(events, run_id="run-A")] == ["alert"]
        assert activity.counts_by_kind(events)["alert"] == 1 and activity.counts_by_kind(events)["run"] == 0

    def test_details_are_trimmed_to_stay_small(self, tmp_path):
        event = activity.record("edit", "x", {"long": "y" * 10_000, "nested": {"k": object()}},
                                path=tmp_path / "a.jsonl")
        assert len(event["details"]["long"]) < 5_000 and event["details"]["long"].endswith(" …")
        assert isinstance(event["details"]["nested"]["k"], str)


class TestRecordedAtTheSource:
    def test_an_alert_records_itself(self):
        from alerts.notifier import alert

        alert("Costco: scrape failed", "the body")
        events = activity.read()
        assert events and events[0]["kind"] == "alert"
        assert events[0]["summary"] == "Costco: scrape failed"
        assert events[0]["details"]["message"] == "the body"

    def test_a_dossier_records_itself(self, tmp_path):
        from diagnostics.dossier import FailureDossier

        directory = FailureDossier("costco", "profile-1", root=tmp_path / "f").write(RuntimeError("selector gone"))
        events = activity.read()
        assert events[0]["kind"] == "dossier"
        assert events[0]["summary"].startswith("costco [profile-1]: failure dossier written")
        assert events[0]["details"]["name"] == directory.name
        assert events[0]["details"]["error"] == "RuntimeError: selector gone"

    def test_a_run_records_its_scrapes_ledger_writes_and_sync(self, monkeypatch, tmp_path):
        import main as main_module

        class Item:
            def __init__(self, order_id):
                self.order_id = order_id

        class Profile:
            label = "profile-1"
            profile_id = "BU-1"

        class Scraper:
            retailer_name = "Costco"
            retailer_key = "costco"
            profile = Profile()

            def scrape(self):
                return [Item("A1"), Item("A2")]

        monkeypatch.setattr(main_module, "_classify_and_drop_personal", lambda items, label: items)
        monkeypatch.setattr(main_module, "_tag_cards", lambda items, label: None)
        monkeypatch.setattr(main_module, "_capture_receipts", lambda items, scraper, label: None)
        monkeypatch.setattr(main_module, "write_csv", lambda items: tmp_path / "orders.csv")
        monkeypatch.setattr(main_module, "sync_csv_to_ledger",
                            lambda path: {"updated": 1, "appended": 1, "split_rows": 0,
                                          "suspect_tracking": [], "skipped_blank": 0,
                                          "skipped_conflicts": 0})
        monkeypatch.setattr(main_module, "sort_ledger_by_date_desc", lambda: {"sorted_rows": 2})
        main_module.run_scrape(Scraper())
        events = activity.read()
        assert events[0]["kind"] == "ledger"
        assert events[0]["summary"] == "Costco [profile-1]: 2 row(s) scraped -- 1 updated, 1 added"
        assert events[0]["details"]["order_ids"] == ["A1", "A2"] and events[0]["details"]["sorted"] is True

        # the buying-group sync's outcome, per group
        from buying_groups.base import SubmissionResult

        push = SubmissionResult(submitted=["1Z1", "1Z2"], failed=[("O9", "1Z9")])
        result = {"outcomes": {"bfmr": {"push": push, "insurance": SubmissionResult(submitted=["1Z1"]),
                                        "payouts": [object(), object(), object()]}},
                  "writes": {5: {}, 6: {}}}
        main_module._record_sync(result)
        events = activity.read()
        assert events[0]["kind"] == "sync"
        assert events[0]["summary"] == ("Buying-group sync: 2 package(s) submitted, 1 insured, 3 payout "
                                        "record(s) read, 2 ledger row(s) updated, 1 submission(s) failed")
        assert events[0]["details"]["groups"]["bfmr"]["tracking_submitted"] == ["1Z1", "1Z2"]
        assert events[0]["details"]["rows_written"] == [5, 6]
        main_module._record_sync({})
        assert activity.read()[0]["summary"] == "Buying-group sync: nothing eligible"

    def test_main_opens_and_closes_a_run(self, monkeypatch):
        import main as main_module

        for step in ("run_buying_group_sync", "run_bfmr_email_autoreply"):
            monkeypatch.setattr(main_module, step, lambda: None)
        main_module.main([])
        events = activity.read()
        assert [e["kind"] for e in events] == ["run", "run"]
        assert events[1]["summary"] == "Run started" and events[0]["summary"] == "Run finished"
        assert events[0]["run_id"] == events[1]["run_id"] and events[0]["run_id"].startswith("run-")
        assert activity.current_run_id() is None


class TestThePage:
    @pytest.fixture
    def client(self, tmp_path):
        from fastapi.testclient import TestClient

        from config.settings import Settings
        from web.app import create_app
        from web.ledger_reader import SnapshotReader

        logs = tmp_path / "logs"
        (logs / "failures").mkdir(parents=True)
        snapshot = tmp_path / "ledger_backup_20260918T000000Z.csv"
        with snapshot.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(HEADER)
            row = {"order_id": "111-1", "order_date": "2026-09-01", "item_name": "Thing",
                   "shipment": "1", "status": "shipped", "retailer": "Costco"}
            writer.writerow([row.get(f, "") for f in FIELDNAMES])
        app = create_app(SnapshotReader(snapshot), logs_dir=logs, failures_dir=logs / "failures",
                         backup_dir=tmp_path / "backups", repo_root_dir=tmp_path,
                         clock=lambda: NOW,
                         settings=dataclasses.replace(Settings(), container_run_interval_hours=6, web_password=""))
        test_client = TestClient(app)
        test_client.activity_path = logs / "activity.jsonl"
        return test_client

    def test_the_page_lists_filters_and_swaps(self, client):
        path = client.activity_path
        activity.record("alert", "Costco: scrape failed", {"message": "boom"}, path=path,
                        at=datetime(2026, 9, 17, 8, 0, tzinfo=timezone.utc), run_id="run-20260917T080000Z")
        activity.record("dossier", "costco [p]: failure dossier written",
                        {"name": "costco_p_20260917T080000Z", "path": "x"}, path=path,
                        at=datetime(2026, 9, 17, 8, 1, tzinfo=timezone.utc), run_id="run-20260917T080000Z")
        activity.record("edit", "Status on 111-1 (shipment 1): 'shipped' → 'delivered'",
                        {"order_id": "111-1"}, path=path,
                        at=datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc))
        activity.record("edit", "old", path=path, at=datetime(2026, 1, 1, tzinfo=timezone.utc))

        body = client.get("/activity").text
        assert "<h1>Activity</h1>" in body and 'id="activity-filters"' in body
        assert body.count("<tr class=\"kind-") == 3  # the January event is outside the default 7 days
        assert "3 event(s) of 4" in body
        assert "costco_p_20260917T080000Z" in body and 'href="/failures' not in body  # the Failures page is gone
        assert 'href="/orders/111-1"' in body  # an order id links to its page
        assert ">Alert<" in body and ">Failure dossier<" in body and ">Dashboard edit<" in body
        assert '<time class="local run" datetime="2026-09-17T08:00:00+00:00">09-17 08:00</time>' in body
        assert '<time class="local" datetime="2026-09-18T09:00:00+00:00"' in body  # rendered in local time by edit.js
        assert body.count("<select") == 0 and 'data-param="days"' in body  # the page's own dropdowns
        assert 'placeholder="any text in an event: order id, retailer, item, subject, tracking number' in body
        assert 'href="/activity"' in body and 'class="gear' in body and 'href="/health"' not in body
        assert 'href="/failures"' not in body
        assert 'class="pill backend"' not in body and "<footer" not in body  # gone (2026-09-18)

        everything = client.get("/activity", params={"days": "0"}).text
        assert everything.count("<tr class=\"kind-") == 4
        only_edits = client.get("/activity", params={"type": "edit", "days": "0"}).text
        assert client.get("/activity", params={"kind": "edit", "days": "0"}).text.count("<tr class=\"kind-") == 2  # an old link
        assert only_edits.count("<tr class=\"kind-") == 2 and ">Alert<" not in only_edits[only_edits.index("<table"):]
        searched = client.get("/activity", params={"q": "boom"}).text
        assert searched.count("<tr class=\"kind-") == 1
        one_run = client.get("/activity", params={"run": "run-20260917T080000Z"}).text
        assert one_run.count("<tr class=\"kind-") == 2 and "run 09-17 08:00" in one_run
        partial = client.get("/activity", headers={"HX-Request": "true"}).text
        assert "<html" not in partial and "<table" in partial
        oldest = client.get("/activity", params={"dir": "asc", "days": "0"}).text
        assert oldest.index("old") < oldest.index("scrape failed")
        # the columns sort from their header arrows, like the other tables
        import re as _re
        by_type = client.get("/activity", params={"sort": "kind", "dir": "asc", "days": "0"}).text
        kinds = _re.findall(r'<tr class="kind-([a-z]+) has-num"', by_type)
        assert kinds == sorted(kinds) and len(kinds) == 4
        assert 'class="col-kind sorted"' in by_type and 'title="sort descending">▲</a>' in by_type
        assert 'href="/activity?days=0&amp;dir=desc&amp;sort=kind"' in by_type or 'href="/activity?days=0&amp;sort=kind&amp;dir=desc"' in by_type
        by_type_desc = client.get("/activity", params={"sort": "kind", "dir": "desc", "days": "0"}).text
        assert _re.findall(r'<tr class="kind-([a-z]+) has-num"', by_type_desc) == sorted(kinds, reverse=True)
        assert 'class="grid compact activity sheetlike stacked"' in by_type

    def test_the_dashboard_records_its_own_changes(self, client):
        client.post("/backup", follow_redirects=False)
        events = activity.read(client.activity_path)
        assert events[0]["kind"] == "backup" and events[0]["summary"].startswith("Backup ledger_backup_")
        assert events[0]["run_id"] is None
        body = client.get("/activity").text
        assert ">Backup<" in body and "dashboard" in body

    def test_a_logged_dossier_shows_its_report_inline_and_an_unlogged_one_is_listed(self, client, tmp_path):
        failures = tmp_path / "logs" / "failures"
        logged = failures / "costco_profile-1_20260917T080000Z"
        logged.mkdir()
        (logged / "report.md").write_text("# Failure dossier\n\n## What failed\n\n**Boom**: x\n", encoding="utf-8")
        (logged / "page_1.html").write_text("<html>", encoding="utf-8")
        activity.record("dossier", "costco [profile-1]: failure dossier written -- Boom",
                        {"name": logged.name, "path": str(logged), "error": "Boom: x"},
                        path=client.activity_path, at=datetime(2026, 9, 17, 8, 0, tzinfo=timezone.utc),
                        run_id="run-20260917T080000Z")
        unlogged = failures / "amazon_profile-2_20260901T000000Z"
        unlogged.mkdir()
        (unlogged / "response_1.txt").write_text("{}", encoding="utf-8")

        body = client.get("/activity", params={"days": "0"}).text
        assert body.count('<tr class="kind-dossier has-num">') == 2  # one logged + one only on disk, not doubled
        assert "<h2>What failed</h2>" in body and "<strong>Boom</strong>" in body  # the report, inline
        assert "<code>page_1.html</code>" in body and ">report<" in body
        assert "amazon [profile-2]: failure dossier" in body and "no report.md" in body
        # the logged one keeps its run; the disk-only one has none; newest first
        assert body.index("costco_profile-1_20260917T080000Z") < body.index("amazon_profile-2_20260901T000000Z")

    def test_hidden_types_are_remembered_in_a_cookie_and_still_pickable(self, client):
        path = client.activity_path
        activity.record("health", "Ledger container healthy again", path=path,
                        at=datetime(2026, 9, 18, 7, 0, tzinfo=timezone.utc))
        activity.record("alert", "Costco: scrape failed", path=path,
                        at=datetime(2026, 9, 18, 8, 0, tzinfo=timezone.utc))
        body = client.get("/activity").text
        assert ">Container health<" in body and body.count("<tr class=\"kind-") == 2
        css = (Path(__file__).resolve().parents[1] / "web" / "static" / "style.css").read_text(encoding="utf-8")
        assert ".tag.kind-health { background: #" in css  # its own colour, not the default white
        assert 'data-param="hide"' in body and '<span class="name">Type</span>' in body and ">Kind<" not in body

        # the form says: hide health
        hidden = client.get("/activity", params={"hide_set": "1", "hide": "health"})
        assert hidden.cookies.get("activity-hide") == "health"
        assert hidden.text.count("<tr class=\"kind-") == 1 and "healthy again" not in hidden.text
        assert "· hidden" in hidden.text
        # the bare page remembers it; picking the type explicitly shows it anyway
        assert client.get("/activity").text.count("<tr class=\"kind-") == 1
        assert "healthy again" in client.get("/activity", params={"type": "health"}).text
        # the form saying "hide nothing" clears the memory
        cleared = client.get("/activity", params={"hide_set": "1"})
        assert not cleared.cookies.get("activity-hide") and cleared.text.count("<tr class=\"kind-") == 2
        assert client.get("/activity").text.count("<tr class=\"kind-") == 2

    def test_the_healthcheck_alert_files_under_health(self, monkeypatch):
        from alerts.notifier import alert

        alert("Ledger container UNHEALTHY -- scheduler is not producing runs", "body", kind="health")
        alert("Costco: scrape failed", "body")
        assert [e["kind"] for e in activity.read()] == ["alert", "health"]
        script = (Path(__file__).resolve().parents[1] / "docker" / "healthcheck.sh").read_text(encoding="utf-8")
        assert script.count("ALERT_KIND=health $NOTIFY") == 2

    def test_reset_lands_on_the_defaults_and_keeps_the_hidden_types(self, client):
        client.get("/activity", params={"hide_set": "1", "hide": "health"})
        assert 'href="/activity?reset=1"' in client.get("/activity", params={"type": "alert"}).text
        reset = client.get("/activity", params={"reset": "1", "type": "alert", "days": "0"}, follow_redirects=False)
        assert reset.status_code == 303 and reset.headers["location"] == "/activity"
        assert client.cookies.get("activity-hide") == "health"

    def test_an_empty_log_renders(self, client):
        body = client.get("/activity").text
        assert "Nothing recorded yet" in body and "0 event(s)" in body


def test_the_default_window_is_the_last_week():
    from web.activity_view import DEFAULT_DAYS, ActivityFilters

    assert DEFAULT_DAYS == 7 and ActivityFilters.from_query({}).days == 7
    assert "days" not in ActivityFilters.from_query({}).as_query()  # the default is not a query
