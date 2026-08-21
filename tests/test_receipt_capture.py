"""attach_receipts: what it stores, what it links, and — most importantly — when it opens nothing.

The single most valuable assertion in this file is the negative one: on a run where every order
already has a stored receipt, NO cloud browser is created. That is what makes it safe to leave this
step switched on for every scheduled run, since the steady state is re-checking open orders.
"""

import pytest

from models.order import OrderItem
from models.profile import ProfileConfig
from receipts import capture, sources, store


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(store, "is_configured", lambda: True)
    monkeypatch.setattr(store, "link_for", lambda key: f"https://par/{key}")


def _items(*specs, status="delivered"):
    """specs: (order_id, order_date, item_name).

    Defaults to `delivered` because a receipt is only taken once an order is FINISHED — capturing an
    open order would freeze a document that predates its own final totals (see is_capturable). Pass
    status= to exercise the gate itself.
    """
    return [
        OrderItem(retailer="Amazon", profile_label="p", order_id=oid, order_date=date,
                  item_name=name, quantity=1, cost_per_item=10.0, status=status)
        for oid, date, name in specs
    ]


def _profile():
    return ProfileConfig(label="p", profile_id="pid", retailers=["amazon"])


class FakePage:
    def __init__(self, *, pdf=b"%PDF-1.4 receipt", pdf_error=None, url=None, selector_error=None):
        self._pdf = pdf
        self._pdf_error = pdf_error
        self._url_override = url
        self._selector_error = selector_error
        self.visited = []
        self.screenshots = 0
        self.context = self

    # --- playwright surface -------------------------------------------------------------------
    def goto(self, url, **kw):
        self.visited.append(url)

    @property
    def url(self):
        return self._url_override or (self.visited[-1] if self.visited else "")

    def wait_for_selector(self, selector, timeout=None):
        if self._selector_error:
            raise self._selector_error

    def wait_for_timeout(self, ms):
        pass

    def screenshot(self, full_page=False):
        self.screenshots += 1
        return b"\x89PNG fallback"

    # --- cdp surface --------------------------------------------------------------------------
    def new_cdp_session(self, page):
        outer = self

        class Session:
            def send(self, method, params=None):
                if outer._pdf_error:
                    raise outer._pdf_error
                import base64
                return {"data": base64.b64encode(outer._pdf).decode()}

            def detach(self):
                pass

        return Session()


class BrowserFactory:
    """Stands in for CdpBrowser and, crucially, counts how many times it was ever called."""

    def __init__(self, page=None):
        self.page = page or FakePage()
        self.calls = 0

    def __call__(self, profile):
        self.calls += 1
        factory = self

        class Ctx:
            def __enter__(self):
                return factory.page

            def __exit__(self, *exc):
                return False

        return Ctx()


class Recorder:
    """A fake store: which keys already exist, and what got uploaded."""

    def __init__(self, present=(), put_error=None, exists_error=None):
        self.present = set(present)
        self.put_error = put_error
        self.exists_error = exists_error
        self.puts = []

    def exists(self, key):
        if self.exists_error:
            raise self.exists_error
        return key in self.present

    def put(self, key, body, ext):
        if self.put_error:
            raise self.put_error
        self.puts.append((key, body, ext))
        return f"https://par/{key}"


@pytest.fixture
def wired(monkeypatch):
    def _wire(recorder):
        monkeypatch.setattr(store, "exists", recorder.exists)
        monkeypatch.setattr(store, "put", recorder.put)
        return recorder

    return _wire


class TestNoBrowserWhenEverythingIsStored:
    """The cost property the whole design rests on."""

    def test_an_already_stored_receipt_opens_no_browser(self, wired):
        wired(Recorder(present={"receipts/amazon/2026-08/A1.pdf"}))
        factory = BrowserFactory()
        items = _items(("A1", "2026-08-15", "Thing"))

        capture.attach_receipts(items, _profile(), "amazon", browser_factory=factory)

        assert factory.calls == 0, "a re-check run must not create a paid cloud browser"
        assert items[0].receipt_url == "https://par/receipts/amazon/2026-08/A1.pdf"

    def test_a_png_from_an_older_run_also_counts_as_stored(self, wired):
        wired(Recorder(present={"receipts/amazon/2026-08/A1.png"}))
        factory = BrowserFactory()
        items = _items(("A1", "2026-08-15", "Thing"))

        capture.attach_receipts(items, _profile(), "amazon", browser_factory=factory)

        assert factory.calls == 0
        assert items[0].receipt_url.endswith("A1.png")

    def test_a_pdf_wins_over_a_png_for_the_same_order(self, wired):
        wired(Recorder(present={"receipts/amazon/2026-08/A1.pdf", "receipts/amazon/2026-08/A1.png"}))
        items = _items(("A1", "2026-08-15", "Thing"))

        capture.attach_receipts(items, _profile(), "amazon", browser_factory=BrowserFactory())

        assert items[0].receipt_url.endswith(".pdf")

    def test_nothing_happens_when_capture_is_unconfigured(self, monkeypatch):
        monkeypatch.setattr(store, "is_configured", lambda: False)
        factory = BrowserFactory()
        items = _items(("A1", "2026-08-15", "Thing"))

        capture.attach_receipts(items, _profile(), "amazon", browser_factory=factory)

        assert factory.calls == 0
        assert items[0].receipt_url == ""


class TestCapturingANewReceipt:
    def test_a_pdf_is_rendered_stored_and_linked(self, wired):
        recorder = wired(Recorder())
        items = _items(("A1", "2026-08-15", "Thing"))
        factory = BrowserFactory()

        capture.attach_receipts(items, _profile(), "amazon", browser_factory=factory)

        assert factory.calls == 1
        assert recorder.puts == [("receipts/amazon/2026-08/A1.pdf", b"%PDF-1.4 receipt", "pdf")]
        assert items[0].receipt_url == "https://par/receipts/amazon/2026-08/A1.pdf"

    def test_the_print_invoice_page_is_what_gets_opened(self, wired):
        wired(Recorder())
        page = FakePage()
        capture.attach_receipts(_items(("A1", "2026-08-15", "T")), _profile(), "amazon",
                                browser_factory=BrowserFactory(page))

        assert page.visited == [
            "https://www.amazon.com/gp/css/summary/print.html?orderID=A1"
        ]

    def test_printtopdf_refusal_falls_back_to_a_full_page_screenshot(self, wired):
        """Browser-Use cloud browsers are headful, and headful Chrome has historically refused
        Page.printToPDF. The fallback is what makes the feature work either way."""
        recorder = wired(Recorder())
        page = FakePage(pdf_error=RuntimeError("PrintToPDF is not implemented"))

        items = _items(("A1", "2026-08-15", "Thing"))
        capture.attach_receipts(items, _profile(), "amazon", browser_factory=BrowserFactory(page))

        assert page.screenshots == 1
        assert recorder.puts == [("receipts/amazon/2026-08/A1.png", b"\x89PNG fallback", "png")]
        assert items[0].receipt_url.endswith(".png")

    def test_one_browser_covers_every_new_order(self, wired):
        recorder = wired(Recorder())
        factory = BrowserFactory()
        items = _items(("A1", "2026-08-15", "T1"), ("A2", "2026-08-15", "T2"),
                       ("A3", "2026-07-30", "T3"))

        capture.attach_receipts(items, _profile(), "amazon", browser_factory=factory)

        assert factory.calls == 1
        assert [k for k, _, _ in recorder.puts] == [
            "receipts/amazon/2026-08/A1.pdf",
            "receipts/amazon/2026-08/A2.pdf",
            "receipts/amazon/2026-07/A3.pdf",
        ]

    def test_a_missing_ready_selector_does_not_block_the_capture(self, wired):
        """The selector is a heuristic. A layout change should degrade to 'captured, logged loudly',
        not to 'no receipt at all' — the logged-out check is the guard that actually protects us."""
        recorder = wired(Recorder())
        page = FakePage(selector_error=RuntimeError("timeout"))

        capture.attach_receipts(_items(("A1", "2026-08-15", "T")), _profile(), "amazon",
                                browser_factory=BrowserFactory(page))

        assert len(recorder.puts) == 1


class TestARetailerOwnPdfIsDownloadedNotRendered:
    """The Amazon Business case, found live.

    Its print-invoice URL 302s to `/documents/download/<uuid>/order-document.pdf` — Amazon's own
    official invoice, and a better receipt than any render of ours. Rendering it would capture
    Chrome's PDF viewer chrome instead of the document.
    """

    @staticmethod
    def _pdf_page():
        class PdfPage(FakePage):
            REDIRECT = ("https://www.amazon.com/documents/download/910e35d4/order-document.pdf"
                        "?ref=ppx_printOD_rd")

            def __init__(self):
                super().__init__()
                self.downloaded = []
                self.request = self

            def goto(self, url, **kw):
                self.visited.append(self.REDIRECT)  # the 302 Amazon Business performs

            def get(self, url, timeout=None):
                self.downloaded.append(url)
                return type("R", (), {"ok": True, "status": 200,
                                      "body": staticmethod(lambda: b"%PDF real invoice")})()

        return PdfPage()

    def test_the_document_is_downloaded_and_stored_as_a_pdf(self, wired):
        recorder = wired(Recorder())
        page = self._pdf_page()

        capture.attach_receipts(_items(("A1", "2026-08-15", "T")), _profile(), "amazon-business",
                                browser_factory=BrowserFactory(page))

        assert page.downloaded == [page.REDIRECT]
        assert recorder.puts == [
            ("receipts/amazon-business/2026-08/A1.pdf", b"%PDF real invoice", "pdf")
        ]

    def test_printtopdf_is_never_called_on_a_pdf_landing(self, wired):
        wired(Recorder())
        page = self._pdf_page()
        page._pdf_error = AssertionError("must not render a page that is already a PDF")

        capture.attach_receipts(_items(("A1", "2026-08-15", "T")), _profile(), "amazon-business",
                                browser_factory=BrowserFactory(page))

        assert page.screenshots == 0, "and it must not fall back to a screenshot of the viewer either"

    def test_a_failed_download_does_not_store_anything(self, wired):
        recorder = wired(Recorder())
        page = self._pdf_page()
        page.get = lambda url, timeout=None: type("R", (), {"ok": False, "status": 403})()
        items = _items(("A1", "2026-08-15", "T"))

        capture.attach_receipts(items, _profile(), "amazon-business",
                                browser_factory=BrowserFactory(page))

        assert recorder.puts == []
        assert items[0].receipt_url == ""


class TestOneOrderPerReceipt:
    def test_every_row_of_a_multi_item_order_gets_the_same_link(self, wired):
        recorder = wired(Recorder())
        items = _items(("A1", "2026-08-15", "First"), ("A1", "2026-08-15", "Second"))

        capture.attach_receipts(items, _profile(), "amazon", browser_factory=BrowserFactory())

        assert len(recorder.puts) == 1, "one order is one document, however many line items"
        assert items[0].receipt_url == items[1].receipt_url != ""

    def test_rows_with_a_blank_order_id_are_skipped(self, wired):
        """ledger_sync already refuses to write them, so there is nothing to link a receipt to."""
        recorder = wired(Recorder())
        items = _items(("A1", "2026-08-15", "Real"))
        blank = OrderItem(retailer="Amazon", order_id="", order_date="2026-08-15", item_name="Orphan")
        items.append(blank)

        capture.attach_receipts(items, _profile(), "amazon", browser_factory=BrowserFactory())

        assert len(recorder.puts) == 1
        assert blank.receipt_url == ""


class TestFailureIsAlwaysPartial:
    def test_a_sign_in_page_is_refused_rather_than_stored(self, wired):
        """Storing it would render fine, upload fine, and mark the order done FOREVER — no later run
        retries, because the object now exists."""
        recorder = wired(Recorder())
        page = FakePage(url="https://www.amazon.com/ap/signin?openid.return_to=x")
        items = _items(("A1", "2026-08-15", "Thing"))

        capture.attach_receipts(items, _profile(), "amazon", browser_factory=BrowserFactory(page))

        assert recorder.puts == []
        assert items[0].receipt_url == ""

    def test_one_failing_order_does_not_stop_the_others(self, wired):
        recorder = wired(Recorder())

        class FlakyPage(FakePage):
            def goto(self, url, **kw):
                super().goto(url, **kw)
                if "A2" in url:
                    raise RuntimeError("navigation timeout")

        items = _items(("A1", "2026-08-15", "T1"), ("A2", "2026-08-15", "T2"),
                       ("A3", "2026-08-15", "T3"))
        capture.attach_receipts(items, _profile(), "amazon",
                                browser_factory=BrowserFactory(FlakyPage()))

        stored = {k for k, _, _ in recorder.puts}
        assert stored == {"receipts/amazon/2026-08/A1.pdf", "receipts/amazon/2026-08/A3.pdf"}
        assert items[0].receipt_url and items[2].receipt_url
        assert items[1].receipt_url == ""

    def test_an_upload_failure_still_links_the_orders_that_succeeded(self, wired):
        recorder = Recorder()
        calls = {"n": 0}

        def flaky_put(key, body, ext):
            calls["n"] += 1
            if calls["n"] == 1:
                raise store.ReceiptStoreError("bucket unreachable")
            return f"https://par/{key}"

        recorder.put = flaky_put  # before wiring: `wired` binds these onto the store module
        wired(recorder)
        items = _items(("A1", "2026-08-15", "T1"), ("A2", "2026-08-15", "T2"))

        capture.attach_receipts(items, _profile(), "amazon", browser_factory=BrowserFactory())

        assert items[0].receipt_url == ""
        assert items[1].receipt_url.endswith("A2.pdf")

    def test_a_storage_outage_during_the_existence_check_skips_capture_entirely(self, wired):
        """Better to record the orders with no link than to re-render everything on a bad guess."""
        wired(Recorder(exists_error=store.ReceiptStoreError("head_object 503")))
        factory = BrowserFactory()
        items = _items(("A1", "2026-08-15", "Thing"))

        capture.attach_receipts(items, _profile(), "amazon", browser_factory=factory)

        assert factory.calls == 0
        assert items[0].receipt_url == ""

    def test_an_unknown_retailer_raises_instead_of_looping_through_every_order(self, wired):
        """Misconfiguration, not a per-order problem — every order would fail identically, and the
        caller in main.py turns it into one alert."""
        wired(Recorder())
        items = _items(("A1", "2026-08-15", "T1"), ("A2", "2026-08-15", "T2"))

        with pytest.raises(KeyError):
            capture.attach_receipts(items, _profile(), "walmart", browser_factory=BrowserFactory())

    def test_an_empty_batch_does_nothing(self, wired):
        wired(Recorder())
        factory = BrowserFactory()

        capture.attach_receipts([], _profile(), "amazon", browser_factory=factory)

        assert factory.calls == 0


class TestExpandingCollapsedSections:
    """Costco hides its whole Order Summary — payment method, subtotal, shipping, tax, grand total —
    behind a "Show Details" toggle.

    Found 2026-08-21 by extracting the text of the PDFs already in the bucket, not by looking at the
    page: the receipts showed WHAT was bought and none of what it COST, and nothing about the run
    looked wrong.
    """

    @staticmethod
    def _page(retailer="costco"):
        class ExpandPage(FakePage):
            def __init__(self):
                super().__init__()
                self.evaluated = []

            def evaluate(self, js, arg=None):
                self.evaluated.append((js, arg))
                return 1  # one section clicked

        return ExpandPage()

    def test_costco_expands_the_order_summary_before_rendering(self, wired):
        wired(Recorder())
        page = self._page()

        capture.attach_receipts(_items(("1399000015", "2026-08-21", "iPad")), _profile(), "costco",
                                browser_factory=BrowserFactory(page))

        assert page.evaluated, "the Order Summary must be opened or the receipt has no money on it"
        _js, selectors = page.evaluated[0]
        assert selectors == ['[automation-id="HideorExpandOrderSummary"]']

    def test_retailers_with_nothing_collapsed_do_not_evaluate_anything(self, wired):
        wired(Recorder())
        page = self._page()

        capture.attach_receipts(_items(("A1", "2026-08-21", "Thing")), _profile(), "amazon",
                                browser_factory=BrowserFactory(page))

        assert page.evaluated == []

    def test_a_failure_to_expand_still_stores_the_receipt(self, wired):
        """A receipt missing one section beats no receipt at all."""
        recorder = wired(Recorder())

        class Broken(FakePage):
            def evaluate(self, js, arg=None):
                raise RuntimeError("evaluate blocked by CSP")

        capture.attach_receipts(_items(("1399000015", "2026-08-21", "iPad")), _profile(), "costco",
                                browser_factory=BrowserFactory(Broken()))

        assert len(recorder.puts) == 1

    def test_the_js_only_clicks_sections_that_are_actually_closed(self):
        """The control is a TOGGLE. Firing it on an already-open accordion would collapse the very
        thing we came to reveal, so a day when Costco ships it open by default must not silently
        start producing worse receipts."""
        js = sources.EXPAND_JS

        assert "aria-expanded" in js and "'false'" in js

    def test_the_expanders_are_surgical_not_every_collapsed_node(self):
        """The same Costco page carries ~29 other aria-expanded="false" nodes — footer accordions,
        nav dropdowns, tooltips — and opening those would only pad the document with chrome."""
        assert sources.expand_selectors("costco") == ('[automation-id="HideorExpandOrderSummary"]',)


class TestOnlyFinishedOrdersAreCaptured:
    """A receipt is taken ONCE and never refreshed, so the moment it is taken decides what it says.

    User decision 2026-08-21. Capturing an order that is still `ordered` or `shipped` permanently
    stores a document predating its own final totals, tracking numbers and delivery date — and
    nothing will ever go back and improve it, because a terminal order is never re-read.

    Note this is NOT about how OFTEN capture runs: storage is checked before any browser opens, so a
    re-check run of an already-captured order costs nothing either way.
    """

    @pytest.mark.parametrize("status", ["ordered", "shipped"])
    def test_an_unfinished_order_is_not_captured(self, wired, status):
        recorder = wired(Recorder())
        factory = BrowserFactory()
        items = _items(("A1", "2026-08-21", "Thing"), status=status)

        capture.attach_receipts(items, _profile(), "amazon", browser_factory=factory)

        assert recorder.puts == []
        assert factory.calls == 0, "and it must not even open a browser to find that out"
        assert items[0].receipt_url == ""

    def test_a_delivered_order_is_captured(self, wired):
        recorder = wired(Recorder())

        capture.attach_receipts(_items(("A1", "2026-08-21", "T")), _profile(),
                                "amazon", browser_factory=BrowserFactory())

        assert len(recorder.puts) == 1

    @pytest.mark.parametrize("status", ["paid", "return"])
    def test_the_buying_groups_own_statuses_are_NOT_a_live_capture_trigger(self, wired, status):
        """DELIVERED is the whole live rule.

        `paid` and `return` are the buying group's outcomes, and no scraper can emit them —
        sync_tracking writes them to the SHEET after a scrape — so they can never reach a live
        capture anyway. Excluding them here is what makes the rule say exactly what it means.
        """
        recorder = wired(Recorder())

        capture.attach_receipts(_items(("A1", "2026-08-21", "T"), status=status), _profile(),
                                "amazon", browser_factory=BrowserFactory())

        assert recorder.puts == []

    @pytest.mark.parametrize("status", ["paid", "return"])
    def test_the_backfill_may_opt_into_them(self, wired, status):
        """scripts/backfill_receipts.py reads statuses off the sheet, where a settled order genuinely
        IS finished — and is exactly the one you may later have to prove. 16 of the first 40 rows
        were `paid`, and without this they could never get a receipt from anything."""
        recorder = wired(Recorder())

        capture.attach_receipts(_items(("A1", "2026-08-21", "T"), status=status), _profile(),
                                "amazon", browser_factory=BrowserFactory(), include_settled=True)

        assert len(recorder.puts) == 1

    def test_the_backfill_opt_in_still_refuses_an_unfinished_order(self, wired):
        recorder = wired(Recorder())

        capture.attach_receipts(_items(("A1", "2026-08-21", "T"), status="shipped"), _profile(),
                                "amazon", browser_factory=BrowserFactory(), include_settled=True)

        assert recorder.puts == []

    def test_a_cancelled_order_is_never_captured(self, wired):
        """It never completed, so the receipt proves nothing and there is nothing to claim."""
        recorder = wired(Recorder())

        capture.attach_receipts(_items(("A1", "2026-08-21", "T"), status="cancelled"), _profile(),
                                "amazon", browser_factory=BrowserFactory())

        assert recorder.puts == []

    def test_a_part_delivered_split_order_waits_for_its_last_box(self, wired):
        """The receipt covers the WHOLE order, so one delivered box is not enough — capturing then
        would store a document that is out of date the moment the last box lands."""
        recorder = wired(Recorder())
        items = _items(("A1", "2026-08-21", "Box one"))          # delivered
        items += _items(("A1", "2026-08-21", "Box two"), status="shipped")

        capture.attach_receipts(items, _profile(), "amazon", browser_factory=BrowserFactory())

        assert recorder.puts == [], "one shipment still moving means the order is not finished"

    def test_a_fully_delivered_split_order_is_captured_once(self, wired):
        recorder = wired(Recorder())
        items = _items(("A1", "2026-08-21", "Box one"), ("A1", "2026-08-21", "Box two"))

        capture.attach_receipts(items, _profile(), "amazon", browser_factory=BrowserFactory())

        assert len(recorder.puts) == 1, "one order is still one document"
        assert items[0].receipt_url == items[1].receipt_url != ""

    def test_a_delivered_order_with_one_cancelled_line_still_captures(self, wired):
        """A partial cancellation is a completed purchase for whatever actually shipped."""
        recorder = wired(Recorder())
        items = _items(("A1", "2026-08-21", "Kept"))
        items += _items(("A1", "2026-08-21", "Dropped"), status="cancelled")

        capture.attach_receipts(items, _profile(), "amazon", browser_factory=BrowserFactory())

        assert len(recorder.puts) == 1

    def test_finished_and_unfinished_orders_in_one_batch_are_separated(self, wired):
        recorder = wired(Recorder())
        items = _items(("DONE", "2026-08-21", "T1"))
        items += _items(("OPEN", "2026-08-21", "T2"), status="shipped")

        capture.attach_receipts(items, _profile(), "amazon", browser_factory=BrowserFactory())

        assert [k for k, _, _ in recorder.puts] == ["receipts/amazon/2026-08/DONE.pdf"]
