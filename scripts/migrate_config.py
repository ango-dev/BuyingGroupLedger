"""Fold the old six config sources into one `config.json` (plus `.state.json`).

Setup used to mean authoring `.env`, `profiles.json`, `warehouses.json`, `cards.json`,
`service_account.json` and `.costco/<label>.json`, each with its own format and loader. This reads
all of them and writes the two files that replace them:

    config.json    everything you author        (gitignored)
    .state.json    the rotating Costco tokens   (gitignored, app-written)

DRY RUN BY DEFAULT, matching sort_ledger / reorder_sheet / sync_tracking. It prints the config it
would write with every secret masked, and touches nothing:

    python -m scripts.migrate_config
    python -m scripts.migrate_config --apply

THE ORIGINALS ARE NEVER DELETED. Migrating is meant to be reversible by doing nothing — if the result
looks wrong, delete config.json and the old files are still exactly where they were. Deleting them is
a decision for after a run has proven the new file works, and it is yours to make, not this script's.

The environment -> config mapping comes from `config.settings.ENV_TO_CONFIG`, the same table the
settings resolve through, so a variable can never be migrated into a place settings won't look.
"""

import argparse
import json
import os
from pathlib import Path

from dotenv import dotenv_values

from config.loader import CONFIG_FILE, STATE_FILE
from config.settings import ENV_TO_CONFIG

ROOT = Path(__file__).resolve().parent.parent
LEGACY_PROFILES = ROOT / "profiles.json"
LEGACY_WAREHOUSES = ROOT / "warehouses.json"
LEGACY_CARDS = ROOT / "cards.json"
LEGACY_ENV = ROOT / ".env"
LEGACY_COSTCO_DIR = ROOT / ".costco"

#: Config paths whose value is a credential. Masked in the dry-run print — the whole point of a dry
#: run is to be able to read it, and this file otherwise puts every secret on screen at once.
SECRET_MARKERS = ("key", "secret", "password", "token", "webhook", "par_url", "private")


def _looks_secret(path: str) -> bool:
    return any(marker in path.lower() for marker in SECRET_MARKERS)


def _mask(value) -> str:
    text = str(value)
    return f"{text[:4]}...{text[-4:]} ({len(text)} chars)" if len(text) > 12 else "..."


def _assign(config: dict, path: str, value) -> None:
    """Set a dotted path, creating the intermediate objects."""
    node = config
    *parents, leaf = path.split(".")
    for key in parents:
        node = node.setdefault(key, {})
    node[leaf] = value


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _coerce(value: str):
    """Turn a .env string into the JSON type it obviously is.

    `.env` can only hold strings, but config.json has real types, and `"true"` or `"3"` sitting in a
    JSON file as strings would read as sloppy and invite someone to "fix" one into a type the
    settings then coerce differently. Left alone when ambiguous — notably a rate like "2%", which
    _get_rate understands and float() would mangle.
    """
    text = value.strip()
    if text.lower() in {"true", "false"}:
        return text.lower() == "true"
    if text.endswith("%"):
        return text
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def build_config() -> tuple[dict, dict, list[str]]:
    """Return `(config, state, notes)` built from whatever legacy sources exist."""
    notes: list[str] = []
    config: dict = {
        "//": "One config file. ENVIRONMENT VARIABLES OVERRIDE ANYTHING HERE - see "
              "config.example.json for every key and its variable name.",
    }

    env = ({k: v for k, v in dotenv_values(LEGACY_ENV).items() if v not in (None, "")}
           if LEGACY_ENV.is_file() else {})
    for name, path in ENV_TO_CONFIG.items():
        # The live environment wins over the file, so a value exported in the shell migrates too.
        value = os.getenv(name) or env.get(name)
        if value not in (None, ""):
            _assign(config, path, _coerce(value))
    unmapped = sorted(set(env) - set(ENV_TO_CONFIG))
    if unmapped:
        notes.append(
            f"{len(unmapped)} .env variable(s) have no config home and stay environment-only "
            f"(expected for ops/test hooks): {', '.join(unmapped)}"
        )

    # The Google credential is the one thing Google issues as a whole JSON object, so it is inlined
    # rather than referenced by path — that is what makes config.json self-contained.
    sa_name = (os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
               or env.get("GOOGLE_SERVICE_ACCOUNT_FILE") or "service_account.json")
    sa_path = ROOT / sa_name
    service_account = _read_json(sa_path)
    if isinstance(service_account, dict):
        _assign(config, "google.service_account", service_account)
        # Drop the path setting: it OVERRIDES the inlined block in settings.google_credentials, so
        # migrating both would quietly keep reading the standalone file this just inlined.
        config.get("google", {}).pop("service_account_file", None)
        notes.append(f"inlined {sa_path.name} into google.service_account")
        if os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE") or env.get("GOOGLE_SERVICE_ACCOUNT_FILE"):
            notes.append(
                "WARNING: GOOGLE_SERVICE_ACCOUNT_FILE is set in your environment/.env, and it "
                "OVERRIDES the credential just inlined. Remove that line once you have migrated, or "
                f"config.json only LOOKS self-contained and deleting {sa_path.name} would break the "
                "run."
            )
    else:
        notes.append(f"NO service account found at {sa_path} - set google.service_account by hand")

    for key, path in (("profiles", LEGACY_PROFILES), ("warehouses", LEGACY_WAREHOUSES),
                      ("cards", LEGACY_CARDS)):
        data = _read_json(path)
        if isinstance(data, list):
            config[key] = data
            notes.append(f"{path.name}: {len(data)} entries -> config.json `{key}`")
        else:
            notes.append(f"{path.name} absent - `{key}` left empty")

    state: dict = {}
    if LEGACY_COSTCO_DIR.is_dir():
        for token_file in sorted(LEGACY_COSTCO_DIR.glob("*.json")):
            data = _read_json(token_file)
            if isinstance(data, dict) and data:
                state.setdefault("costco", {})[token_file.stem] = data
                notes.append(f"{token_file.name} -> .state.json costco.{token_file.stem}")
    return config, state, notes


def preview(config: dict, prefix: str = "") -> list[str]:
    """The config as readable lines, with credentials masked."""
    lines: list[str] = []
    for key, value in config.items():
        path = f"{prefix}.{key}" if prefix else key
        if key == "service_account":
            lines.append(f"  {path}: <inlined Google credential, {len(value)} keys>")
        elif isinstance(value, dict):
            lines += preview(value, path)
        elif isinstance(value, list):
            lines.append(f"  {path}: [{len(value)} entries]")
        elif _looks_secret(path):
            lines.append(f"  {path}: {_mask(value)}")
        else:
            lines.append(f"  {path}: {value!r}")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="Actually write config.json and .state.json (default: dry run)")
    args = parser.parse_args()

    config, state, notes = build_config()
    print("Reading the legacy config:")
    for note in notes:
        print(f"  - {note}")
    print("\nconfig.json would contain (secrets masked):")
    for line in preview({k: v for k, v in config.items() if k != "//"}):
        print(line)
    print(f"\n.state.json would contain: {len(state.get('costco', {}))} Costco token set(s)")

    if not args.apply:
        print("\nDry run - nothing written. Re-run with --apply to write the files.")
        print("The old files are never deleted, so this is reversible by deleting config.json.")
        return

    for path, data in ((CONFIG_FILE, config), (STATE_FILE, state)):
        if data:
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            print(f"\nWrote {path}")
    print("\nNow run `python -m scripts.preflight` to check it, then delete the old files by hand "
          "once you're satisfied.")


if __name__ == "__main__":
    main()
