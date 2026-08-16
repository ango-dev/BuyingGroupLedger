"""attach_receipts: what it stores, what it links, and — most importantly — when it opens nothing.

The single most valuable assertion in this file is the negative one: on a run where every order
already has a stored receipt, NO cloud browser is created. That is what makes it safe to leave this
step switched on for every scheduled run, since the steady state is re-checking open orders.
"""

import pytest

from models.order import OrderItem
from models.profile import ProfileConfig
from receipts import capture, store


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(store, "is_configured", lambda: True)
    monkeypatch.setattr(store, "link_for", lambda key: f"https://par/{key}")


def _items(*specs):
    """specs: (order_id, order_date, item_name)."""
    return [
        OrderItem(retailer="Amazon", profile_label="p", order_id=oid, order_date=date,
                  item_name=name, quantity=1, cost_per_item=10.0)
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
