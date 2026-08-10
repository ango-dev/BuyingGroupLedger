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

from alerts.notifier import alert  # noqa: E402
from config.profiles import load_profiles_for_retailer  # noqa: E402
from output.csv_writer import write_csv  # noqa: E402
from scrapers.amazon import AmazonScraper  # noqa: E402
from scrapers.base import BaseRetailerScraper, LoggedOutError  # noqa: E402
from scrapers.bestbuy import BestBuyScraper  # noqa: E402
from scrapers.costco import CostcoScraper  # noqa: E402
from sheets.ledger_sync import sync_csv_to_sheet  # noqa: E402

log = logging.getLogger("main")

SCRAPERS: dict[str, type[BaseRetailerScraper]] = {
    "amazon": AmazonScraper,
    "bestbuy": BestBuyScraper,
    "costco": CostcoScraper,
}


def run_scrape(scraper: BaseRetailerScraper) -> None:
    label = f"{scraper.retailer_name} [{scraper.profile.label}]"
    log.info("Scraping %s...", label)

    try:
        items = scraper.scrape()
    except LoggedOutError:
        log.warning("%s session is logged out; alert sent, skipping.", label)
        return
    except Exception:
        log.exception("Scrape failed for %s", label)
        alert(f"{label}: scrape failed", "Unhandled error during scrape. Check logs/run.log.")
        return

    if not items:
        log.info("No orders found for %s (nothing new in the lookback window).", label)
        return

    csv_path = write_csv(items)
    log.info("Wrote %d line item(s) to %s", len(items), csv_path)

    try:
        sync_csv_to_sheet(csv_path)
        log.info("Synced %s into the Google Sheet ledger.", csv_path.name)
    except Exception:
        log.exception("Sheet sync failed for %s", csv_path)
        alert("Sheet sync failed", f"Failed to sync {csv_path} into the ledger. Check logs/run.log.")


def main(retailers: list[str]) -> None:
    for name in retailers:
        scraper_cls = SCRAPERS[name]
        profiles = load_profiles_for_retailer(scraper_cls.retailer_key)
        if not profiles:
            log.warning("No profiles configured for '%s' in profiles.json; skipping.", name)
            continue

        for profile in profiles:
            if not profile.profile_id:
                log.warning(
                    "Skipping profile '%s' for '%s': no profile_id set (run scripts.create_profile first).",
                    profile.label,
                    name,
                )
                continue
            run_scrape(scraper_cls(profile))


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
