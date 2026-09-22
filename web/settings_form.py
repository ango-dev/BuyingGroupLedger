"""The browser Settings page: a form DERIVED from config/settings.py, never a second list.

THE STANDARD. This module does not name a single setting. It reads:

    config.settings.ENV_TO_CONFIG   which settings exist, in what order, and where each lives
    config.settings.Settings        the field each one fills (parsed from the class source), its
                                    Python type, and whether it is a SECRET (`repr=False`)
    config.settings.BOOLEAN_SETTINGS which ones are flags
    config.example.json             the "// key" comment beside each key, shown as help text

So adding a setting the documented way -- the table, a `Settings` field, a key in
config.example.json -- puts it on the page with the right widget and no further work, and
tests/test_web_settings.py fails if any ENV_TO_CONFIG entry does not resolve to a field here. The ONE
thing that must be added by hand is a NEW STRUCTURED SECTION (a list of objects like `profiles`,
`warehouses`, `cards`): those are edited as JSON, each
validated by its own model, and `SECTIONS` below is their list. A new one goes there, with its model.

Saving writes config.json through config.loader.save_config, exactly as scripts/create_profile does,
so the author's "//" comment keys survive. The environment still outranks the file: a setting whose
variable is exported (from .env or the container) is marked on the page, because editing the file
cannot change what the running process sees for it.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from config import loader
from config.loader import config_value, load_config, save_config, strip_comments
from config.settings import BOOLEAN_SETTINGS, ENV_TO_CONFIG, Settings
from models import retailers as retailers_module
from models.card import Card
from models.profile import ProfileConfig
from models.warehouse import Warehouse

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_PATH = ROOT / "config.example.json"

#: `name: type = _get_x("ENV", ...)`, `= field(default=_get_x("ENV"...` or `= (_get_x("ENV"...`.
_DECL = re.compile(
    r'^\s{4}(?P<field>\w+):\s*(?P<type>\w+)\s*=\s*\(?(?:field\(\s*default=)?_get_(?P<getter>\w+)\('
    r'\s*"(?P<env>[A-Z0-9_]+)"',
    re.M,
)

#: Structured sections edited as JSON. (dotted path, "list" | "object", model or None, help)
SECTIONS: tuple[tuple[str, str, Any, str], ...] = (
    ("profiles", "list", ProfileConfig,
     "One Browser-Use browser identity per entry: label, profile_id, proxy, retailers, auth."),
    ("warehouses", "list", Warehouse,
     "Buying-group warehouses and the address jigs that classify a Delivery Address."),
    ("cards", "list", Card,
     "Cards by last 4: name, cashback rate, per-retailer overrides."),
)


#: Sub-groups inside a panel: the Alerts card is one card split into its two channels. The
#: template writes a sub-heading where the group changes; settings without one stand alone.
SUBGROUP_OF_ENV: dict[str, str] = {
    "DISCORD_ALERTS_ENABLED": "Discord", "DISCORD_WEBHOOK_URL": "Discord",
    "GMAIL_ALERTS_ENABLED": "Gmail", "GMAIL_ADDRESS": "Gmail",
    "GMAIL_APP_PASSWORD": "Gmail", "ALERT_EMAIL_TO": "Gmail",
    # The Dashboard panel's sign-in (web/auth.py).
    "WEB_PASSWORD": "Sign-in", "WEB_SESSION_HOURS": "Sign-in", "WEB_REMEMBER_DAYS": "Sign-in",
    "WEB_LOGIN_ATTEMPTS": "Sign-in", "WEB_LOGIN_LOCKOUT_MINUTES": "Sign-in",
}


#: Settings nobody should touch without knowing exactly why.
#: They leave their sections and gather in the Advanced panel at the foot of the page, behind a
#: warning; a section left empty by this (Database) disappears from the page.
ADVANCED_ENVS: frozenset[str] = frozenset({
    "LEDGER_DB_PATH", "WEB_LEDGER_SOURCE", "WEB_SNAPSHOT_PATH", "WEB_LEDGER_CACHE_TTL_SECONDS",
    "WEB_ENABLED", "WEB_BIND_HOST", "WEB_PORT", "RECEIPTS_DIR", "PREFLIGHT_STRICT",
    "BFMR_API_BASE_URL", "MAXOUTDEALS_API_BASE_URL",
})


@dataclass(frozen=True)
class Setting:
    env: str
    path: str
    field: str
    kind: str  # "bool" | "int" | "float" | "rate" | "str"
    secret: bool
    help: str

    @property
    def section(self) -> str:
        return self.path.split(".")[0]

    @property
    def subgroup(self) -> str:
        return SUBGROUP_OF_ENV.get(self.env, "")

    @property
    def advanced(self) -> bool:
        return self.env in ADVANCED_ENVS

    @property
    def key(self) -> str:
        return self.path.split(".", 1)[1] if "." in self.path else self.path


def env_to_field() -> dict[str, tuple[str, str]]:
    """{ENV_NAME: (Settings field name, getter suffix)} parsed from the class source."""
    out: dict[str, tuple[str, str]] = {}
    for match in _DECL.finditer(inspect.getsource(Settings)):
        out.setdefault(match["env"], (match["field"], match["getter"]))
    return out


def code_defaults() -> dict[str, Any]:
    """{ENV_NAME: the default the code applies when neither the file nor the environment sets
    it}, read from the literal second argument of each `_get_x("ENV", default)` in the Settings
    source. The page shows it for a key config.json omits."""
    import ast

    out: dict[str, Any] = {}
    for node in ast.walk(ast.parse(inspect.getsource(Settings))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id.startswith("_get_") and node.args
                and isinstance(node.args[0], ast.Constant)):
            continue
        default: Any = None
        literal = node.args[1] if len(node.args) > 1 else next(
            (kw.value for kw in node.keywords if kw.arg == "default"), None)
        if literal is not None:
            try:
                default = ast.literal_eval(literal)
            except ValueError:
                default = None
        out.setdefault(str(node.args[0].value), default)
    return out


def _example() -> dict:
    try:
        return json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _help_for(example: dict, path: str) -> str:
    node: Any = example
    parts = path.split(".")
    for part in parts[:-1]:
        node = node.get(part, {}) if isinstance(node, dict) else {}
    if not isinstance(node, dict):
        return ""
    # The key's own comment, else the section-level "// " note the example uses for a group of
    # keys that share one explanation (the BFMR headers).
    return str(node.get(f"// {parts[-1]}", "") or node.get("// ", "") or "")


def schema() -> list[Setting]:
    """Every scalar setting, in ENV_TO_CONFIG order. KeyError here = a setting was added to the
    table without a Settings field, which is exactly the drift the tests refuse."""
    fields = {f.name: f for f in dataclasses.fields(Settings)}
    mapping = env_to_field()
    example = _example()
    out = []
    for env, path in ENV_TO_CONFIG.items():
        field_name, getter = mapping[env]
        if env in BOOLEAN_SETTINGS or getter == "bool":
            kind = "bool"
        elif getter in ("int", "float", "rate"):
            kind = getter
        else:
            kind = "str"
        out.append(Setting(env=env, path=path, field=field_name, kind=kind,
                           secret=fields[field_name].repr is False,
                           help=_help_for(example, path)))
    return out


def container_restart_envs() -> set[str]:
    """The settings docker/entrypoint.sh resolves ONCE at container start (the schedule, the
    timezone, whether the dashboard runs) -- derived from scripts.container_settings.EXPORTS, the
    list the entrypoint sources, so a knob added there is prompted for here without a second list."""
    from scripts.container_settings import EXPORTS

    return {name for name, _read in EXPORTS}


def restart_scope(setting: Setting) -> str:
    """What has to happen for a saved value to take effect:
    "container"  docker compose restart (the entrypoint reads it once)
    "dashboard"  the Restart-dashboard button (the web process reads settings once)
    "next run"   nothing -- every scheduled run is a fresh process that reads the file"""
    if setting.env in container_restart_envs():
        return "container"
    if setting.section in ("web", "database"):
        return "dashboard"
    return "next run"


def restart_needed(changes: Mapping[str, Any]) -> str:
    """The strongest restart the changed paths call for: "container" > "dashboard" > ""."""
    by_path = {s.path: s for s in schema()}
    scopes = {restart_scope(by_path[p]) for p in changes if p in by_path}
    if "container" in scopes:
        return "container"
    if "dashboard" in scopes:
        return "dashboard"
    return ""


def sections_in_order(settings: list[Setting]) -> list[str]:
    """The panels in the order the settings come, leaving out a section whose every setting is
    advanced (those sit in the Advanced panel at the foot of the page instead)."""
    seen: list[str] = []
    for s in settings:
        if s.advanced:
            continue
        if s.section not in seen:
            seen.append(s.section)
    return seen


# --------------------------------------------------------------------------------------------------
# Current values (what the page shows)
# --------------------------------------------------------------------------------------------------


def current_value(setting: Setting):
    """The FILE's value for the page. Secrets are never returned -- only whether one is set."""
    value = config_value(setting.path)
    if setting.secret:
        return None
    return value


def is_overridden(setting: Setting, environ: Mapping[str, str]) -> bool:
    return bool((environ.get(setting.env) or "").strip())


def view(settings: list[Setting], environ: Mapping[str, str]) -> list[dict]:
    """One row per setting: the FILE's value, or -- when the file omits the key -- the code's
    default, marked `defaulted` so the page can say so (a flag that defaults to true reads as
    ticked, not blank). No setting borrows another's value (the combined-package account used to
    show the alerts account as its placeholder; that link is gone, 2026-09-18)."""
    defaults = code_defaults()
    rows = []
    for s in settings:
        value = current_value(s)
        defaulted = value is None and not s.secret and defaults.get(s.env) not in (None, "")
        if defaulted:
            value = defaults[s.env]
        stored = config_value(s.path)
        rows.append({
            "setting": s,
            "value": "" if value is None else value,
            "secret_set": bool(stored) if s.secret else False,
            "overridden": is_overridden(s, environ),
            "restart": restart_scope(s),
            "defaulted": defaulted,
        })
    return rows


def shape_of(path: str) -> str:
    """"list" or "object" for a structured section (list when unknown)."""
    return next((spec[1] for spec in SECTIONS if spec[0] == path), "list")


def section_text(path: str) -> str:
    """A structured section as pretty JSON for its textarea ("[]" / "{}" when absent)."""
    value = config_value(path)
    if value in (None, ""):
        value = {} if shape_of(path) == "object" else []
    return json.dumps(value, indent=2)


# --------------------------------------------------------------------------------------------------
# Applying a submitted form
# --------------------------------------------------------------------------------------------------


class SettingsError(ValueError):
    """One or more fields did not validate; `.errors` lists them, nothing was written."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _set_path(data: dict, path: str, value) -> None:
    node = data
    parts = path.split(".")
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _validate_frequency(text: str) -> str:
    from scripts.backup import FREQUENCIES

    if not text:
        return ""  # the code default (daily) applies
    if text.lower() not in FREQUENCIES:
        raise ValueError("one of daily, weekly, monthly")
    return text.lower()


def _validate_time(text: str) -> str:
    from scripts.backup import parse_time

    if not text:
        return ""  # the code default (03:30) applies
    hour, minute = parse_time(text)
    return f"{hour:02d}:{minute:02d}"


def _validate_days(text: str) -> str:
    from scripts.backup import parse_days_any

    return parse_days_any(text)


def _validate_number(text: str, *, integer: bool, least: float, what: str):
    """A sign-in length or count: blank keeps the default, otherwise a number of at least
    `least` (a zero-hour session or a zero-attempt lock would sign everyone out for good)."""
    if not text:
        return ""
    try:
        value = int(text) if integer else float(text)
    except ValueError as exc:
        raise ValueError(f"not a number: {text!r}") from exc
    if value < least:
        raise ValueError(f"{what} must be at least {least:g}")
    return value


def _validate_session_hours(text):
    return _validate_number(text, integer=False, least=0.05, what="a sign-in")


def _validate_remember_days(text):
    return _validate_number(text, integer=False, least=1 / 24, what="remember me")


def _validate_attempts(text):
    return _validate_number(text, integer=True, least=1, what="the attempts before a lock")


def _validate_lockout(text):
    return _validate_number(text, integer=False, least=0, what="the lock")


#: Settings with a vocabulary or a floor of their own, checked before anything is written.
_VALIDATORS = {"BACKUP_FREQUENCY": _validate_frequency, "BACKUP_TIME": _validate_time,
               "BACKUP_DAYS": _validate_days,
               "WEB_SESSION_HOURS": _validate_session_hours, "WEB_REMEMBER_DAYS": _validate_remember_days,
               "WEB_LOGIN_ATTEMPTS": _validate_attempts, "WEB_LOGIN_LOCKOUT_MINUTES": _validate_lockout}


#: Whole-number settings with a range of their own: the entrypoint falls back to 6 on an interval
#: outside 1..23 (stderr only), and a keep of 0 would prune every backup.
_BOUNDS: dict[str, tuple[int, int | None]] = {"RUN_INTERVAL_HOURS": (1, 23), "BACKUP_KEEP": (1, None)}


def _parse(setting: Setting, raw: str) -> Any:
    """The typed text as the value to store. The messages are the page's: a person reads them
    under the field, so "invalid literal for int() with base 10" is not one of them."""
    text = (raw or "").strip()
    if setting.env in _VALIDATORS:
        return _VALIDATORS[setting.env](text)
    if setting.kind == "int":
        if not text:
            return ""
        try:
            value = int(text)
        except ValueError:
            raise ValueError(f"{text!r} is not a whole number") from None
        low, high = _BOUNDS.get(setting.env, (None, None))
        if low is not None and value < low:
            raise ValueError(f"must be at least {low}")
        if high is not None and value > high:
            raise ValueError(f"must be between {low} and {high}")
        return value
    if setting.kind == "float":
        if not text:
            return ""
        try:
            return float(text)
        except ValueError:
            raise ValueError(f"{text!r} is not a number") from None
    if setting.kind == "rate":
        if not text:
            return ""
        try:
            rate = float(text[:-1].strip()) / 100 if text.endswith("%") else float(text)
        except ValueError:
            raise ValueError(f"{text!r} is not a rate: write 2% or 0.02") from None
        if not 0 <= rate <= 1:
            raise ValueError("outside 0-1 (write 2% as 0.02 or \"2%\")")
        return text if text.endswith("%") else rate
    return text


def humanise_errors(exc: Exception, where: str) -> list[str]:
    """pydantic's report as lines a person can act on: the field's name, the message without the
    "Value error, " prefix, and none of the `[type=..., input_value=...] For further information
    visit https://errors.pydantic.dev/...` tail."""
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return [f"{where}: {exc}"]
    out = []
    for e in errors():
        loc = " › ".join(str(p).replace("_", " ") for p in (e.get("loc") or ()) if not isinstance(p, int))
        msg = str(e.get("msg") or "")
        for prefix in ("Value error, ", "Assertion failed, "):
            if msg.startswith(prefix):
                msg = msg[len(prefix):]
        # Python's own parse messages, as a person would say them
        msg = re.sub(r"could not convert string to float: (.+)", r"\1 is not a number", msg)
        msg = re.sub(r"invalid literal for int\(\) with base 10: (.+)", r"\1 is not a whole number", msg)
        out.append(f"{where}: {loc + ': ' if loc else ''}{msg}")
    return out or [f"{where}: {exc}"]


def apply_scalars(form: Mapping[str, str], settings: list[Setting] | None = None,
                  skip: set[str] | None = None) -> dict:
    """Write every scalar setting from the form into config.json. Checkboxes: absent = false.
    Secrets: blank keeps the stored value; `<ENV>__clear` = "1" blanks it. `skip` names the
    settings the page did not render (hidden_envs): those keep their file value, since "absent
    from the form" would otherwise read as blank / unticked. Returns {path: value} for what
    changed. Raises SettingsError (nothing written) when any value fails to parse."""
    settings = [s for s in (settings or schema()) if s.env not in (skip or set())]
    defaults = code_defaults()
    data = load_config()
    changes: dict[str, Any] = {}
    errors: list[str] = []
    for s in settings:
        before = config_value(s.path)
        if before is None and not s.secret and defaults.get(s.env) not in (None, ""):
            before = defaults[s.env]  # what the page showed: a key the file omits reads as its default
        if s.kind == "bool":
            value = str(form.get(s.env, "")).strip().lower() in ("on", "true", "1", "yes")
        elif s.secret:
            if str(form.get(f"{s.env}__clear", "")).strip() in ("1", "on", "true"):
                value = ""
            else:
                raw = str(form.get(s.env, "") or "")
                if not raw.strip():
                    continue  # blank keeps what is stored
                value = raw.strip()
        else:
            try:
                value = _parse(s, str(form.get(s.env, "") or ""))
            except ValueError as exc:
                errors.append(f"{s.env}: {exc}")
                continue
        if value != (before if before is not None else ("" if not s.kind == "bool" else False)):
            changes[s.path] = value
        _set_path(data, s.path, value)
    if errors:
        loader.reload_config()  # discard the mutated cache
        raise SettingsError(errors)
    save_config(data)
    return changes


def apply_section(path: str, text: str) -> int:
    """Replace one structured section from its JSON text, validated entry by entry with the
    section's own model. Returns the entry count. Raises SettingsError, nothing written."""
    spec = next((s for s in SECTIONS if s[0] == path), None)
    if spec is None:
        raise SettingsError([f"unknown section {path!r}"])
    _, shape, model, _ = spec
    try:
        value = json.loads(text or ("{}" if shape == "object" else "[]"))
    except ValueError as exc:
        raise SettingsError([f"{path}: not valid JSON -- {exc}"]) from exc
    if shape == "list":
        if not isinstance(value, list):
            raise SettingsError([f"{path}: must be a JSON list"])
        errors = []
        for index, entry in enumerate(value):
            try:
                model.model_validate(strip_comments(entry))
            except Exception as exc:  # noqa: BLE001 -- pydantic's message is the useful part
                errors.append(f"{path}[{index}]: {exc}")
        if errors:
            raise SettingsError(errors)
    elif not isinstance(value, dict):
        raise SettingsError([f"{path}: must be a JSON object"])
    data = load_config()
    _set_path(data, path, value)
    save_config(data)
    return len(value)


# --------------------------------------------------------------------------------------------------
# Presentation: section titles and field labels (a fallback derives both from the key)
# --------------------------------------------------------------------------------------------------

#: Friendly titles and one-line blurbs for the page's panels. A section missing here still renders,
#: titled from its key -- this is presentation only, never a second list of settings.
SECTION_TITLES: dict[str, tuple[str, str]] = {
    # One clause each; the per-setting help under each field carries the detail.
    "browser_use": ("Browser-Use", "The cloud browser Best Buy and the Amazons drive."),
    "container": ("Schedule", "How often the container runs. Read once at container start."),
    "scraping": ("Scraping", "How far back each run looks, and the money rules the ledger applies."),
    "alerts": ("Alerts", "Where failures are reported: a Discord webhook and a Gmail account, each with "
               "its own switch."),
    "buying_groups": ("Buying groups", "BFMR and MaxOutDeals: API access, insurance and the auto-reply."),
    "receipts": ("Receipts", "Receipt capture: each order's proof of purchase, kept beside the ledger."),
    "web": ("Dashboard", "This dashboard: the address alerts link to, the heartbeat threshold and the "
            "sign-in. Read once at dashboard start."),
    "database": ("Database", "The SQLite file that is the ledger."),
    "advanced": ("Advanced", "Paths, backends, bind address and API hosts. A wrong value here shows an "
                 "empty ledger or takes the dashboard off the network."),
    "backups": ("Backups", "Scheduled backups into backups/, and how many to keep. Read once at "
                "container start."),
    "profiles": ("Profiles", "One browser identity per entry: its proxy, retailers and unattended sign-in."),
    "warehouses": ("Warehouses", "Each buying group and the address jigs that route an order to it."),
    "cards": ("Cards", "Cards by last 4, with the cashback rate the profit formula nets from COGS."),
}


def hidden_envs() -> set[str]:
    """The settings the page does not render (and a save must leave alone). Nothing, since
    2026-09-18; kept as the one place to hide a
    setting from the page should one need it."""
    return set()


def section_title(section: str) -> tuple[str, str]:
    return SECTION_TITLES.get(section, (section.replace("_", " ").capitalize(), ""))


def field_label(setting: Setting) -> tuple[str, str]:
    """("Lookback days", "") for scraping.lookback_days; ("Api key", "bfmr") for
    buying_groups.bfmr.api_key -- the last part humanised, the sub-group beside it."""
    parts = setting.key.split(".")
    return parts[-1].replace("_", " ").capitalize(), ".".join(parts[:-1])


# --------------------------------------------------------------------------------------------------
# Entry cards: profiles / warehouses / cards edited one entry at a time. Each card is a form; a submitted card is
# rebuilt from its fields ON TOP of the stored entry, so comment keys and anything the form does
# not show survive, validated by the section's model, and written through save_config like
# everything else. Secrets (proxy and sign-in passwords, TOTP seeds) are never rendered: blank
# keeps the stored value, a "clear" box blanks it.
# --------------------------------------------------------------------------------------------------

#: The retailer keys a profile can be logged into (scripts.preflight is the authority).
from scripts.preflight import KNOWN_RETAILER_KEYS as RETAILER_KEYS  # noqa: E402

#: The retailers with an unattended sign-in (models.profile.RetailerAuth's docstring).
AUTH_RETAILERS = ("bestbuy", "amazon-business")
#: The sections that have entry cards (the rest of SECTIONS stay JSON-only).
CARD_SECTIONS = ("profiles", "warehouses", "cards")


def _entries(path: str) -> list:
    value = config_value(path)
    return list(value) if isinstance(value, list) else []


def _is_set(value) -> bool:
    return bool(str(value or "").strip())


def display_entries(path: str, today: str | None = None) -> list[dict]:
    """What the cards show: the stored entries with every secret replaced by whether it is set."""
    out = []
    for index, raw in enumerate(_entries(path)):
        entry = strip_comments(raw) if isinstance(raw, dict) else {}
        if not isinstance(entry, dict):
            continue
        out.append(_display_one(path, index, entry, today))
    return out


def draft_entry(path: str, form: Mapping[str, str], today: str | None = None) -> dict:
    """A refused add card's fields as typed, in the shape the card macros render, so a 400 keeps
    what the person wrote instead of a blank card. Built by the
    same builder the save uses; a value the builder cannot read is shown as typed."""
    try:
        entry = _card_from_form(form, {}, today) if path == "cards" else _BUILDERS[path](form, {})
    except Exception:  # noqa: BLE001 -- the builder's refusal is already on the page
        entry = {"label": _text(form, "label"), "buying_group": _text(form, "buying_group"),
                 "last4": _text(form, "last4"), "name": _text(form, "name"),
                 "cashback_rate": _text(form, "cashback_rate"), "virtual_of": _text(form, "virtual_of")}
    return _display_one(path, None, entry, today)


def _display_one(path: str, index: int | None, entry: dict, today: str | None = None) -> dict:
    """One entry as its card shows it (see display_entries)."""
    if path == "profiles":
        proxy = entry.get("proxy") or {}
        auth = entry.get("auth") or {}
        return {
            "index": index, "label": entry.get("label", ""),
            "profile_id": entry.get("profile_id", ""),
            "retailers": [retailers_module.key_of(str(r)) for r in (entry.get("retailers") or [])],  # keys: the boxes compare them
            "proxy": {"host": proxy.get("host", ""), "port": proxy.get("port", ""),
                      "username": proxy.get("username", ""),
                      "password_set": _is_set(proxy.get("password")),
                      "enabled": proxy.get("enabled", True) is not False} if proxy else None,
            "auth": [{"retailer": retailers_module.key_of(str(key)), "username": (a or {}).get("username", ""),
                      "password_set": _is_set((a or {}).get("password")),
                      "totp_set": _is_set((a or {}).get("totp_secret"))}
                     for key, a in auth.items()],
        }
    if path == "warehouses":
        return {
            "index": index, "buying_group": entry.get("buying_group", ""),
            "jigs": [{"label": j.get("label", ""), "street": j.get("street", ""),
                      "zip": j.get("zip", ""), "name_contains": j.get("name_contains", ""),
                      "contains": ", ".join(j.get("contains") or [])}
                     for j in (entry.get("jigs") or []) if isinstance(j, dict)],
        }
    caps = [c for c in (entry.get("caps") or []) if isinstance(c, dict)]
    return {
        "index": index, "last4": entry.get("last4", ""), "name": entry.get("name", ""),
        "cashback_rate": _rate_text(entry.get("cashback_rate", "")),
        "profile": entry.get("profile", ""),
        "virtual": bool(entry.get("virtual")) or bool(entry.get("virtual_of")),
        "virtual_of": str(entry.get("virtual_of") or ""),
        "own_bonus": bool(entry.get("own_bonus")),
        "retailer_rates": [(r, _rate_text(v)) for r, v in (entry.get("retailer_rates") or {}).items()],
        "caps": [_cap_display(c, today) for c in caps],
        "rate_rows": _rate_rows(entry.get("retailer_rates") or {}, caps, today),
        "cap_all": next(({**_cap_display(c, today), "cap_index": j} for j, c in enumerate(caps) if not c.get("retailers")), _cap_display({}, today)),
    }


def _rate_rows(retailer_rates: dict, caps: list, today: str | None = None) -> list[dict]:
    """The card's rates by retailer as ONE table: a row per retailer cap (its retailers share the row, the
    rate and the allowance), then a row per retailer rate without a cap."""
    from models.card import normalize_retailer

    by_key = {normalize_retailer(str(r)): (r, _rate_text(rate)) for r, rate in retailer_rates.items()}
    rows, covered = [], set()
    for cap_index, cap in enumerate(caps):
        retailers = cap.get("retailers") or []
        if isinstance(retailers, str):
            retailers = [retailers]
        if not retailers:
            continue  # the catch-all rides on the "everywhere else" row
        keys = [normalize_retailer(str(r)) for r in retailers]
        rate = next((by_key[k][1] for k in keys if k in by_key), "")
        # the form's tick values are retailer_keys (models/retailers.py); the chips show the names
        rows.append({**_cap_display(cap, today), "cap_index": cap_index,
                     "retailers": ", ".join(retailers_module.key_of(str(r)) for r in retailers), "rate": rate})
        covered.update(keys)
    # retailers without a cap that share a rate share a row; the file stores a flat dict
    grouped: dict = {}
    for key, (retailer, rate) in by_key.items():
        if key not in covered:
            grouped.setdefault(str(rate), (rate, []))[1].append(retailers_module.key_of(str(retailer)))
    for rate, keys in grouped.values():  # the rate as stored ("amazon-business" is not by_key's key)
        rows.append({**_cap_display({}, today), "retailers": ", ".join(keys), "rate": rate})
    return rows


def _cap_display(cap: dict, today: str | None = None) -> dict:
    """A CashbackCap entry as the card's form shows it (models.card.CashbackCap): the outside spend
    as the dated log's rows plus the current period's total and bounds (`today` says which)."""
    from datetime import date

    from models import amount_log
    from models.card import CashbackCap

    resets = str(cap.get("resets") or "calendar-year")
    anniversary = resets if resets not in ("calendar-year", "never") else ""
    retailers = cap.get("retailers") or []
    if isinstance(retailers, str):
        retailers = [retailers]
    today = today or date.today().isoformat()
    try:  # the model reads every shape the file may hold and knows the period; a cap the model
        # refuses (a typo in config.json) still shows its log, undated by period
        fields = {k: v for k, v in cap.items() if k in ("retailers", "spend_limit", "fallback_rate", "resets", "outside_spend")}
        model = CashbackCap(**{**fields, "spend_limit": fields.get("spend_limit") or 1})  # a row without a limit still has its period
        entries, bounds = model.outside_spend, model.period_bounds(today)
        current = model.outside_for(model.period_of(today))
    except Exception:  # noqa: BLE001
        try:
            entries = amount_log.coerce(cap.get("outside_spend"), "1970-01-01")
        except ValueError:
            entries = []
        bounds, current = None, amount_log.total(entries)

    def plain(value):  # 150000.0 shows as 150000
        return int(value) if isinstance(value, float) and value.is_integer() else value

    return {
        "retailers": ", ".join(str(r) for r in retailers),
        "spend_limit": plain(cap.get("spend_limit", "")),
        "fallback_rate": _rate_text(cap.get("fallback_rate", "")),
        "resets": "anniversary" if anniversary else resets,
        "anniversary": anniversary,
        "outside_entries": amount_log.display(entries),
        "outside_total": current,
        "period_start": bounds[0] if bounds else "",
        "period_end": bounds[1] if bounds else "",
    }


def profile_labels() -> list[str]:
    return [str(e.get("label", "")) for e in _entries("profiles") if isinstance(e, dict)]


def _text(form: Mapping[str, str], name: str) -> str:
    return str(form.get(name, "") or "").strip()


def _secret(form: Mapping[str, str], name: str, stored) -> str:
    """Blank keeps `stored`; `<name>__clear` blanks it; anything typed replaces it."""
    if _text(form, f"{name}__clear") in ("1", "on", "true"):
        return ""
    typed = str(form.get(name, "") or "")
    return typed.strip() if typed.strip() else str(stored or "")


def _rate_text(value) -> str:
    """A stored rate as the page shows it: a percent, however the file spells it. Anything the
    normaliser cannot read is shown as stored."""
    if value is None or value == "":
        return ""
    shown = _rate_value(str(value))
    return shown if isinstance(shown, str) else str(value)


def _rate_value(text: str):
    """A rate as typed, written as a PERCENT: "2%" stays "2%", a fraction up to 1 becomes its percent ("0.015" -> "1.5%"),
    and anything else is handed on as typed for the model to refuse (a bare "2" is ambiguous)."""
    text = text.strip()
    if not text:
        return None
    if text.endswith("%"):
        try:
            return f"{float(text[:-1].strip()):g}%"
        except ValueError:
            return text
    try:
        value = float(text)
    except ValueError:
        return text
    if 0 <= value <= 1:
        return f"{value * 100:g}%"
    return value  # the model says why a bare number above 1 is refused


def _indexed(form: Mapping[str, str], prefix: str) -> list[int]:
    """The row indexes a form carries for `prefix` ("jig" -> jig.0.*, jig.1.*), in order."""
    seen: set[int] = set()
    for key in form.keys():
        if key.startswith(prefix + "."):
            head = key[len(prefix) + 1:].split(".", 1)[0]
            if head.isdigit():
                seen.add(int(head))
    return sorted(seen)


def _profile_from_form(form: Mapping[str, str], base: dict) -> dict:
    entry = dict(base)
    entry["label"] = _text(form, "label")
    entry["profile_id"] = _text(form, "profile_id")
    chosen = form.getlist("retailers") if hasattr(form, "getlist") else form.get("retailers", [])
    if isinstance(chosen, str):
        chosen = [chosen]
    chosen = {retailers_module.key_of(str(c)) for c in chosen}
    entry["retailers"] = [retailers_module.name_of(r) for r in RETAILER_KEYS if r in chosen]  # written by name (models/retailers.py)
    old_proxy = base.get("proxy") or {}
    host = _text(form, "proxy_host")
    if host:
        proxy = dict(old_proxy)
        proxy["host"] = host
        port = _text(form, "proxy_port")
        proxy["port"] = int(port) if port.isdigit() else port
        proxy["username"] = _text(form, "proxy_username")
        proxy["password"] = _secret(form, "proxy_password", old_proxy.get("password"))
        if "proxy_form" in form:  # the page's form carries the "in use" box (unticked = absent); a hand-made post keeps the stored state
            if str(form.get("proxy_enabled", "")).strip().lower() in ("on", "true", "1", "yes"):
                proxy.pop("enabled", None)
            else:
                proxy["enabled"] = False
        entry["proxy"] = proxy
    else:
        entry.pop("proxy", None)
    # the auth keys are written by name too; the same retailer under two spellings is one entry
    auth = {retailers_module.name_of(k): a for k, a in (base.get("auth") or {}).items()}
    for retailer in list(auth):
        if _text(form, f"auth.{retailers_module.key_of(retailer)}.__remove") in ("1", "on", "true"):
            auth.pop(retailer)
            continue
        key = retailers_module.key_of(retailer)
        if f"auth.{key}.username" not in form:
            continue  # not on the form (a key the page does not know): kept as stored
        old = dict(auth.get(retailer) or {})
        old["method"] = old.get("method") or "password"
        old["username"] = _text(form, f"auth.{key}.username")
        old["password"] = _secret(form, f"auth.{key}.password", old.get("password"))
        old["totp_secret"] = _secret(form, f"auth.{key}.totp_secret", old.get("totp_secret"))
        auth[retailer] = old
    new_retailer = retailers_module.name_of(_text(form, "auth_new_retailer")) if _text(form, "auth_new_retailer") else ""
    if new_retailer and new_retailer not in auth:
        auth[new_retailer] = {"method": "password", "username": _text(form, "auth_new_username"),
                              "password": _text(form, "auth_new_password"),
                              "totp_secret": _text(form, "auth_new_totp_secret")}
    if auth:
        entry["auth"] = auth
    else:
        entry.pop("auth", None)
    return entry


def _warehouse_from_form(form: Mapping[str, str], base: dict) -> dict:
    entry = dict(base)
    entry["buying_group"] = _text(form, "buying_group")
    jigs = []
    for i in _indexed(form, "jig"):
        if _text(form, f"jig.{i}.__remove") in ("1", "on", "true"):
            continue
        jig = {"label": _text(form, f"jig.{i}.label"), "street": _text(form, f"jig.{i}.street"),
               "zip": _text(form, f"jig.{i}.zip"),
               "name_contains": _text(form, f"jig.{i}.name_contains"),
               "contains": [c.strip() for c in _text(form, f"jig.{i}.contains").split(",")
                            if c.strip()]}
        if not any([jig["label"], jig["street"], jig["zip"], jig["name_contains"], jig["contains"]]):
            continue  # the blank "new jig" row
        jigs.append({k: v for k, v in jig.items() if v not in ("", [])})
    entry["jigs"] = jigs
    return entry


def _card_from_form(form: Mapping[str, str], base: dict, today: str | None = None) -> dict:
    entry = dict(base)
    entry["last4"] = _text(form, "last4")
    entry["name"] = _text(form, "name")
    rate = _rate_value(_text(form, "cashback_rate"))
    if rate is None:
        entry.pop("cashback_rate", None)
    else:
        entry["cashback_rate"] = rate
    profile = _text(form, "profile")
    if profile:
        entry["profile"] = profile
    else:
        entry.pop("profile", None)
    # the card type: `kind` (regular / virtual / employee) from the page; the older `virtual` and
    # `own_bonus` ticks are still understood
    kind = _text(form, "kind").lower()
    virtual = kind in ("virtual", "employee") or str(form.get("virtual", "")).strip().lower() in ("on", "true", "1", "yes")
    employee = kind == "employee" or str(form.get("own_bonus", "")).strip().lower() in ("on", "true", "1", "yes")
    virtual_of = _text(form, "virtual_of")
    if virtual or virtual_of:
        if not virtual_of:  # a virtual card must say which card it is a number of
            raise SettingsError(["A virtual card must name the card it is a virtual number of (its spend counts "
                                 "against that card's caps): pick it under \"Shares limits with\"."])
        parent = next((e for e in _entries("cards") if isinstance(e, dict)
                       and str(e.get("last4", "")) == virtual_of), None)
        if parent is None:
            raise SettingsError([f"No card ends in {virtual_of}: pick the card this number belongs to under "
                                 "\"Shares limits with\" (add that card first)."])
        if parent.get("virtual") or parent.get("virtual_of"):
            raise SettingsError([f"{parent.get('name', virtual_of)} …{virtual_of} is itself a virtual number: a virtual "
                                 "card belongs to a real card."])
        entry["virtual"] = True
        entry["virtual_of"] = virtual_of
        if employee:
            entry["own_bonus"] = True  # an employee card: the Taxes page still asks for its bonus
        else:
            entry.pop("own_bonus", None)
        # a virtual number shares the card's rates and limits: nothing of its own is kept
        for key in ("cashback_rate", "retailer_rates", "caps"):
            entry.pop(key, None)
        return entry
    else:
        entry.pop("virtual", None)
        entry.pop("virtual_of", None)
        entry.pop("own_bonus", None)
    # one table of rates and caps: a row's retailers share its rate, and its
    # allowance when it has a spend limit; the "everywhere else" row carries the catch-all cap
    rates: dict = {}
    caps: list = []
    for i in _indexed(form, "rr"):
        names = [retailers_module.name_of(r) for r in _many(form, f"rr.{i}.retailers")]  # written by name
        if not names:
            continue  # the blank "new" row, or a row being dropped
        value = _rate_value(_text(form, f"rr.{i}.rate"))
        if value is not None:
            for r in names:
                rates[r] = value
        cap = _cap_from_form(form, f"rr.{i}", names, today)
        if cap:
            caps.append(cap)
    catch_all = _cap_from_form(form, "cap_all", [], today)
    if catch_all:
        caps.append(catch_all)
    if rates:
        entry["retailer_rates"] = rates
    else:
        entry.pop("retailer_rates", None)
    if caps:
        entry["caps"] = caps
    else:
        entry.pop("caps", None)
    return entry


def _many(form: Mapping[str, str], name: str) -> list[str]:
    """Every value a field carries: the dropdown's ticks (one value each), or a comma-separated
    string from a hand-made post; blanks and repeats dropped."""
    raw = form.getlist(name) if hasattr(form, "getlist") else form.get(name, [])
    if isinstance(raw, str):
        raw = [raw]
    out: list[str] = []
    for value in raw or []:
        for part in str(value).split(","):
            if part.strip() and part.strip() not in out:
                out.append(part.strip())
    return out


def _cap_from_form(form: Mapping[str, str], prefix: str, retailers: list, today: str | None = None) -> dict | None:
    """A CashbackCap from a table row's fields, or None when the row has no spend limit."""
    limit = _text(form, f"{prefix}.spend_limit")
    if not limit:
        return None
    resets = _text(form, f"{prefix}.resets") or "calendar-year"
    if resets == "anniversary":
        resets = _text(form, f"{prefix}.anniversary")
    cap = {"retailers": list(retailers), "spend_limit": _money_value(limit),
           "fallback_rate": _rate_value(_text(form, f"{prefix}.fallback_rate")), "resets": resets}
    from models import amount_log

    if amount_log.has_fields(form, f"{prefix}.os"):  # the dated log's rows
        entries = _log_rows(form, f"{prefix}.os", today)
        if entries:
            cap["outside_spend"] = entries
    else:  # the older one-line form, "2026: 4,000, 2027: 0": the model dates each period at its start
        offsets = {}
        for part in re.split(r"[;,](?=\s*[^,;:]+:)", _text(form, f"{prefix}.outside_spend")):
            if ":" in part:
                period, amount = part.split(":", 1)
                if period.strip() and amount.strip():
                    offsets[period.strip()] = _money_value(amount)
        if offsets:  # stored as the dated log (the model dates each period); a refused cap keeps the text for its message
            try:
                from models.card import CashbackCap

                cap["outside_spend"] = CashbackCap(**{**cap, "outside_spend": offsets}).outside_spend
            except Exception:  # noqa: BLE001
                cap["outside_spend"] = offsets
    if cap["fallback_rate"] is None:
        cap.pop("fallback_rate")  # blank: the card's everywhere-else rate applies past the limit
    return cap


def _log_rows(form: Mapping[str, str], prefix: str, today: str | None = None) -> list[dict]:
    """The dated log's rows as posted; a bad amount is left as typed for the model to refuse by name."""
    from models import amount_log

    try:
        return amount_log.parse(form, prefix, today=today)
    except ValueError as exc:
        raise SettingsError([str(exc)]) from None


def _money_value(text: str):
    """An amount as typed: "150,000" or "$150000" -> 150000.0; anything else stays text for the
    model to refuse with its own message."""
    cleaned = text.strip().replace("$", "").replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return text.strip()


_BUILDERS = {"profiles": _profile_from_form, "warehouses": _warehouse_from_form,
             "cards": _card_from_form}

#: What names an entry of each section: the field, its label on the page, and how the file
#: matches on it. A blank one used to save (the browser's `required` was the only guard), and a
#: twin used to save beside the first and shadow it.
_IDENTITY = {"profiles": ("label", "Label"), "warehouses": ("buying_group", "Buying group"), "cards": ("last4", "Last 4")}


def _check_identity(path: str, index: int | None, entry: dict, entries: list) -> None:
    field, label = _IDENTITY[path]
    value = str(entry.get(field, "") or "").strip()
    if not value:
        raise SettingsError([f"{label} is required."])
    if path == "cards":
        if not (value.isdigit() and len(value) == 4):
            raise SettingsError([f"{label} must be the card's four digits, got {value!r}."])
        if not str(entry.get("name", "") or "").strip():
            raise SettingsError(["Name is required."])
    for i, other in enumerate(entries):
        if i == index or not isinstance(other, dict):
            continue
        if str(other.get(field, "") or "").strip().lower() == value.lower():
            noun = {"profiles": "profile", "warehouses": "buying group", "cards": "card"}[path]
            raise SettingsError([f"A {noun} {'ending in' if path == 'cards' else 'named'} {value!r} already exists "
                                 f"(entry {i + 1}): edit that one, or pick another {label.lower()}."])


def entry_key(path: str, entry: dict) -> str:
    """The key a card on the page carries (data-key): "profiles:<label>", "warehouses:<group>", "cards:<last4>"."""
    entry = entry if isinstance(entry, dict) else {}
    field = {"profiles": "label", "warehouses": "buying_group"}.get(path, "last4")
    return f"{path}:{entry.get(field, '')}"


def entry_label(path: str, entry: dict) -> str:
    entry = entry if isinstance(entry, dict) else {}
    if path == "profiles":
        return str(entry.get("label") or "(unlabelled profile)")
    if path == "warehouses":
        return str(entry.get("buying_group") or "(unnamed group)")
    name = entry.get("name") or "(unnamed card)"
    return f"{name} \u2026{entry.get('last4', '')}" if entry.get("last4") else str(name)


def apply_entry(path: str, index: int | None, form: Mapping[str, str], today: str | None = None) -> tuple[int, str]:
    """Add (index None) or replace one entry of a card section from its form. Returns the index
    it landed at and its label. Raises SettingsError, nothing written."""
    if path not in CARD_SECTIONS:
        raise SettingsError([f"{path!r} has no entry cards"])
    model = next(m for p, _shape, m, _help in SECTIONS if p == path)
    entries = _entries(path)
    if index is not None and not 0 <= index < len(entries):
        raise SettingsError([f"{path}[{index}] does not exist (the page may be stale; reload it)"])
    base = dict(entries[index]) if index is not None and isinstance(entries[index], dict) else {}
    where = f"{path}[{index}]" if index is not None else f"new {path[:-1]}"
    try:
        entry = _card_from_form(form, base, today) if path == "cards" else _BUILDERS[path](form, base)  # a card's logs date a blank row today
        _check_identity(path, index, entry, entries)
        model.model_validate(strip_comments(entry))
    except SettingsError:
        raise
    except Exception as exc:  # noqa: BLE001 -- pydantic's message is the useful part, once humanised
        raise SettingsError(humanise_errors(exc, where)) from exc
    if index is None:
        entries.append(entry)
        index = len(entries) - 1
    else:
        entries[index] = entry
    data = load_config()
    _set_path(data, path, entries)
    save_config(data)
    return index, entry_label(path, entry)


def toggle_proxy(index: int) -> tuple[str, bool]:
    """Flip a profile's proxy between in use and switched off (the details stay). Returns
    (label, now on). Raises SettingsError when the profile or its proxy does not exist."""
    entries = _entries("profiles")
    if not 0 <= index < len(entries) or not isinstance(entries[index], dict):
        raise SettingsError([f"profiles[{index}] does not exist (the page may be stale; reload it)"])
    entry = entries[index]
    proxy = entry.get("proxy")
    if not isinstance(proxy, dict) or not proxy.get("host"):
        raise SettingsError([f"profile {entry.get('label', index)} has no proxy to switch"])
    on = proxy.get("enabled", True) is False  # the new state
    if on:
        proxy.pop("enabled", None)
    else:
        proxy["enabled"] = False
    data = load_config()
    _set_path(data, "profiles", entries)
    save_config(data)
    return str(entry.get("label", "")), on


def delete_entry(path: str, index: int) -> str:
    """Remove one entry; returns its label. Raises SettingsError when it does not exist."""
    if path not in CARD_SECTIONS:
        raise SettingsError([f"{path!r} has no entry cards"])
    entries = _entries(path)
    if not 0 <= index < len(entries):
        raise SettingsError([f"{path}[{index}] does not exist (the page may be stale; reload it)"])
    removed = entries.pop(index)
    data = load_config()
    _set_path(data, path, entries)
    save_config(data)
    return entry_label(path, removed)
