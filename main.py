import logging
import os
import sys
import time
from pathlib import Path

Path("logs").mkdir(exist_ok=True)

# Prevent overlapping scheduled runs (a run that overruns its interval would otherwise collide
# with the next one on the same profiles/browsers). A lock older than this is treated as stale
# (previous run crashed) and overridden.
_LOCK_FILE = Path("logs") / ".run.lock"
_LOCK_STALE_SECONDS = 3 * 60 * 60  # 3h


def _acquire_lock() -> bool:
    if _LOCK_FILE.exists():
        age = time.time() - _LOCK_FILE.stat().st_mtime
        if age < _LOCK_STALE_SECONDS:
            return False
    _LOCK_FILE.write_text(f"{os.getpid()} {time.time()}", encoding="utf-8")
    return True


def _release_lock() -> None:
    try:
        _LOCK_FILE.unlink()
    except FileNotFoundError:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path("logs") / "run.log", encoding="utf-8"),
    ],
)

from functools import lru_cache  # noqa: E402

from alerts.notifier import alert  # noqa: E402
from config.cards import load_cards, tag_cards  # noqa: E402
from config.profiles import load_profiles_for_retailer  # noqa: E402
from config.settings import settings  # noqa: E402
from config.warehouses import load_warehouses, tag_and_filter_personal  # noqa: E402
from output.csv_writer import write_csv  # noqa: E402
from scrapers.amazon import AmazonScraper  # noqa: E402
from scrapers.amazon_business import AmazonBusinessScraper  # noqa: E402
from scrapers.base import (  # noqa: E402
    BaseRetailerScraper,
    DeterministicPathError,
    LoggedOutError,
    ScrapeUnavailableError,
)
from scrapers.bestbuy import BestBuyScraper  # noqa: E402
from scrapers.costco import CostcoScraper  # noqa: E402
from sheets.ledger_sync import sort_ledger_by_date_desc, sync_csv_to_sheet  # noqa: E402

log = logging.getLogger("main")

SCRAPERS: dict[str, type[BaseRetailerScraper]] = {
    "amazon": AmazonScraper,
    "amazon-business": AmazonBusinessScraper,
    "bestbuy": BestBuyScraper,
    "costco": CostcoScraper,
}


# Loaded once per process (the file rarely changes within a run) and shared across every profile x
# retailer. lru_cache also means a missing/edited warehouses.json is read at most once.
@lru_cache(maxsize=1)
def _warehouses():
    return load_warehouses()


@lru_cache(maxsize=1)
def _cards():
    return load_cards()


def _tag_cards(items: list, label: str) -> None:
    """Resolve each row's card name + cashback rate from the last 4 digits the scraper captured.

    Same one-call-site reasoning as _classify_and_drop_personal: every retailer
    funnels through run_scrape. Unlike the warehouse classifier this never
    drops a row — an unrecognized card is only a missing profit input, not a reason to lose an order.
    """
    unknown = tag_cards(items, _cards(), apply_promo=settings.amazon_promo_cashback_enabled)
    if unknown:
        log.info(
            "%s: %d row(s) have a card ending in digits not listed in config.json `cards` (default "
            "cashback rate applied).",
            label, unknown,
        )


def _classify_and_drop_personal(items: list, label: str) -> list:
    """Tag each row's buying group from its delivery address and drop personal rows.

    Runs here because every retailer funnels through run_scrape, so one call
    site handles them all. Personal-address orders are excluded from the ledger entirely; Unclassified
    (unrecognized) rows are kept and counted so a not-yet-configured warehouse stays visible.
    """
    kept, dropped_personal, unclassified = tag_and_filter_personal(items, _warehouses())
    if dropped_personal:
        log.info("%s: dropped %d personal-address row(s) (not recorded).", label, dropped_personal)
    if unclassified:
        log.info(
            "%s: %d row(s) tagged Unclassified (address matched no configured warehouse jig).",
            label, unclassified,
        )
    return kept


def _capture_receipts(items: list, scraper: BaseRetailerScraper, label: str) -> None:
    """Store each new order's receipt and link it from its rows. NEVER raises.

    Swallowing everything is the whole contract. This step is additive — a receipt is proof of
    purchase you'll want when a buying group asks for one — but a missing receipt costs an
    inconvenience while a missing ORDER costs reimbursement money. So a storage outage, an expired
    browser session or a changed page must not stop write_csv and sync_csv_to_sheet from running
    three lines later, which is exactly what would happen if this propagated into run_scrape's
    post-scrape guard.

    Inert (and silent after one log line) when no bucket is configured.
    """
    try:
        from receipts.capture import attach_receipts  # local: keeps `import main` free of boto3

        attach_receipts(items, scraper.profile, scraper.retailer_key)
    except Exception:
        log.exception("Receipt capture failed for %s", label)
        alert(f"{label}: receipt capture failed",
              "The orders themselves were still recorded and synced; only their Receipt Link is "
              "missing, and the next run retries. Check logs/run.log.")


def run_scrape(scraper: BaseRetailerScraper) -> None:
    label = f"{scraper.retailer_name} [{scraper.profile.label}]"
    log.info("Scraping %s...", label)

    try:
        items = scraper.scrape()
    except LoggedOutError:
        log.warning("%s session is logged out; alert sent, skipping.", label)
        return
    except ScrapeUnavailableError as exc:
        # Deliberately NOT folded into the branch above. "Logged out" sends someone to re-authorize;
        # this means the retailer was unreachable and the account is fine. The scraper has already
        # alerted with the real diagnosis.
        log.warning("%s could not be reached (%s); alert sent, skipping.", label, exc)
        return
    except DeterministicPathError as exc:
        # The page/API changed shape. The scraper already wrote the
        # failure dossier and alerted with its path; the fix is a code change, not a re-login.
        log.warning("%s deterministic path failed (%s); dossier written, alert sent, skipping.",
                    label, exc)
        return
    except Exception:
        log.exception("Scrape failed for %s", label)
        alert(f"{label}: scrape failed", "Unhandled error during scrape. Check logs/run.log.")
        return

    if not items:
        log.info("No orders found for %s (nothing new in the lookback window).", label)
        return

    # Guarded for the same reason scrape() is: this ran UNPROTECTED until 2026-08-14, so a bad address
    # in warehouses.json, an unreadable cards.json or a disk error in write_csv would propagate out of
    # run_scrape, out of main()'s loop, and take every remaining retailer AND the buying-group sync
    # with it — from a failure that only concerned one retailer.
    try:
        items = _classify_and_drop_personal(items, label)
        if not items:
            log.info("Nothing to record for %s (all scraped rows were personal addresses).", label)
            return

        # After the personal-address drop, so no work is spent resolving cards for rows we discard.
        _tag_cards(items, label)

        # Before write_csv, so the Receipt Link lands in the SAME sheet sync as the rows it belongs
        # to. _capture_receipts never raises — see its docstring.
        _capture_receipts(items, scraper, label)

        csv_path = write_csv(items)
    except Exception:
        log.exception("Post-scrape processing failed for %s", label)
        alert(f"{label}: post-scrape processing failed",
              "Orders were scraped but could not be classified/tagged/written. Check logs/run.log.")
        return
    log.info("Wrote %d line item(s) to %s", len(items), csv_path)

    try:
        result = sync_csv_to_sheet(csv_path)
        log.info("Synced %s into the Google Sheet ledger.", csv_path.name)
    except Exception:
        # This alert has always existed, but it never said what was LOST. A sync failure discards
        # every row that run scraped — live it silently dropped 4 Amazon Business rows
        # twice — so the count and the retailer are the facts worth putting in front of someone.
        log.exception("Sheet sync failed for %s (%d row(s) NOT recorded)", csv_path, len(items))
        alert(
            f"{label}: sheet sync FAILED — {len(items)} row(s) not recorded",
            f"{len(items)} scraped row(s) could not be written to the ledger and are not on the "
            f"sheet. The CSV is kept at {csv_path} if they are needed. Open orders will be "
            "re-scraped next run; a newly-discovered order is only re-found while it stays in the "
            "lookback window. Check logs/run.log.",
        )
        return

    # Keep the ledger newest-first. Only APPENDS can put rows out of order — an update rewrites a row
    # where it already sits — so the common re-check run (0 appended) skips this entirely rather than
    # paying a full re-stamp of every formula each time. Sorting happens AFTER the sync so the row
    # numbers sync cached from its pre-sync snapshot are never invalidated mid-write.
    if (result or {}).get("appended"):
        try:
            sort_ledger_by_date_desc()
        except Exception:
            # The scraped rows are already safely written; a sort failure only leaves them out of
            # order, which the next append-triggered sort (or scripts/sort_ledger.py) fixes.
            log.exception("Ledger sort failed after syncing %s", csv_path)
            alert(
                "Ledger sort failed",
                f"Rows from {csv_path.name} were written to the sheet, but the newest-first re-sort "
                "afterwards failed, so the ledger may be out of order. The data itself is intact. "
                "Run `python -m scripts.sort_ledger --apply` to fix. Check logs/run.log.",
            )


def main(retailers: list[str]) -> None:
    for name in retailers:
        scraper_cls = SCRAPERS[name]
        profiles = load_profiles_for_retailer(scraper_cls.retailer_key)
        if not profiles:
            log.warning("No profiles configured for '%s' in config.json `profiles`; skipping.", name)
            continue

        for profile in profiles:
            if not profile.profile_id:
                log.warning(
                    "Skipping profile '%s' for '%s': no profile_id set (run scripts.create_profile first).",
                    profile.label,
                    name,
                )
                continue
            # Backstop for the per-retailer isolation this loop promises. run_scrape guards its own
            # steps, but a failure in the scraper's CONSTRUCTOR — or anything new added here later —
            # would otherwise abort the whole run. Live a Best Buy sign-in took the rest of
            # a run with it (Costco never ran, and neither did the sync), so the isolation is worth
            # asserting here rather than trusting every callee to keep it.
            try:
                run_scrape(scraper_cls(profile))
            except Exception:
                log.exception("Retailer '%s' [%s] failed; continuing with the rest of the run.",
                              name, profile.label)
                alert(f"{name} [{profile.label}]: run failed",
                      "That retailer was skipped; the rest of the run continued. Check logs/run.log.")

    # Deliberately outside the loop AND reached even if every retailer failed: the sync submits
    # tracking for rows already on the sheet from earlier runs, so it has work to do regardless.
    run_buying_group_sync()


def run_buying_group_sync() -> None:
    """Post newly-shipped tracking numbers to the buying groups and read their payouts back.

    Runs AFTER every scraper, inside the same run lock, because it reads the sheet the scrapers have
    just finished writing — a tracking number discovered this run is submitted in the same run.

    Failures here never fail the run: the scraped orders are already safely on the sheet, and the
    submission is retried on the next pass. Same isolation rule as run_scrape.

    OFF BY DEFAULT, behind BUYING_GROUP_SYNC_ENABLED. This path submits to third parties and files
    BFMR insurance, which spends real money per shipment — and none of it has been exercised against
    a live account yet. So the wiring exists (nobody has to remember to add it later) but stays
    inert until the manual validation in the plan has actually been done:

        python -m scripts.bg_probe          # read-only; settles the open API questions
        python -m sync_tracking             # dry run; shows exactly what would be sent
        python -m sync_tracking --apply --limit 1
    """
    if not settings.buying_group_sync_enabled:
        log.info(
            "Buying-group sync is disabled (set buying_groups.sync_enabled to true in config.json — "
            "or BUYING_GROUP_SYNC_ENABLED=true for one run — once you've validated it manually; "
            "see `python -m sync_tracking --help`)."
        )
        return
    try:
        from sync_tracking import run as run_tracking_sync  # local: keeps `import main` cheap

        run_tracking_sync(apply=True)
    except Exception:
        log.exception("Buying-group sync failed")
        alert("Buying-group sync failed",
              "Tracking numbers may not have been submitted. Check logs/run.log.")


if __name__ == "__main__":
    targets = sys.argv[1:] or list(SCRAPERS.keys())
    unknown = [t for t in targets if t not in SCRAPERS]
    if unknown:
        print(f"Unknown retailer(s): {', '.join(unknown)}. Available: {', '.join(SCRAPERS)}")
        sys.exit(1)

    if not _acquire_lock():
        log.warning("Another run appears to be in progress (%s exists); exiting.", _LOCK_FILE)
        sys.exit(0)
    try:
        main(targets)
    finally:
        _release_lock()
