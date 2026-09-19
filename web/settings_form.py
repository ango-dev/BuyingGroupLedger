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


#: Text settings with a vocabulary of their own, checked before anything is written.
_VALIDATORS = {"BACKUP_FREQUENCY": _validate_frequency, "BACKUP_TIME": _validate_time,
               "BACKUP_DAYS": _validate_days}


def _parse(setting: Setting, raw: str) -> Any:
    text = (raw or "").strip()
    if setting.env in _VALIDATORS:
        return _VALIDATORS[setting.env](text)
    if setting.kind == "int":
        if not text:
            return ""
        return int(text)
    if setting.kind == "float":
        if not text:
            return ""
        return float(text)
    if setting.kind == "rate":
        if not text:
            return ""
        rate = float(text[:-1].strip()) / 100 if text.endswith("%") else float(text)
        if not 0 <= rate <= 1:
            raise ValueError("outside 0-1 (write 2% as 0.02 or \"2%\")")
        return text if text.endswith("%") else rate
    return text


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
    "browser_use": ("Browser-Use", "The cloud browser Best Buy and the Amazons drive over CDP."),
    "container": ("Schedule", "How often the container runs, and whether it runs at start. "
                  "Read once at container start."),
    "scraping": ("Scraping", "How far back each run looks, and the money rules the ledger applies."),
    "alerts": ("Alerts", "Where a failed run, a logged-out session or a stale heartbeat is reported: "
               "a Discord webhook and a Gmail account, each with its own switch (off keeps the "
               "configuration and sends nothing). The Gmail account here is only for alerts -- the "
               "BFMR auto-reply's mailbox is its own setting under Buying groups, entered separately "
               "even when it is the same account."),
    "buying_groups": ("Buying groups", "BFMR and MaxOutDeals: API access, insurance, and the "
                      "combined-package auto-reply."),
    "receipts": ("Receipts", "Receipt capture: each order's proof of purchase, kept as a file beside "
                 "the ledger and served by this dashboard."),
    "web": ("Dashboard", "This web dashboard: its ledger source, bind address and port. "
            "Read once at dashboard start."),
    "database": ("Database", "The SQLite file that is the ledger. Read once at dashboard start."),
    "advanced": ("Advanced", "Where the ledger lives, which backend the dashboard serves, where it "
                 "listens, where receipts go, and the buying groups' API hosts. A wrong value here "
                 "shows an empty ledger, writes to the wrong file, or takes the dashboard off the "
                 "network -- the runs and the page were set up around these once and do not need "
                 "them changed."),
    "backups": ("Backups", "Scheduled backups of config.json, .state.json, .env and data/ (the "
                "ledger) into backups/, on the container's clock, and how many to keep. Read once "
                "at container start; the Backup & Restore panel below shows the schedule."),
    "profiles": ("Profiles", "One browser identity per entry: the Browser-Use profile, its proxy, "
                 "the retailers it is logged into and the sign-in it can perform unattended."),
    "warehouses": ("Warehouses", "Each buying group and the address jigs that route an order to "
                   "it. An order matching no jig is tagged Unclassified."),
    "cards": ("Cards", "Cards by their last 4 digits, with the cashback rate the profit formula "
              "nets from COGS -- overall, and per retailer."),
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


def display_entries(path: str) -> list[dict]:
    """What the cards show: the stored entries with every secret replaced by whether it is set."""
    out = []
    for index, raw in enumerate(_entries(path)):
        entry = strip_comments(raw) if isinstance(raw, dict) else {}
        if not isinstance(entry, dict):
            continue
        if path == "profiles":
            proxy = entry.get("proxy") or {}
            auth = entry.get("auth") or {}
            out.append({
                "index": index, "label": entry.get("label", ""),
                "profile_id": entry.get("profile_id", ""),
                "retailers": list(entry.get("retailers") or []),
                "proxy": {"host": proxy.get("host", ""), "port": proxy.get("port", ""),
                          "username": proxy.get("username", ""),
                          "password_set": _is_set(proxy.get("password"))} if proxy else None,
                "auth": [{"retailer": key, "username": (a or {}).get("username", ""),
                          "password_set": _is_set((a or {}).get("password")),
                          "totp_set": _is_set((a or {}).get("totp_secret"))}
                         for key, a in auth.items()],
            })
        elif path == "warehouses":
            out.append({
                "index": index, "buying_group": entry.get("buying_group", ""),
                "jigs": [{"label": j.get("label", ""), "street": j.get("street", ""),
                          "zip": j.get("zip", ""), "name_contains": j.get("name_contains", ""),
                          "contains": ", ".join(j.get("contains") or [])}
                         for j in (entry.get("jigs") or []) if isinstance(j, dict)],
            })
        elif path == "cards":
            out.append({
                "index": index, "last4": entry.get("last4", ""), "name": entry.get("name", ""),
                "cashback_rate": entry.get("cashback_rate", ""),
                "profile": entry.get("profile", ""),
                "virtual": bool(entry.get("virtual")),
                "retailer_rates": list((entry.get("retailer_rates") or {}).items()),
            })
    return out


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


def _rate_value(text: str):
    """A rate as typed: "2%" stays text (the models accept it), a number becomes a float."""
    text = text.strip()
    if not text:
        return None
    return text if text.endswith("%") else float(text)


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
    entry["retailers"] = [r for r in RETAILER_KEYS if r in chosen]
    old_proxy = base.get("proxy") or {}
    host = _text(form, "proxy_host")
    if host:
        proxy = dict(old_proxy)
        proxy["host"] = host
        port = _text(form, "proxy_port")
        proxy["port"] = int(port) if port.isdigit() else port
        proxy["username"] = _text(form, "proxy_username")
        proxy["password"] = _secret(form, "proxy_password", old_proxy.get("password"))
        entry["proxy"] = proxy
    else:
        entry.pop("proxy", None)
    auth = dict(base.get("auth") or {})
    for retailer in list(auth):
        if _text(form, f"auth.{retailer}.__remove") in ("1", "on", "true"):
            auth.pop(retailer)
            continue
        if f"auth.{retailer}.username" not in form:
            continue  # not on the form (a key the page does not know): kept as stored
        old = dict(auth.get(retailer) or {})
        old["method"] = old.get("method") or "password"
        old["username"] = _text(form, f"auth.{retailer}.username")
        old["password"] = _secret(form, f"auth.{retailer}.password", old.get("password"))
        old["totp_secret"] = _secret(form, f"auth.{retailer}.totp_secret", old.get("totp_secret"))
        auth[retailer] = old
    new_retailer = _text(form, "auth_new_retailer")
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


def _card_from_form(form: Mapping[str, str], base: dict) -> dict:
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
    if str(form.get("virtual", "")).strip().lower() in ("on", "true", "1", "yes"):
        entry["virtual"] = True
    else:
        entry.pop("virtual", None)
    rates = {}
    for i in _indexed(form, "rate"):
        retailer = _text(form, f"rate.{i}.retailer")
        value = _rate_value(_text(form, f"rate.{i}.rate"))
        if retailer and value is not None:
            rates[retailer] = value
    if rates:
        entry["retailer_rates"] = rates
    else:
        entry.pop("retailer_rates", None)
    return entry


_BUILDERS = {"profiles": _profile_from_form, "warehouses": _warehouse_from_form,
             "cards": _card_from_form}


def entry_label(path: str, entry: dict) -> str:
    entry = entry if isinstance(entry, dict) else {}
    if path == "profiles":
        return str(entry.get("label") or "(unlabelled profile)")
    if path == "warehouses":
        return str(entry.get("buying_group") or "(unnamed group)")
    name = entry.get("name") or "(unnamed card)"
    return f"{name} \u2026{entry.get('last4', '')}" if entry.get("last4") else str(name)


def apply_entry(path: str, index: int | None, form: Mapping[str, str]) -> tuple[int, str]:
    """Add (index None) or replace one entry of a card section from its form. Returns the index
    it landed at and its label. Raises SettingsError, nothing written."""
    if path not in CARD_SECTIONS:
        raise SettingsError([f"{path!r} has no entry cards"])
    model = next(m for p, _shape, m, _help in SECTIONS if p == path)
    entries = _entries(path)
    if index is not None and not 0 <= index < len(entries):
        raise SettingsError([f"{path}[{index}] does not exist (the page may be stale; reload it)"])
    base = dict(entries[index]) if index is not None and isinstance(entries[index], dict) else {}
    try:
        entry = _BUILDERS[path](form, base)
        model.model_validate(strip_comments(entry))
    except SettingsError:
        raise
    except Exception as exc:  # noqa: BLE001 -- pydantic's / int()'s message is the useful part
        where = f"{path}[{index}]" if index is not None else f"new {path[:-1]}"
        raise SettingsError([f"{where}: {exc}"]) from exc
    if index is None:
        entries.append(entry)
        index = len(entries) - 1
    else:
        entries[index] = entry
    data = load_config()
    _set_path(data, path, entries)
    save_config(data)
    return index, entry_label(path, entry)


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
