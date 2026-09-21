"""
Fill the Promo Rate column for rows already on the ledger -- OFFLINE, from the numbers the ledger
holds, no browser, no profile.

Promo Rate (added beside Cashback Rate on 2026-09-20) records Amazon's per-order "... plus an
extra N% back ..." on its own. Before it existed the promo was folded into Cashback Rate, so on an
old Amazon / Amazon Business row the promo is whatever the Cashback Rate cell holds ABOVE the
card's configured rate for that retailer (config.json `cards`, resolved through the same
config.cards.resolve_card the scrapers use). That difference is moved into Promo Rate; the
Cashback Rate cell is never touched.

WHAT IS FILLED, AND WHAT IS LEFT FOR YOU. A row is filled only when all of these hold, because a
difference from the configured rate can also mean an older-era rate (a rate cell records the rate
at purchase time; the config may have moved since):

  - the retailer is Amazon or Amazon Business (only Amazon advertises the promo);
  - the card resolves to a configured entry with a rate for that retailer;
  - Promo Rate is blank, and Cashback Rate was not typed by hand (ledger_db/hand_edits);
  - the difference is a promo-shaped step: a positive multiple of 0.5% up to 10%.

Every other row with a difference is LISTED as unexplained, never guessed at: read the list, and
if a row's difference is a promo, put it in Promo Rate on the Orders page yourself (or fix the
rate). scripts/backfill_amazon_promo re-reads the order pages when you would rather have the
truth than the inference -- at the cost of a cloud browser session.

DRY RUN BY DEFAULT -- reads the ledger and writes nothing:

    python -m scripts.backfill_promo_rate

Apply for real (one batched write of the Promo Rate cells only):

    python -m scripts.backfill_promo_rate --apply
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger_db.worksheet import ValueInputOption, ValueRenderOption  # noqa: E402

from config.cards import load_cards, resolve_card  # noqa: E402
from ledger.sync import HEADER, _col_letter, _get_worksheet  # noqa: E402
from models.card import normalize_last4, normalize_retailer, parse_rate  # noqa: E402

log = logging.getLogger("backfill_promo_rate")

AMAZON = {normalize_retailer("Amazon"), normalize_retailer("Amazon Business")}
STEP = 0.005        # promos come in half-percent steps
MAX_PROMO = 0.10    # above this the difference is something else


def _rate(value):
    try:
        parsed = parse_rate(value if isinstance(value, str) else str(value))
    except (TypeError, ValueError):
        return None
    return float(parsed) if parsed is not None else None


def promo_step(difference: float) -> float | None:
    """The difference as a promo (a positive multiple of STEP up to MAX_PROMO), else None."""
    if difference <= 1e-9 or difference > MAX_PROMO + 1e-9:
        return None
    steps = difference / STEP
    if abs(steps - round(steps)) > 1e-6:
        return None
    return round(round(steps) * STEP, 4)


def plan_promo_backfill(header: list[str], data_rows: list[list], cards, protected: dict | None = None,
                        default_rate: float | None = None) -> dict:
    """{will_write: [(row_number, promo)], unexplained: [(row_number, order_id, cell, configured)],
    skipped_hand: [row_number]} -- the ledger read as get_values gives it, header first."""
    idx = {name: i for i, name in enumerate(header)}

    def cell(row, name):
        i = idx.get(name)
        return row[i] if i is not None and i < len(row) else ""

    protected = protected or {}
    will_write, unexplained, skipped_hand = [], [], []
    for number, row in enumerate(data_rows, start=2):
        if normalize_retailer(str(cell(row, "Retailer"))) not in AMAZON:
            continue
        if str(cell(row, "Promo Rate")).strip():
            continue  # already filled
        current = _rate(cell(row, "Cashback Rate"))
        if current is None:
            continue
        last4 = normalize_last4(str(cell(row, "Card Last 4")))
        if not last4:
            continue
        name, configured = resolve_card(last4, cards, str(cell(row, "Profile")), str(cell(row, "Retailer")), default_rate)
        if not name or configured is None:
            continue  # an unconfigured card: nothing to measure against
        difference = round(current - float(configured), 6)
        if difference <= 1e-9:
            continue  # at (or below) the configured rate: no promo in the cell
        key = (str(cell(row, "Order ID")).strip(), str(cell(row, "Order Date")).strip(),
               str(cell(row, "Item Name")).strip(), str(cell(row, "Shipment")).strip())
        if "cashback_rate" in protected.get(key, ()):
            skipped_hand.append(number)
            continue
        promo = promo_step(difference)
        if promo is None:
            unexplained.append((number, key[0], current, float(configured)))
        else:
            will_write.append((number, promo))
    return {"will_write": will_write, "unexplained": unexplained, "skipped_hand": skipped_hand}


def _print_plan(plan: dict) -> None:
    writes = plan["will_write"]
    if writes:
        print(f"{len(writes)} row(s) get a Promo Rate moved out of their Cashback Rate:")
        for number, promo in writes:
            print(f"  row {number:>4}  promo {promo:.3%}")
    else:
        print("No row needs a Promo Rate filled.")
    if plan["skipped_hand"]:
        print(f"\n{len(plan['skipped_hand'])} row(s) skipped: their Cashback Rate was typed by hand "
              f"(rows {', '.join(str(n) for n in plan['skipped_hand'])}).")
    if plan["unexplained"]:
        print(f"\n{len(plan['unexplained'])} row(s) sit above the configured rate by an amount that is not a "
              "promo step -- an older-era rate, or a hand-typed one. Left alone; check them yourself:")
        for number, order_id, current, configured in plan["unexplained"]:
            print(f"  row {number:>4}  {order_id:<22} cell {current:.3%}  configured {configured:.3%}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Actually write the Promo Rate cells (default: dry run)")
    args = parser.parse_args()

    cards = load_cards()
    if not cards:
        raise SystemExit("config.json has no `cards` section: there is no configured rate to measure a promo against.")
    worksheet = _get_worksheet()
    existing = worksheet.get_values(value_render_option=ValueRenderOption.unformatted)
    if not existing or not any(str(c).strip() for c in existing[0]):
        raise SystemExit("Ledger is empty -- nothing to backfill.")
    header = [str(c) for c in existing[0]]
    if header != list(HEADER):
        raise SystemExit("The ledger's header doesn't match the current schema (the file migrates its own "
                         f"columns on open, so this should not happen).\n  ledger:   {header}\n  expected: {list(HEADER)}")
    from ledger_db.hand_edits import protected_fields

    plan = plan_promo_backfill(header, existing[1:], cards, protected_fields(worksheet))
    _print_plan(plan)
    if not args.apply:
        print("\nDry run only -- nothing written. Re-run with --apply to make these changes.")
        return
    if not plan["will_write"]:
        print("\nNothing to write.")
        return
    column = _col_letter(HEADER.index("Promo Rate"))
    worksheet.batch_update([{"range": f"{column}{n}", "values": [[promo]]} for n, promo in plan["will_write"]],
                           value_input_option=ValueInputOption.raw)
    print(f"\nWrote {len(plan['will_write'])} Promo Rate cell(s). Cashback Rate untouched.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    main()
