"""
One-off: spell every retailer in config.json ONE way -- by name (models/retailers.py: Amazon,
Amazon Business, Best Buy, Costco) -- in the profiles' `retailers` and `auth` keys and in the
cards' `retailer_rates` and `caps`. Every spelling already loads; this
only makes the file read the same everywhere. Secrets are untouched.

DRY RUN BY DEFAULT:

    python -m scripts.standardize_retailers
    python -m scripts.standardize_retailers --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.loader import load_config, save_config  # noqa: E402
from models.retailers import name_of  # noqa: E402


def plan(config: dict) -> list[str]:
    """Rewrite in place; return one line per change."""
    changes: list[str] = []
    for profile in config.get("profiles") or []:
        if not isinstance(profile, dict):
            continue
        label = profile.get("label", "?")
        old = list(profile.get("retailers") or [])
        new = []
        for r in old:
            name = name_of(str(r))
            if name not in new:
                new.append(name)
        if new != old:
            profile["retailers"] = new
            changes.append(f"profile {label}: retailers {old} -> {new}")
        auth = profile.get("auth")
        if isinstance(auth, dict):
            renamed = {name_of(str(k)): v for k, v in auth.items()}
            if list(renamed) != list(auth):
                profile["auth"] = renamed
                changes.append(f"profile {label}: auth keys {list(auth)} -> {list(renamed)}")
    for card in config.get("cards") or []:
        if not isinstance(card, dict):
            continue
        name = card.get("name", "?")
        rates = card.get("retailer_rates")
        if isinstance(rates, dict):
            renamed = {name_of(str(k)): v for k, v in rates.items()}
            if list(renamed) != list(rates):
                card["retailer_rates"] = renamed
                changes.append(f"card {name}: retailer_rates keys {list(rates)} -> {list(renamed)}")
        for cap in card.get("caps") or []:
            if isinstance(cap, dict) and isinstance(cap.get("retailers"), list):
                old = list(cap["retailers"])
                new = [name_of(str(r)) for r in old]
                if new != old:
                    cap["retailers"] = new
                    changes.append(f"card {name}: cap retailers {old} -> {new}")
    return changes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Actually rewrite config.json (default: dry run)")
    args = parser.parse_args()
    config = load_config()
    changes = plan(config)
    if not changes:
        print("Every retailer is already spelled by name. Nothing to change.")
        return
    print(f"{len(changes)} change(s):")
    for line in changes:
        print("  " + line)
    if not args.apply:
        print("\nDry run only -- nothing written. Re-run with --apply to make these changes.")
        return
    save_config(config)
    print("\nconfig.json rewritten.")


if __name__ == "__main__":
    main()
