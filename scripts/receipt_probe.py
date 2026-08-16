"""Decision gate for receipt capture: can a Browser-Use cloud browser render a page to PDF?

    python -m scripts.receipt_probe --label profile-bravo --retailer amazon --order-id 113-...
    python -m scripts.receipt_probe --label profile-alpha --retailer costco --order-id 1399000007

Writes to a gitignored `.receipt_capture/` and **UPLOADS NOTHING**. It costs one short cloud-browser
session per invocation; run it before trusting the capture path, and look at what it produced.

WHY THIS EXISTS. Playwright's `page.pdf()` refuses outright on headful Chromium, and Browser-Use
cloud browsers are headful (they serve a live-view URL). The raw CDP command `Page.printToPDF`
usually works anyway because the restriction is client-side in Playwright — but headful Chrome has
historically answered `PrintToPDF is not implemented`, so this is a genuine unknown and not
something to discover from an unattended run. Same discipline as the Amazon and Amazon Business
network captures: capture first, then build against what you saw.

It also settles three smaller questions the table in receipts/sources.py currently guesses at:
  - does Amazon's print invoice work for an Amazon BUSINESS account, or does it need another path?
  - does Best Buy have any printable view beyond order-details?
  - what selector actually signals Costco's hash-route SPA has rendered?

Read the output, then fix receipts/sources.py — that is the whole point of running this.
"""

from __future__ import annotations

import argparse
import base64
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

# CdpBrowser talks to Browser-Use, which reads BROWSER_USE_API_KEY from the environment. Nothing
# else this script imports pulls in config.settings, so the .env has to be loaded here.
load_dotenv()

from config.profiles import load_profiles_for_retailer  # noqa: E402
from receipts.sources import (  # noqa: E402
    looks_like_pdf,
    looks_logged_out,
    ready_selector,
    receipt_url,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("receipt_probe")

OUT_DIR = Path(".receipt_capture")


def render_pdf(page) -> bytes:
    """Render the current page to PDF over raw CDP, bypassing Playwright's headless-only check.

    `page.pdf()` raises "PDF generation is only supported for Headless Chromium" before it ever
    reaches the browser; the protocol command underneath has no such restriction, so going straight
    to it is what makes this possible at all on a cloud browser.
    """
    session = page.context.new_cdp_session(page)
    try:
        result = session.send("Page.printToPDF", {
            "printBackground": True,
            # Honour the page's own @page CSS. Amazon's print invoice is print-styled, so this is
            # the difference between a document that looks like a receipt and one that looks like a
            # screenshot of a website.
            "preferCSSPageSize": True,
        })
        return base64.b64decode(result["data"])
    finally:
        try:
            session.detach()
        except Exception:  # noqa: BLE001 — detaching is cleanup; a failure here must not lose the PDF
            pass


def probe(label: str, retailer: str, order_id: str, out_dir: Path) -> int:
    profiles = [p for p in load_profiles_for_retailer(retailer) if p.label == label]
    if not profiles:
        # Fall back to any profile carrying that label, so the probe still works for a retailer whose
        # profile lists a different key (e.g. probing costco.com on a profile that only lists costco
        # for the token path).
        log.error("No profile labelled %r lists retailer %r in profiles.json.", label, retailer)
        return 1
    profile = profiles[0]

    out_dir.mkdir(parents=True, exist_ok=True)
    url = receipt_url(retailer, order_id)
    tag = f"{retailer}_{order_id}"

    # Imported here, not at module scope, so --help works without playwright installed.
    from scrapers.cdp import CdpBrowser

    # block_resources=False: a receipt with no logos or fonts is a poor document. This is the one
    # place the extra proxy bandwidth is worth paying for.
    with CdpBrowser(profile, block_resources=False) as page:
        log.info("Loading %s", url)
        page.goto(url, wait_until="domcontentloaded", timeout=90000)

        # Amazon Business redirects the print-invoice URL to its OWN invoice PDF. Rendering that
        # captures Chrome's PDF viewer, not the document, so production downloads it instead —
        # mirrored here so the probe reports what production would actually store.
        if looks_like_pdf(page.url or ""):
            log.info("Landed on a PDF document (%s) — downloading it rather than rendering.",
                     page.url)
            body = page.context.request.get(page.url, timeout=90000).body()
            (out_dir / f"{tag}.pdf").write_bytes(body)
            print()
            print(f"  retailer      {retailer}")
            print(f"  order         {order_id}")
            print(f"  PDF           RETAILER'S OWN DOCUMENT ({len(body)} bytes, downloaded)")
            print(f"  output        {out_dir.resolve()}")
            return 0

        selector = ready_selector(retailer)
        try:
            page.wait_for_selector(selector, timeout=30000)
            log.info("Ready selector %r matched.", selector)
        except Exception:  # noqa: BLE001
            log.warning("Ready selector %r never matched — capturing anyway so you can SEE what "
                        "rendered. If the output is a spinner or an empty shell, that selector is "
                        "the thing to fix in receipts/sources.py.", selector)
        # Settle late-loading images/webfonts, which affect how the render looks but not whether the
        # selector matched.
        page.wait_for_timeout(3000)

        final_url = page.url or ""
        log.info("Final URL: %s", final_url)
        if looks_logged_out(final_url):
            log.error("LANDED ON A SIGN-IN / CAPTCHA PAGE. In production this is detected and the "
                      "capture is skipped rather than storing a PDF of a login form.")

        (out_dir / f"{tag}_url.txt").write_text(final_url, encoding="utf-8")
        try:
            (out_dir / f"{tag}.html").write_text(page.content(), encoding="utf-8")
        except Exception:  # noqa: BLE001
            log.debug("content() dump failed", exc_info=True)

        pdf_ok = False
        try:
            pdf = render_pdf(page)
            (out_dir / f"{tag}.pdf").write_bytes(pdf)
            pdf_ok = True
            log.info("PDF OK — %d bytes -> %s", len(pdf), out_dir / f"{tag}.pdf")
        except Exception as exc:  # noqa: BLE001 — the whole question is WHETHER this raises
            log.warning("Page.printToPDF FAILED (%s: %s). This is the answer the probe exists to "
                        "get; the PNG below is what production would store instead.",
                        type(exc).__name__, exc)

        try:
            page.screenshot(path=str(out_dir / f"{tag}.png"), full_page=True)
            log.info("PNG fallback OK -> %s", out_dir / f"{tag}.png")
        except Exception:  # noqa: BLE001
            log.error("Full-page screenshot ALSO failed — there is no working capture path for this "
                      "page.", exc_info=True)
            return 1

    print()
    print(f"  retailer      {retailer}")
    print(f"  order         {order_id}")
    print(f"  PDF           {'WORKS' if pdf_ok else 'NOT AVAILABLE — production would store a PNG'}")
    print(f"  output        {out_dir.resolve()}")
    print()
    print("  Open the PDF/PNG. It must show the items, prices, totals and payment method — if it "
          "shows a sign-in page, a spinner or an empty shell, fix receipts/sources.py before "
          "wiring capture into a real run.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--label", required=True, help="profiles.json label to run on")
    parser.add_argument("--retailer", required=True,
                        help="amazon | amazon-business | bestbuy | costco")
    parser.add_argument("--order-id", required=True, help="a real order id on that account")
    parser.add_argument("--out", default=str(OUT_DIR), help="output dir (gitignored)")
    args = parser.parse_args(argv)
    return probe(args.label, args.retailer, args.order_id, Path(args.out))


if __name__ == "__main__":
    sys.exit(main())
