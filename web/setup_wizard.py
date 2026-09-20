"""The first-time setup wizard (/setup: "a first time setup wizard that can be
reactivated in settings -- when reactivated it asks the same things but asks if you'd like to
change what has already been set").

WHAT IT IS. Ten steps over the Settings page's own fields and save functions (web/settings_form.py):
restore a backup, the dashboard password, the Browser-Use key, profiles, buying groups (the
warehouses and the groups' API keys), cards, alerts, the schedule and backups, the history
importer, done. Nothing here is a second way to store a setting -- every step writes config.json
through `apply_scalars` / `apply_entry`, exactly as Settings does.

WHEN IT SHOWS. On a FRESH install only: no config.json, or one with no profiles, and no record of
a finished setup. A configured install is never interrupted: the first time the dashboard sees one
without the record it writes the record (the install is its own finished setup). The record lives
in .state.json (`setup`), the file the app owns, which every backup carries. Re-running from
Settings sets `rerun`; then every step shows what is set now and offers Keep.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from config import loader

__all__ = ["STEPS", "Step", "is_configured", "mark_complete", "mark_rerun", "needs_setup", "step"]

RESTART_RANK = {"": 0, "next run": 0, "dashboard": 1, "container": 2}
#: The gate's switch. Off in the test suite (conftest.py): every test runs with an empty config,
#: which is exactly what a fresh install looks like, and only tests/test_setup_wizard.py wants the
#: wizard for that.
GATE_ENABLED = True


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    kind: str  # restore | password | scalars | entries | import | done
    optional: bool = False
    envs: tuple = ()          # the scalar settings the step saves, by env name...
    sections: tuple = ()      # ...or by config section (advanced ones excluded)
    section: str = ""         # the entry-card section (profiles / warehouses / cards) the step edits

    def scalar_settings(self) -> list:
        from web import settings_form

        return [s for s in settings_form.schema() if not s.advanced
                and (s.env in self.envs or s.section in self.sections)]


STEPS: tuple[Step, ...] = (
    Step("restore", "Restore a backup", "restore", optional=True),
    Step("password", "Dashboard password", "password", envs=("WEB_PASSWORD",)),
    Step("browser_use", "Browser-Use key", "scalars", envs=("BROWSER_USE_API_KEY",)),
    Step("profiles", "Profiles", "entries", section="profiles"),
    Step("groups", "Buying groups", "entries", section="warehouses", sections=("buying_groups",)),
    Step("cards", "Cards", "entries", section="cards"),
    Step("alerts", "Alerts", "scalars", sections=("alerts",)),
    Step("schedule", "Schedule and backups", "scalars", sections=("container", "backups")),
    Step("import", "Import history", "import", optional=True),
    Step("done", "Done", "done"),
)
_BY_KEY = {s.key: s for s in STEPS}


def step(key: str) -> Step:
    return _BY_KEY[key]  # KeyError = not a step


def index(key: str) -> int:
    return [s.key for s in STEPS].index(key)


def next_step(key: str) -> Step | None:
    i = index(key)
    return STEPS[i + 1] if i + 1 < len(STEPS) else None


def prev_step(key: str) -> Step | None:
    i = index(key)
    return STEPS[i - 1] if i > 0 else None


# --------------------------------------------------------------------------------------------------
# The record in .state.json
# --------------------------------------------------------------------------------------------------


def is_configured(config: dict | None = None) -> bool:
    """A profile exists: the install has been set up, by the wizard or by hand."""
    config = loader.load_config() if config is None else config
    profiles = config.get("profiles")
    return isinstance(profiles, list) and len(profiles) > 0


def state() -> dict:
    record = loader.load_state().get("setup")
    return dict(record) if isinstance(record, dict) else {}


def _save(record: dict) -> None:
    whole = loader.load_state()
    whole["setup"] = record
    loader.save_state(whole)


def mark_complete(clock: Callable[[], datetime]) -> None:
    record = state()
    record.update(completed_at=clock().isoformat(timespec="seconds"), version=1, rerun=None)
    _save(record)


def mark_rerun(clock: Callable[[], datetime]) -> None:
    record = state()
    record["rerun"] = clock().isoformat(timespec="seconds")
    _save(record)


def clear_rerun() -> None:
    record = state()
    if record.get("rerun"):
        record["rerun"] = None
        _save(record)


def rerunning() -> bool:
    return bool(state().get("rerun"))


def note_restart(scope: str) -> None:
    """The strongest restart the wizard's saves have called for so far ("container" beats
    "dashboard"); the Done step shows it."""
    record = state()
    current = str(record.get("restart") or "")
    if RESTART_RANK.get(scope, 0) > RESTART_RANK.get(current, 0):
        record["restart"] = scope
        _save(record)


def pending_restart() -> str:
    scope = str(state().get("restart") or "")
    return scope if scope in ("dashboard", "container") else ""


def clear_restart() -> None:
    record = state()
    if record.get("restart"):
        record["restart"] = ""
        _save(record)


def needs_setup(clock: Callable[[], datetime]) -> bool:
    """Gate the dashboard to /setup? A fresh install with no finished setup on record. An install
    with profiles and no record is stamped complete here, once, so an existing host is never
    interrupted and can still re-run the wizard later."""
    fresh = not is_configured()
    done = bool(state().get("completed_at"))
    if not fresh and not done:
        mark_complete(clock)
        return False
    return fresh and not done
