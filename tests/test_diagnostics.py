"""The failure dossier — what replaces the paid agent when a deterministic path breaks.

Offline. Exercises the dossier itself (files, report, redaction, selector audit, pruning), the
CdpBrowser hook that captures the page on the way out of a failed `with` block, the soft-problem path
(a scrape that succeeded but could not read a tracking page), and the tripwire that keeps each
parser's selector literals declared for the audit.
"""

import re
from pathlib import Path

import pytest

import diagnostics
import diagnostics.dossier as dossier_mod
from diagnostics.dossier import FailureDossier, audit_selectors, redact

ROOT = Path(__file__).resolve().parents[1]


class _FakePage:
    def __init__(self, html="<html><body><div class='order-card'>Order 111-2</div></body></html>",
                 url="https://www.amazon.com/your-orders/orders", fail_screenshot=False):
        self.url = url
        self._html = html
        self._fail_screenshot = fail_screenshot

    def title(self):
        return "Your Orders"

    def content(self):
        return self._html

    def screenshot(self, path, **kwargs):
        if self._fail_screenshot:
            raise RuntimeError("no screenshot for you")
        Path(path).write_bytes(b"\x89PNG fake")


class TestRedaction:
    def test_secrets_and_pii_patterns_are_masked(self):
        text = ("pw=hunter2secret user=someone@example.com call (555) 123-4567 "
                "card ending in 1234 XXXX-XXXX-XXXX-9876 order 113-9990002-0000000")
        out = redact(text, {"hunter2secret"})
        assert "hunter2secret" not in out and "[REDACTED]" in out
        assert "someone@example.com" not in out and "[email]" in out
        assert "123-4567" not in out and "[phone]" in out
        assert "ending in ####" in out and "ending in 1234" not in out
        assert "9876" not in out
        # Order ids are kept on purpose: they are what a fix gets tested against.
        assert "113-9990002-0000000" in out

    def test_short_or_empty_secrets_are_ignored(self):
        assert redact("abc abc", {"abc", ""}) == "abc abc"


class TestSelectorAudit:
    def test_counts_css_and_text_markers_and_survives_a_bad_selector(self):
        html = "<div class='order-card'>A</div><div class='order-card'>B</div><script>self.__next_f.push([1,\"x\"])</script>"
        rows = audit_selectors(html, {
            "cards": ".order-card",
            "missing": "#nope",
            "flight": "text:self.__next_f.push([1,",
            "broken": "div[",
        })
        by = {r["name"]: r for r in rows}
        assert by["cards"]["count"] == 2 and by["cards"]["sample"] == "A"
        assert by["missing"]["count"] == 0
        assert by["flight"]["count"] == 1
        assert "error" in by["broken"]


class TestFailureDossier:
    def test_a_snapshot_writes_html_and_png_and_the_report_audits_selectors(self, tmp_path):
        d = FailureDossier("amazon", "bravo", selectors={"card": ".order-card", "gone": "#gone"},
                           secrets={"s3cretpw"}, root=tmp_path)
        d.note("amazon", "loading order history")
        d.snapshot(_FakePage(html="<div class='order-card'>x s3cretpw</div>"), "at failure")
        try:
            raise RuntimeError("boom s3cretpw")
        except RuntimeError as exc:
            path = d.write(exc)

        assert path.parent == tmp_path
        assert (path / "page_1.html").exists() and (path / "page_1.png").exists()
        assert "s3cretpw" not in (path / "page_1.html").read_text(encoding="utf-8")
        report = (path / "report.md").read_text(encoding="utf-8")
        assert "s3cretpw" not in report and "[REDACTED]" in report
        assert "RuntimeError" in report and "Traceback" in report
        assert "loading order history" in report
        assert "1 of 2 declared selectors matched NOTHING" in report and "gone" in report
        assert "| card | `.order-card` | 1 |" in report
        assert "Do not commit it" in report

    def test_a_failed_screenshot_does_not_lose_the_html(self, tmp_path):
        d = FailureDossier("amazon", "bravo", root=tmp_path)
        d.snapshot(_FakePage(fail_screenshot=True), "at failure")
        path = d.write(RuntimeError("x"))
        assert (path / "page_1.html").exists()
        assert not (path / "page_1.png").exists()
        assert "screenshot: no screenshot for you" in (path / "report.md").read_text(encoding="utf-8")

    def test_api_responses_are_saved_and_redacted(self, tmp_path):
        d = FailureDossier("costco", "alpha", secrets={"tok-abcdef"}, root=tmp_path)
        d.record_response("GraphQL getOnlineOrders", 500, {"errors": ["bad tok-abcdef"]},
                          request={"variables": {"warehouseNumber": "847"}})
        path = d.write(RuntimeError("GraphQL HTTP 500"))
        body = (path / "response_1.txt").read_text(encoding="utf-8")
        assert "tok-abcdef" not in body and "[REDACTED]" in body
        report = (path / "report.md").read_text(encoding="utf-8")
        assert "GraphQL getOnlineOrders — status 500" in report
        assert '"warehouseNumber": "847"' in report

    def test_old_dossiers_are_pruned(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dossier_mod, "KEEP_DOSSIERS", 2)
        for i in range(3):
            (tmp_path / f"amazon_p_2026010{i}T000000Z").mkdir()
            (tmp_path / f"amazon_p_2026010{i}T000000Z" / "report.md").write_text("old")
        FailureDossier("amazon", "p", root=tmp_path).write(RuntimeError("x"))
        remaining = sorted(p.name for p in tmp_path.iterdir())
        assert len(remaining) == 2
        assert "amazon_p_20260100T000000Z" not in remaining
        assert "amazon_p_20260101T000000Z" not in remaining

    def test_nothing_is_written_for_a_clean_run(self, tmp_path):
        with diagnostics.collecting("amazon", "bravo", root=tmp_path) as d:
            d.note("amazon", "fine")
        assert not any(tmp_path.iterdir())


class TestModuleLevelApi:
    def test_calls_outside_a_dossier_are_no_ops(self):
        assert diagnostics.current() is None
        diagnostics.note("x", "y")
        diagnostics.problem("z")
        diagnostics.snapshot(_FakePage(), "l")
        diagnostics.record_response("l", 500, "body")
        diagnostics.add_secrets("abc")

    def test_calls_inside_a_dossier_reach_it_and_a_nested_open_reuses_it(self, tmp_path):
        with diagnostics.collecting("amazon", "bravo", root=tmp_path) as outer:
            with diagnostics.collecting("amazon", "bravo", root=tmp_path) as inner:
                assert inner is outer
                diagnostics.note("s", "m")
                diagnostics.problem("p")
                diagnostics.add_secrets("secretvalue")
            assert outer.notes[0][1:] == ("s", "m")
            assert outer.problems == ["p"]
            assert "secretvalue" in outer.secrets
        assert diagnostics.current() is None


class TestCdpBrowserCapturesOnFailure:
    """Every browser-driven path exits through CdpBrowser.__exit__; that is where the page is saved."""

    def _browser(self, monkeypatch):
        from scrapers import cdp
        from models.profile import ProfileConfig

        class _Client:
            _http = type("H", (), {"request": staticmethod(lambda *a, **k: {"id": "b", "cdpUrl": "ws://x"})})()

            def close(self):
                pass

        b = cdp.CdpBrowser(ProfileConfig(label="p", profile_id="x"))
        b._client = _Client()
        b.page = _FakePage()
        return b

    def test_an_exception_inside_the_with_block_snapshots_the_page(self, monkeypatch, tmp_path):
        b = self._browser(monkeypatch)
        with diagnostics.collecting("amazon", "p", root=tmp_path) as d:
            b.__exit__(RuntimeError, RuntimeError("shape"), None)
        assert len(d.snapshots) == 1
        assert d.snapshots[0]["label"] == "at failure (RuntimeError)"
        assert d.snapshots[0]["url"] == "https://www.amazon.com/your-orders/orders"

    def test_a_clean_exit_snapshots_nothing(self, monkeypatch, tmp_path):
        b = self._browser(monkeypatch)
        with diagnostics.collecting("amazon", "p", root=tmp_path) as d:
            b.__exit__(None, None, None)
        assert d.snapshots == []

    def test_a_keyboard_interrupt_snapshots_nothing(self, monkeypatch, tmp_path):
        b = self._browser(monkeypatch)
        with diagnostics.collecting("amazon", "p", root=tmp_path) as d:
            b.__exit__(KeyboardInterrupt, KeyboardInterrupt(), None)
        assert d.snapshots == []


class TestSoftProblemsStillProduceADossier:
    """A scrape that SUCCEEDS but could not read a tracking page must not look healthy."""

    def test_an_unreadable_tracking_page_is_reported_after_a_successful_scrape(self, monkeypatch, tmp_path):
        from models.profile import ProfileConfig
        import scrapers.base as base
        from scrapers.amazon import AmazonScraper

        monkeypatch.setattr(dossier_mod, "FAILURES_DIR", tmp_path)
        scraper = AmazonScraper(ProfileConfig(label="p", profile_id="x", retailers=["amazon"]),
                                lookback_days=1)

        def _api():
            diagnostics.problem("order 111 / shipment 1: the package-tracking page could not be read")
            return ["ROW"]

        monkeypatch.setattr(scraper, "_scrape_via_api", _api)
        alerts = []
        monkeypatch.setattr(base, "alert", lambda subject, body: alerts.append((subject, body)))

        assert scraper.scrape() == ["ROW"], "the rows are still returned"
        assert alerts and "1 problem(s)" in alerts[0][0]
        assert "package-tracking page could not be read" in alerts[0][1]
        assert "Failure dossier:" in alerts[0][1]
        report = next(tmp_path.glob("*/report.md")).read_text(encoding="utf-8")
        assert "Non-fatal problems" in report

    def test_amazon_api_flags_an_unreadable_tracking_page(self, monkeypatch, tmp_path):
        """The reader returning None used to route the order to the agent; now it is a problem."""
        from scrapers.amazon_api import AmazonApiClient
        from models.profile import ProfileConfig
        import scrapers.amazon_api as amazon_api

        monkeypatch.setattr(amazon_api, "parse_shipment_targets", lambda html: [
            {"shipment": "1", "status": "ordered", "tracking_url": "https://www.amazon.com/pt"}])
        page = _FakePage(url="https://www.amazon.com/pt")
        page.goto = lambda url, **k: None
        page.wait_for_timeout = lambda ms: None
        client = AmazonApiClient(ProfileConfig(label="p", profile_id="x"), tracking_reader=lambda p: None)
        with diagnostics.collecting("amazon", "p", root=tmp_path) as d:
            assert client._read_tracking_numbers(page, {"111": "<html></html>"}) == {}
        assert d.problems and "111 / shipment 1" in d.problems[0]
        assert d.snapshots and "tracking page unreadable" in d.snapshots[0]["label"]


class TestSelectorLiteralsAreDeclared:
    """Every CSS literal a parser calls `select(...)` with must be in its SELECTORS, or the audit
    would silently stop covering it the day someone adds a new one."""

    @pytest.mark.parametrize("module_name", ["amazon_mapping", "amazon_business_mapping"])
    def test_mapping_literals_are_all_declared(self, module_name):
        import importlib

        source = (ROOT / "scrapers" / f"{module_name}.py").read_text(encoding="utf-8")
        literals = set(re.findall(r'select(?:_one)?\(\s*"([^"]+)"', source))
        assert literals, "no select() literals found — the scan has rotted"
        declared = set(importlib.import_module(f"scrapers.{module_name}").SELECTORS.values())
        assert literals <= declared, f"undeclared selectors: {literals - declared}"

    def test_every_retailer_scraper_declares_its_selectors(self):
        from scrapers.amazon import AmazonScraper
        from scrapers.amazon_business import AmazonBusinessScraper
        from scrapers.bestbuy import BestBuyScraper
        from scrapers.costco import CostcoScraper

        assert AmazonScraper.diagnostic_selectors["pt_tracking_number"] == ".pt-delivery-card-trackingId"
        assert "history_order_link" in AmazonBusinessScraper.diagnostic_selectors
        assert BestBuyScraper.diagnostic_selectors["flight_orders_key"].startswith("text:")
        # Costco's data path has no browser; the token grab's sign-in screen is the one page it
        # can capture, so those are the selectors it declares.
        assert CostcoScraper.diagnostic_selectors["signin_email"] == "#signInName"


class TestSettingsAndDocs:
    def test_the_agent_fallback_is_off_by_default(self, monkeypatch):
        monkeypatch.delenv("AGENT_FALLBACK_ENABLED", raising=False)
        from config.settings import _get_bool

        assert _get_bool("AGENT_FALLBACK_ENABLED", False) is False

    def test_main_handles_the_new_error_quietly(self, monkeypatch):
        import main
        from models.profile import ProfileConfig
        from scrapers.base import DeterministicPathError
        from scrapers.amazon import AmazonScraper

        scraper = AmazonScraper(ProfileConfig(label="p", profile_id="x"), lookback_days=1)
        monkeypatch.setattr(scraper, "scrape",
                            lambda: (_ for _ in ()).throw(DeterministicPathError("Amazon:p")))
        alerts = []
        monkeypatch.setattr(main, "alert", lambda subject, body: alerts.append(subject))
        main.run_scrape(scraper)
        assert alerts == [], "the scraper already alerted with the dossier path; main must not repeat it"


class TestSnapshotFromHtml:
    """The Amazon parsers run after the browser has closed; a shape failure there attaches the document."""

    def test_html_is_written_and_audited_without_a_page(self, tmp_path):
        html = "<html><head><title>Your Order</title></head><body><div data-component='x'>t</div></body></html>"
        with diagnostics.collecting("amazon", "p", root=tmp_path, selectors={"item_title": "[data-component='itemTitle']", "x": "[data-component='x']"}) as d:
            diagnostics.snapshot_html(html, "order-details for 111-1: no item titles", url="https://www.amazon.com/gp/css/order-details?orderID=111-1")
        snap = d.snapshots[0]
        assert snap["html_file"] == "page_1.html" and snap["png_file"] == ""
        assert snap["title"] == "Your Order"
        assert {r["name"]: r["count"] for r in snap["audit"]} == {"item_title": 0, "x": 1}
        report = d.write(RuntimeError("x"))
        assert "item_title" in (report / "report.md").read_text(encoding="utf-8")

    def test_outside_a_dossier_it_is_a_no_op(self):
        diagnostics.snapshot_html("<html></html>", "l")
