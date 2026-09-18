"""
One-off: fill the Card / Cashback Rate / Total Profit columns on rows ALREADY on the ledger.

Why this exists: those three columns are derived at SCRAPE time (main.run_scrape -> config.cards.
tag_cards, and sheets.ledger_sync stamps the profit formula on the rows it touches). A row that was
already `delivered` or `cancelled` when the columns were introduced is TERMINAL — no future run ever
re-reads it — so it would keep blank cells forever. This is the backfill.

It reproduces exactly what a scrape would have written, from data the sheet already holds:

  - Card + Cashback Rate  <- resolved from that row's recorded Card Last 4, scoped by its Profile and
                             Retailer, through the same config.cards.resolve_card the scrapers use.
  - Total Profit          <- the live formula, stamped on every data row (it reads blank until you
                             fill Actual Payout, so stamping a not-yet-paid row costs nothing).

SAFE BY DEFAULT: only fills cells that are currently BLANK, so a card name you typed by hand is never
overwritten. Pass --refresh to also correct non-blank cells that disagree with cards.json (use that
after editing a rate).

DRY RUN BY DEFAULT — reads the live sheet, writes nothing:
    python -m scripts.backfill_profit_columns

Apply for real:
    python -m scripts.backfill_profit_columns --apply
    python -m scripts.backfill_profit_columns --apply --refresh
"""

import argparse
import logging

from gspread.utils import ValueInputOption, ValueRenderOption

from config.cards import load_cards, resolve_card
from models.card import normalize_last4, parse_rate
from sheets.ledger_sync import HEADER, _col_letter, _get_worksheet, _write_profit_formulas

log = logging.getLogger("backfill_profit_columns")

# The derived columns this script owns, and the OrderItem-ish value each should hold.
CARD_COL = "Card"
RATE_COL = "Cashback Rate"


# How far ABOVE the cards.json rate a cell may sit and still count as correct. A row's rate can
# legitimately exceed it: Amazon's order page advertises a per-order promo ("... plus an extra 1% back
# ...") that config.cards.tag_cards folds into this same cell, because cashback is deliberately one
# summed rate rather than two columns. Without this tolerance `--refresh` would quietly revert every
# promo row to the base rate and check_card_and_rate_coverage would flag them forever.
MAX_PROMO_RATE = 0.10


def _same_rate(sheet_value, resolved) -> bool:
    """Is the sheet's Cashback Rate cell consistent with the resolved rate?

    Compared through parse_rate so a cell that reads back as "4%" counts as equal to 0.04. That
    happens for real: the column is usually percent-FORMATTED, and a formatted read returns the
    DISPLAY text. Without this, every already-correct row would be reported as disagreeing with
    cards.json — and --refresh would rewrite 0.04 on top of 0.04, pure churn.

    A cell ABOVE the resolved rate by up to MAX_PROMO_RATE is also treated as consistent (a folded-in
    Amazon promo). A cell BELOW it, or above it by more than that, is still a real disagreement —
    those are the cases that mean a stale or mis-typed rate.
    """
    if resolved is None:
        return not str(sheet_value).strip()
    try:
        parsed = parse_rate(sheet_value if isinstance(sheet_value, str) else str(sheet_value))
    except ValueError:
        return False
    if parsed is None:
        return False
    delta = parsed - float(resolved)
    return -1e-9 <= delta <= MAX_PROMO_RATE + 1e-9


def plan_profit_backfill(header: list[str], data_rows: list[list[str]], cards,
                         default_rate: float | None = None, refresh: bool = False) -> dict:
    """Read-only: what the backfill would write. Mirrors plan_buying_group_retag's shape.

    Returns {"fills", "changes", "unresolved", "skipped_no_last4", "formula_rows"} where fills/changes
    are (row_number, column_name, old_value, new_value) tuples. `fills` are blank -> value (always
    applied); `changes` are non-blank -> a DIFFERENT value (only applied with refresh=True).
    """
    oid_idx = header.index("Order ID")
    last4_idx = header.index("Card Last 4")
    card_idx = header.index(CARD_COL)
    rate_idx = header.index(RATE_COL)
    profile_idx = header.index("Profile") if "Profile" in header else None
    retailer_idx = header.index("Retailer") if "Retailer" in header else None

    fills, changes, unresolved = [], [], []
    skipped_no_last4 = 0
    formula_rows = []

    for offset, row in enumerate(data_rows):
        row_number = offset + 2  # row 1 is the header

        def cell(i):
            return str(row[i]).strip() if i is not None and i < len(row) else ""

        if not cell(oid_idx):
            continue  # same rule as sync_csv_to_sheet: a blank Order ID isn't a real row
        formula_rows.append(row_number)

        last4 = cell(last4_idx)
        if not normalize_last4(last4):
            skipped_no_last4 += 1
            continue

        name, rate = resolve_card(
            last4, cards, cell(profile_idx), cell(retailer_idx), default_rate
        )
        if not name:
            unresolved.append((row_number, last4))

        for col_idx, col_name, new_value, matches in (
            (card_idx, CARD_COL, name, cell(card_idx) == name),
            (rate_idx, RATE_COL, rate, _same_rate(cell(rate_idx), rate)),
        ):
            old = cell(col_idx)
            if new_value in ("", None) or matches:
                continue
            (fills if not old else changes).append((row_number, col_name, old, new_value))

    return {
        "fills": fills,
        "changes": changes,
        "unresolved": unresolved,
        "skipped_no_last4": skipped_no_last4,
        "formula_rows": formula_rows,
        "will_write": fills + (changes if refresh else []),
    }


def _print_plan(plan: dict, apply: bool, refresh: bool) -> None:
    print(f"Profit-column backfill ({'APPLYING' if apply else 'DRY RUN — nothing will be written'}):\n")
    print(f"  {len(plan['fills'])} blank cell(s) would be FILLED")
    for row_number, col, _old, new in plan["fills"][:12]:
        print(f"    row {row_number:>3}  {col:<14} -> {new}")
    if len(plan["fills"]) > 12:
        print(f"    ... and {len(plan['fills']) - 12} more")

    verb = "CORRECTED" if refresh else "left alone (pass --refresh to correct them)"
    print(f"\n  {len(plan['changes'])} non-blank cell(s) disagree with config.json `cards` — {verb}")
    for row_number, col, old, new in plan["changes"][:12]:
        print(f"    row {row_number:>3}  {col:<14} {old!r} -> {new}")
    if len(plan["changes"]) > 12:
        print(f"    ... and {len(plan['changes']) - 12} more")

    print(f"\n  Total Profit formula would be stamped on {len(plan['formula_rows'])} row(s)")
    if plan["skipped_no_last4"]:
        print(f"  {plan['skipped_no_last4']} row(s) have no Card Last 4 recorded — card columns left blank")
    if plan["unresolved"]:
        cards_seen = sorted({last4 for _r, last4 in plan["unresolved"]})
        print(f"  {len(plan['unresolved'])} row(s) charged a card NOT in config.json `cards` (last 4: "
              f"{cards_seen}) — they get the default rate and no name")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--apply", action="store_true",
                        help="Actually write to the live sheet (default: dry run, read-only)")
    parser.add_argument("--refresh", action="store_true",
                        help="Also correct non-blank cells that disagree with config.json `cards`")
    parser.add_argument("--formulas-only", action="store_true",
                        help="Touch no card cell at all; just re-stamp COGS / Total Profit on every "
                             "row (the migration step after the formula itself changes)")
    args = parser.parse_args()

    cards = load_cards()
    if not cards:
        print("config.json has no `cards` section (or it is empty) — every row would get a blank Card and the "
              "default rate. Configure it there first if that's not what you want.\n")

    worksheet = _get_worksheet()
    # UNFORMATTED so a percent-formatted Cashback Rate comes back as 0.04, not the display text "4%".
    existing = worksheet.get_values(value_render_option=ValueRenderOption.unformatted)
    if not existing or not any(str(c).strip() for c in existing[0]):
        raise SystemExit("Sheet is empty — nothing to backfill.")

    header = [str(c) for c in existing[0]]
    if header != list(HEADER):
        raise SystemExit(
            "Sheet header doesn't match the current schema; run `python -m scripts.reorder_sheet` "
            f"first.\n  sheet:    {header}\n  expected: {list(HEADER)}"
        )

    plan = plan_profit_backfill(header, existing[1:], cards, refresh=args.refresh)
    if args.formulas_only:
        plan["will_write"] = []
        print("--formulas-only: card cells untouched; only the formulas will be re-stamped.\n")
    _print_plan(plan, args.apply, args.refresh)

    if not args.apply:
        print("\nDry run only — nothing written. Re-run with --apply to make these changes.")
        return

    if plan["will_write"]:
        # One batched RAW write: RAW keeps the rate a number and can't reinterpret a card name.
        data = [
            {"range": f"{_col_letter(HEADER.index(col))}{row_number}", "values": [[value]]}
            for row_number, col, _old, value in plan["will_write"]
        ]
        worksheet.batch_update(data, value_input_option=ValueInputOption.raw)
        print(f"\nWrote {len(data)} cell(s).")
    else:
        print("\nNo card cells needed writing.")

    _write_profit_formulas(worksheet, plan["formula_rows"])
    print(f"Stamped the Total Profit formula on {len(plan['formula_rows'])} row(s).")
    print("\nDone.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    main()
