"""Which page is a receipt, and what is it stored as — the pure half of receipt capture.

Every rule here is a string rule, so all of it is provable offline before a paid run renders
anything. The two that matter most are the drift guard against the scrapers' own order URLs and the
refusal to invent a URL for an unknown retailer.
"""

import pytest

from receipts import sources


class TestReceiptUrls:
    def test_amazon_uses_the_print_invoice_not_order_details(self):
        """The print invoice is the only real DOCUMENT any of these retailers publishes.

        order-details renders the same facts wrapped in recommendation carousels; the print page is
        print-styled and is what you would attach to a BFMR proof-of-purchase ticket.
        """
        url = sources.receipt_url("amazon", "113-9990003-1234567")

        assert url == "https://www.amazon.com/gp/css/summary/print.html?orderID=113-9990003-1234567"

    def test_amazon_business_shares_the_consumer_print_invoice(self):
        """Business runs on amazon.com and shares the consumer order pages verbatim.

        UNVERIFIED live until scripts/receipt_probe.py is run on the business account — if it
        diverges, this test is the one that has to change with it.
        """
        assert sources.receipt_url("amazon-business", "114-9990004-0000000") == sources.receipt_url(
            "amazon", "114-9990004-0000000"
        )

    def test_bestbuy_and_costco_use_their_order_details_pages(self):
        assert sources.receipt_url("bestbuy", "BBY01-806123456789") == (
            "https://www.bestbuy.com/profile/ss/orders/order-details/BBY01-806123456789/view"
        )
        assert sources.receipt_url("costco", "1399000007").endswith("/orderdetails/1399000007")

    def test_every_scraper_retailer_key_has_a_receipt_source(self):
        """A retailer registered in main.SCRAPERS but missing here would raise on every new order."""
        import main

        assert set(main.SCRAPERS) <= set(sources.RECEIPT_URLS)

    def test_unknown_retailer_raises_rather_than_returning_a_blank_url(self):
        """A blank URL would navigate nowhere, leave the PREVIOUS page up, and store THAT.

        Storing the wrong document is worse than storing none, and it would be permanent — the key
        exists afterwards, so no later run would ever retry.
        """
        with pytest.raises(sources.UnknownRetailerError):
            sources.receipt_url("walmart", "123")

    def test_a_blank_order_id_raises(self):
        with pytest.raises(ValueError):
            sources.receipt_url("amazon", "")


class TestUrlsMatchTheScrapersOwnOrderLinks:
    """The Best Buy and Costco receipt URLs are duplicated from their mapping modules.

    The duplication is deliberate — receipts/ must not import scrapers/, which would drag Playwright
    and curl_cffi into a dependency-free module — but a copy that can drift silently is exactly the
    kind of thing this project pins with a test instead of a comment.
    """

    def test_bestbuy_matches_bestbuy_mapping(self):
        from scrapers.bestbuy_mapping import ORDER_DETAILS_URL

        assert sources.RECEIPT_URLS["bestbuy"] == ORDER_DETAILS_URL

    def test_costco_matches_costco_mapping(self):
        from scrapers.costco_mapping import ORDER_DETAILS_URL

        assert sources.RECEIPT_URLS["costco"] == ORDER_DETAILS_URL


class TestObjectKeys:
    def test_the_key_layout(self):
        assert sources.object_key("amazon", "113-9990003-1234567", "2026-08-15", "pdf") == (
            "receipts/amazon/2026-08/113-9990003-1234567.pdf"
        )

    def test_the_key_is_stable_for_one_order_regardless_of_rows(self):
        """Idempotency rests entirely on this: one order, one key, every run.

        If the key varied with the row (item name, shipment) a multi-item order would render and
        upload once per line, and a re-check would re-render everything.
        """
        first = sources.object_key("costco", "1399000007", "2026-08-13", "pdf")
        again = sources.object_key("costco", "1399000007", "2026-08-13", "pdf")

        assert first == again

    def test_a_leading_dot_on_the_extension_is_tolerated(self):
        assert sources.object_key("amazon", "1", "2026-08-15", ".png").endswith("/1.png")

    @pytest.mark.parametrize("bad_date", ["", "2026", "not-a-date", "08/15/2026"])
    def test_an_unusable_order_date_files_under_unknown_rather_than_raising(self, bad_date):
        """A receipt in an odd folder is still a receipt; losing it to a date string would not be."""
        key = sources.object_key("amazon", "113-0000000-0000000", bad_date, "pdf")

        assert key == "receipts/amazon/unknown/113-0000000-0000000.pdf"

    def test_a_slash_in_an_order_id_cannot_create_a_nested_prefix(self):
        key = sources.object_key("amazon", "abc/def", "2026-08-15", "pdf")

        assert key == "receipts/amazon/2026-08/abc_def.pdf"

    def test_a_blank_order_id_raises(self):
        with pytest.raises(ValueError):
            sources.object_key("amazon", "", "2026-08-15", "pdf")

    def test_pdf_is_probed_before_png(self):
        """A real document must always win over a screenshot left by an older run."""
        assert sources.EXTENSIONS == ("pdf", "png")
        assert set(sources.CONTENT_TYPES) == set(sources.EXTENSIONS)


class TestLoggedOutDetection:
    @pytest.mark.parametrize("url", [
        "https://www.amazon.com/ap/signin?openid.return_to=...",
        "https://www.amazon.com/ap/mfa",
        "https://www.amazon.com/errors/validateCaptcha",
        "https://www.bestbuy.com/identity/signin",
        "https://signin.costco.com/login",
    ])
    def test_sign_in_and_captcha_pages_are_recognized(self, url):
        """Without this the capture renders a login form perfectly, uploads it, and marks the order
        done forever — no later run retries, because the object now exists."""
        assert sources.looks_logged_out(url)

    @pytest.mark.parametrize("url", [
        "https://www.amazon.com/gp/css/summary/print.html?orderID=113-9990003-1234567",
        "https://www.bestbuy.com/profile/ss/orders/order-details/BBY01-8061/view",
        "https://www.costco.com/myaccount/#/app/4900eb1f/orderdetails/1399000007",
    ])
    def test_real_receipt_pages_are_not_flagged(self, url):
        assert not sources.looks_logged_out(url)

    def test_a_blank_url_is_not_flagged(self):
        assert not sources.looks_logged_out("")


class TestPdfLandingDetection:
    """Amazon Business answers the print-invoice URL with a 302 to its OWN invoice PDF.

    Rendering that captures Chrome's PDF VIEWER rather than the document — proved live:
    the rendered file came back with Roboto and SegoeFluentIcons (Chrome's UI fonts) and 22 text
    operators, against AmazonEmber and 823 for the consumer HTML page. So a PDF landing must be
    downloaded, never re-rendered.
    """

    @pytest.mark.parametrize("url", [
        "https://www.amazon.com/documents/download/910e35d4-c4ee/order-document.pdf",
        "https://www.amazon.com/documents/download/910e35d4/order-document.pdf?ref=ppx_printOD_rd",
        "https://example.com/a/b/INVOICE.PDF",
    ])
    def test_a_pdf_landing_is_recognized_including_with_a_query_string(self, url):
        assert sources.looks_like_pdf(url)

    @pytest.mark.parametrize("url", [
        "https://www.amazon.com/gp/css/summary/print.html?orderID=113-9990003-1234567",
        "https://www.bestbuy.com/profile/ss/orders/order-details/BBY01-8061/view?pas=false",
        "https://www.costco.com/myaccount/#/app/x/orderdetails/1399000007",
        "",
    ])
    def test_an_html_page_is_not(self, url):
        assert not sources.looks_like_pdf(url)

    def test_a_pdf_in_the_query_string_only_does_not_count(self):
        """The check is on the PATH — a `?ref=x.pdf` is not a PDF document."""
        assert not sources.looks_like_pdf("https://www.amazon.com/order?ref=thing.pdf")


class TestReadySelectors:
    def test_every_retailer_has_one(self):
        assert set(sources.READY_SELECTORS) == set(sources.RECEIPT_URLS)

    def test_an_unlisted_retailer_falls_back_to_body(self):
        assert sources.ready_selector("walmart") == "body"


class TestCostcoSelectorIsAnAttributeNotAnId:
    """Costco's order page is a hash-route SPA, so the ready selector is what stops us capturing an
    empty shell — and it is easy to get subtly wrong.

    The page renders `<div automation-id="orderNumber">`, an ATTRIBUTE, not `id="orderNumber"`. A
    naive `#orderNumber` matches nothing, waits out the full timeout on every single capture, and
    then renders whatever happened to be on screen. Costco's real classes are hashed MUI names
    (`css-1pzz4na`), so class-substring matching is no good either.
    """

    def test_it_targets_the_automation_id_attribute(self):
        selector = sources.ready_selector("costco")

        assert '[automation-id="orderNumber"]' in selector
        assert "#orderNumber" not in selector, "that is an attribute, not an id — it would match nothing"

    def test_it_does_not_rely_on_hashed_mui_class_names(self):
        assert "class*=" not in sources.ready_selector("costco")


class TestSplitOrdersWaitForEveryShipment:
    """A receipt is captured ONCE and never refreshed, so the moment it is taken is permanent.

    Briefly on 2026-08-21 the rule was "ANY row shipped", to make receipts available sooner. That
    stored a partial invoice for a split order — `Not Yet Shipped` printed against the shipments that
    had not moved — and nothing would ever go back and improve it. These documents substantiate COGS,
    so the whole order ships first.
    """

    @pytest.mark.parametrize("statuses, capturable", [
        (["shipped"], True),
        (["delivered"], True),
        (["shipped", "shipped"], True),
        (["shipped", "delivered"], True),
        (["shipped", "ordered"], False),      # one box still in flight
        (["delivered", "ordered"], False),
        (["ordered"], False),
        (["cancelled"], False),
        (["cancelled", "cancelled"], False),
        (["shipped", "cancelled"], True),     # partial cancellation, rest shipped
        ([], False),
    ])
    def test_the_truth_table(self, statuses, capturable):
        assert sources.is_capturable(statuses) is capturable

    def test_an_unknown_status_blocks_rather_than_guesses(self):
        """Never assume an unrecognized state means finished — that is how a partial invoice gets
        stored permanently."""
        assert sources.is_capturable(["shipped", "some-new-status"]) is False

    def test_the_buying_groups_statuses_need_the_backfill_opt_in(self):
        assert sources.is_capturable(["paid"]) is False
        assert sources.is_capturable(["paid"], include_settled=True) is True

    def test_the_opt_in_does_not_relax_the_whole_order_rule(self):
        assert sources.is_capturable(["paid", "ordered"], include_settled=True) is False


class TestAFinalDocumentOverridesAStrayMarker:
    """Amazon's invoice has a section per shipment, so a genuinely-final one can still contain
    `Not Yet Shipped` for a cancelled or straggling line.

    Matching that phrase anywhere would refuse a receipt for goods that shipped — and refuse it again
    every run, since capture retries. The TITLE is the real discriminator: all 10 stored Amazon
    Business invoices read `Final Details for Order #…`, while the one pre-shipment invoice read
    `Details for Order #…` with no "Final".
    """

    def test_a_final_invoice_is_accepted(self):
        text = "Final Details for Order #114-9990030-9990030 ... Shipped on August 20, 2026"

        assert sources.not_final_reason("amazon-business", text) is None

    def test_final_wins_over_a_not_yet_shipped_section(self):
        text = "Final Details for Order #1 ... Shipped on Aug 20 ... Not Yet Shipped ... Item B"

        assert sources.not_final_reason("amazon-business", text) is None

    def test_a_pre_shipment_invoice_is_still_refused(self):
        text = "Details for Order #111-9990021-9990021 ... Not Yet Shipped"

        assert sources.not_final_reason("amazon-business", text) == "Not Yet Shipped"

    def test_retailers_without_markers_are_unaffected(self):
        """Consumer Amazon, Best Buy and Costco renders carry no finality wording either way, so
        inventing one would only produce false rejections."""
        for key in ("amazon", "bestbuy", "costco"):
            assert sources.not_final_reason(key, "Not Yet Shipped") is None

    def test_unextractable_text_fails_open(self):
        assert sources.not_final_reason("amazon-business", "") is None
