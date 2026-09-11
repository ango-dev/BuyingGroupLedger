"""RECON: where (if anywhere) does Best Buy's site expose the units' SERIAL NUMBERS?

BFMR's combined-package emails ask for per-unit serials, and nothing captured so far carries
them: the ss-api order payload doesn't (5 live orders, 1001 distinct key paths, zero hits) and
neither does the rendered order-details HTML from August — though those captures likely predate
fulfillment, and Best Buy's own help says serials appear on your previous orders. The
production fetch (`scrapers/bestbuy_serials.py`, called by respond_bfmr on --apply) hunts the
same two surfaces with labelled-only extraction; when it comes up empty for a shipped order,
THIS probe is the richer recon that pins where serials actually render (sub-views, app-only
endpoints), and its capture is the fixture any fix gets built against.

What it does, riding scripts/bestbuy_capture.py's proven helpers (CDP attach, deterministic
password login, network capture, in-page fetch):

  1. Opens the order-details page for the given SHIPPED order; scrolls; dumps HTML + PNG + a
     full XHR manifest (with "serial" added to the interesting-URL keywords).
  2. Clicks every expandable affordance whose text suggests a per-item sub-view ("View details",
     "Return or replace", "Manage", "Serial"), re-dumping the page after each — serials often
     render only inside such a sub-view.
  3. Greps everything it saw — the DOM after every step, every captured XHR body — for
     serial-shaped content and writes the hits with context to serial_hits.json.

Read-only against Best Buy, but it spends one Browser-Use CDP-browser fee and touches the live
account — ASK BEFORE RUNNING (CLAUDE.md's live-validation gate):

    python -m scripts.bestbuy_serial_probe --label profile-alpha --order-id BBY01-<digits>

Output: .bestbuy_capture/serial_probe/ (gitignored — real order data).
"""

import argparse
import json
import logging
import re
import sys
from pathlib import Path

import config.settings  # noqa: F401  (puts BROWSER_USE_API_KEY in the environment — see bestbuy_capture)
from scripts.bestbuy_capture import (
    ORDER_DETAILS_URL,
    _capture,
    _deterministic_login,
    _dismiss_survey,
    _dump_page_diagnostics,
    _inpage_fetch,
    _looks_logged_out,
)
import scripts.bestbuy_capture as bestbuy_capture

log = logging.getLogger("bestbuy_serial_probe")

#: Sub-views worth expanding: button/link text that hides per-item detail behind a click.
_EXPANDABLE_TEXT = re.compile(r"serial|view details|see details|item details|return or replace|manage",
                              re.IGNORECASE)

#: The word itself, and Apple-style serial shapes near it, hunted with context.
_SERIAL_WORD = re.compile(r"serial", re.IGNORECASE)


def _collect_serial_hits(text: str, source: str, hits: list) -> None:
    for match in _SERIAL_WORD.finditer(text):
        start = max(0, match.start() - 120)
        hits.append({"source": source,
                     "context": " ".join(text[start:match.end() + 160].split())})


def _click_expandables(page, out_dir: Path, hits: list) -> None:
    """Open every sub-view that might hold per-item detail, dumping the page after each."""
    try:
        candidates = page.evaluate(
            """() => Array.from(document.querySelectorAll('button, a, summary'))
                     .map((el, i) => ({i, text: (el.innerText || '').trim().slice(0, 80)}))
                     .filter(c => c.text)"""
        )
    except Exception:
        log.warning("Could not enumerate clickable elements.", exc_info=True)
        return

    to_click = [c for c in candidates if _EXPANDABLE_TEXT.search(c["text"])]
    log.info("%d expandable affordance(s): %s", len(to_click), [c["text"] for c in to_click][:10])
    for n, c in enumerate(to_click[:8]):  # bounded: this is recon, not a crawl
        try:
            _dismiss_survey(page)
            page.evaluate(
                """(i) => { const els = document.querySelectorAll('button, a, summary');
                            if (els[i]) els[i].click(); }""", c["i"])
            page.wait_for_timeout(2500)
            tag = f"expanded_{n:02d}_" + "".join(ch if ch.isalnum() else "_" for ch in c["text"])[:40]
            _dump_page_diagnostics(page, out_dir, tag)
            _collect_serial_hits(page.content(), tag, hits)
            page.go_back(wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)
        except Exception:
            log.debug("expand %r failed; continuing", c["text"], exc_info=True)


def run(label: str, order_id: str, out_dir: Path) -> int:
    from config.profiles import load_profiles
    from scrapers.cdp import CdpBrowser

    profile = next((p for p in load_profiles() if p.label == label), None)
    if profile is None:
        sys.exit(f"No profile '{label}' configured.")
    auth = profile.auth.get("bestbuy")

    out_dir.mkdir(parents=True, exist_ok=True)
    captured: list = []
    manifest: list = []
    hits: list = []

    # Widen the capture net: any URL mentioning "serial" is interesting by definition here.
    bestbuy_capture._INTERESTING_URL = tuple(bestbuy_capture._INTERESTING_URL) + ("serial",)

    try:
        with CdpBrowser(profile) as page:
            _capture(page, captured, manifest, out_dir)

            url = ORDER_DETAILS_URL.format(order_id)
            log.info("Loading order details for %s…", order_id)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)

            if _looks_logged_out(page):
                if auth is None or auth.method != "password" or not auth.username:
                    log.error("Logged out and profile '%s' has no bestbuy password auth.", label)
                    _dump_page_diagnostics(page, out_dir, "logged_out_no_auth")
                    return 1
                _deterministic_login(page, auth, out_dir)
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(4000)

            for _ in range(4):
                try:
                    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    pass
                page.wait_for_timeout(1500)

            _dump_page_diagnostics(page, out_dir, "order_details")
            _collect_serial_hits(page.content(), "order_details", hits)

            _click_expandables(page, out_dir, hits)

            # The known data endpoint one more time, on a SHIPPED order, in case serials appear
            # post-fulfillment under a key the August captures predate.
            details = _inpage_fetch(page, [f"/profile/ss/api/v1/orders/{order_id}"])
            for u, r in details.items():
                body = r.get("body", "")
                if body:
                    (out_dir / f"detail_{order_id}.json").write_text(body, encoding="utf-8")
                    _collect_serial_hits(body, f"ss-api {u}", hits)
    except Exception:
        log.exception("Probe hit an error; writing out whatever was captured before it.")

    for entry in captured:
        _collect_serial_hits(json.dumps(entry.get("response_body"), default=str),
                             f"graphql {entry.get('operation_names')}", hits)

    (out_dir / "xhr_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (out_dir / "serial_hits.json").write_text(json.dumps(hits, indent=2), encoding="utf-8")

    print(f"\n{len(hits)} serial-shaped hit(s) -> {out_dir / 'serial_hits.json'}")
    for h in hits[:20]:
        print(f"  [{h['source']}] …{h['context'][:160]}…")
    if not hits:
        print("Nothing on the order-details surface mentions serials. If the Best Buy APP shows "
              "them, they come over an app-only API — serials stay hand-typed in that case.")
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", default="profile-alpha", help="Profile that owns the Best Buy login")
    parser.add_argument("--order-id", required=True,
                        help="A SHIPPED order to inspect (serials only exist post-fulfillment)")
    parser.add_argument("--out", default=".bestbuy_capture/serial_probe", help="Output dir (gitignored)")
    args = parser.parse_args()
    sys.exit(run(args.label, args.order_id, Path(args.out)))


if __name__ == "__main__":
    main()
