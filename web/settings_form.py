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
`warehouses`, `cards`, or an object like `google.service_account`): those are edited as JSON, each
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
    ("google.service_account", "object", None,
     "The whole service-account JSON object Google issued (share the sheet with its client_email)."),
)


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
    def key(self) -> str:
        return self.path.split(".", 1)[1] if "." in self.path else self.path


def env_to_field() -> dict[str, tuple[str, str]]:
    """{ENV_NAME: (Settings field name, getter suffix)} parsed from the class source."""
    out: dict[str, tuple[str, str]] = {}
    for match in _DECL.finditer(inspect.getsource(Settings)):
        out.setdefault(match["env"], (match["field"], match["getter"]))
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
    # keys that share one explanation (the BFMR headers, the OCI credential pair).
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


def sections_in_order(settings: list[Setting]) -> list[str]:
    seen: list[str] = []
    for s in settings:
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
    rows = []
    for s in settings:
        value = current_value(s)
        rows.append({
            "setting": s,
            "value": "" if value is None else value,
            "secret_set": bool(config_value(s.path)) if s.secret else False,
            "overridden": is_overridden(s, environ),
        })
    return rows


def section_text(path: str) -> str:
    """A structured section as pretty JSON for its textarea ("[]" / "{}" when absent)."""
    value = config_value(path)
    if value in (None, ""):
        value = {} if path == "google.service_account" else []
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


def _parse(setting: Setting, raw: str) -> Any:
    text = (raw or "").strip()
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


def apply_scalars(form: Mapping[str, str], settings: list[Setting] | None = None) -> dict:
    """Write every scalar setting from the form into config.json. Checkboxes: absent = false.
    Secrets: blank keeps the stored value; `<ENV>__clear` = "1" blanks it. Returns {path: value}
    for what changed. Raises SettingsError (nothing written) when any value fails to parse."""
    settings = settings or schema()
    data = load_config()
    changes: dict[str, Any] = {}
    errors: list[str] = []
    for s in settings:
        before = config_value(s.path)
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
