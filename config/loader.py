"""The single config file, and the small state file the app writes for itself.

ONE FILE YOU AUTHOR, ONE THE APP OWNS. Setup used to mean six sources — `.env`, `profiles.json`,
`warehouses.json`, `cards.json`, `service_account.json` and `.costco/<label>.json` — each with its
own format, location and loader, plus four `*.example.*` templates to keep in step. Everything you
write now lives in `config.json`; everything the app rewrites lives in `.state.json`.

WHY THE SPLIT, rather than truly one file. `.state.json` holds the Costco refresh token, which
rotates on EVERY refresh — the app rewrites it several times a day. Folding that into the file you
hand-author would mean the app reformatting your work, and would force the whole config to be mounted
writable in Docker, losing the `:ro` guard that currently protects `profiles.json` and
`service_account.json`. So the rule is: if a human types it, it is config; if the machine replaces
it, it is state.

ENVIRONMENT VARIABLES OVERRIDE THE FILE — always, and from either `.env` or the shell. That is what
keeps `docker-compose.yml`'s `env_file:` and every variable named in DEPLOY.md working unchanged, and
it is what makes a one-off override (`BFMR_MIN_INSURANCE_VALUE=999 python -m sync_tracking`) possible
without editing anything. See config/settings.py for the resolution order.
"""

import json
from pathlib import Path

__all__ = [
    "CONFIG_FILE",
    "STATE_FILE",
    "config_section",
    "config_value",
    "load_config",
    "load_state",
    "reload_config",
    "save_config",
    "save_state",
    "strip_comments",
]

_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = _ROOT / "config.json"
STATE_FILE = _ROOT / ".state.json"

_cache: dict | None = None


def _read_json(path: Path) -> dict:
    """Parse a JSON object, or {} when the file is absent.

    is_file(), not exists(): Docker creates an empty DIRECTORY at a bind-mount path whose host file
    is missing, and reading that raises — taking a whole run down over a file that is allowed to be
    absent. Same reasoning config/warehouses.py has always applied.

    A file that EXISTS but is malformed raises, deliberately. A typo in the one file that holds every
    credential must not silently degrade to "no config", which would look exactly like a fresh
    install and scrape nothing.

    An EMPTY file (nothing but whitespace) is absent, not malformed. A fresh Docker host has to
    `touch` config.json and .state.json BEFORE the first `docker compose up` -- a bind mount whose
    host file is missing becomes a directory -- so the zero-byte stub is exactly what the setup
    wizard finds and fills (DEPLOY.md, "let the dashboard ask"). Nothing a person typed is lost in
    an empty file, so the typo argument does not apply.
    """
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a JSON object, got {type(data).__name__}.")
    return data


def load_config() -> dict:
    """The parsed config.json, read once per process."""
    global _cache
    if _cache is None:
        _cache = _read_json(CONFIG_FILE)
    return _cache


def reload_config() -> None:
    """Drop the cache so the next read re-reads. For tests, and after `save_config`.

    Deliberately does NOT read: a malformed file raises, and an eager read here would make merely
    invalidating the cache fail — including in a test's teardown, where the failure would be
    reported against whatever test ran next rather than the one that wrote the bad file.
    """
    global _cache
    _cache = None


def config_value(path: str, default=None):
    """Read a dotted path (`buying_groups.bfmr.api_key`), or `default` if any hop is missing.

    Missing and blank are treated the same on purpose: a key left as "" in the template means "not
    configured", and callers should get the same answer as if it had been deleted.
    """
    node = load_config()
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    if node is None or (isinstance(node, str) and not node.strip()):
        return default
    return node


def strip_comments(node):
    """Drop every `//`-prefixed key, at any depth, returning a copy.

    JSON has no comments, so the config file uses `"// something": "..."` keys — a convention this
    repo already used in the old warehouses.example.json. They are real keys, which means a pydantic
    model handed one rejects it: `Card.retailer_rates` is typed `dict[str, ...]`, so a comment
    sitting beside the per-retailer rates fails validation rather than being ignored.

    Stripped HERE rather than at load, and on a COPY, because `save_config` writes back exactly what
    `load_config` returned — stripping earlier would silently delete the author's comments the first
    time `create_profile` saved a profile_id.
    """
    if isinstance(node, dict):
        return {k: strip_comments(v) for k, v in node.items() if not str(k).startswith("//")}
    if isinstance(node, list):
        return [strip_comments(v) for v in node]
    return node


def config_section(name: str) -> list:
    """A top-level list section — `profiles`, `warehouses`, `cards` — ready to hand to a model.

    Returns [] when absent so a partly-filled config still runs: no cards means no cashback rates,
    not a crash. A non-list raises, because that is a config mistake rather than an empty one.
    """
    value = load_config().get(name, [])
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError(f"config.json: `{name}` must be a list, got {type(value).__name__}.")
    return strip_comments(value)


def save_config(data: dict) -> None:
    """Write config.json back.

    Only `create_profile` does this, to store the `profile_id` Browser-Use assigns. Callers mutate
    the dict from `load_config()` and pass it back, so every other key — including the `"//"` comment
    keys the template uses — survives: JSON round-trips them as ordinary keys, and Python dicts
    preserve insertion order, so the file comes back in the order you wrote it.
    """
    CONFIG_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    reload_config()


def load_state() -> dict:
    """The app-written state file. Never hand-edited, safe to delete (it is re-earned)."""
    return _read_json(STATE_FILE)


def save_state(data: dict) -> None:
    STATE_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
