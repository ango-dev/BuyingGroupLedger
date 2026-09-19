"""The dashboard's Tools page: a curated registry of the scripts worth running from a browser, a job runner that runs
one as a subprocess and keeps its output, and the profile-login session -- the one interactive
tool -- with the live Browser-Use browser embedded in the page.

WHAT IS HERE AND WHAT IS NOT. Every tool below is `python -m <module> <args>` (scripts.<name>, or
main for the run itself) with the
arguments its own argparse declares, run from the repo root exactly as the operator would run it;
nothing is re-implemented. The RECON probes (capture / signin / paginate probes) and the one-off
migrations (migrate_config) are developer tools and stay on
the command line, as do the ones that take a local file (import_history, restore_cells).

WHAT IS GUARDED. A tool marked `writes` changes the ledger (or a third party), so its form asks
in-page first, its dry run is the default where the script has one, and it is REFUSED while a
scheduled run holds `logs/.run.lock` -- the same rule as the dashboard's own ledger writes. A
tool marked `spends` says so on its card (a cloud browser session, a buying-group call).

THE PROFILE SESSION. `ProfileSessions` drives scripts.create_profile's functions: create / reuse
the cloud profile, open a kept-alive live session through the profile's proxy, hand the page the
`live_url` to embed, and CLOSE the session -- which is what saves the cookies -- when the user
says so, or after `web.tool_session_minutes` (default 60) if they walked away. Its state lives in
`logs/tools/profile_session.json` so a dashboard restart still knows there is a session to close.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
RETAILER_CHOICES = ("", "amazon", "amazon-business", "bestbuy", "costco")


@dataclass(frozen=True)
class Field:
    """One form field of a tool: how the page asks for it, and the argv it becomes."""

    name: str            # the argparse option ("--apply") or "" for a positional
    label: str
    kind: str = "flag"   # "flag" | "text" | "int" | "select" | "list"
    help: str = ""
    default: str = ""
    choices: tuple[str, ...] = ()
    required: bool = False


@dataclass(frozen=True)
class Tool:
    key: str
    module: str          # scripts.<module>
    title: str
    blurb: str
    group: str
    fields: tuple[Field, ...] = ()
    writes: bool = False  # changes the ledger / a third party: confirmed, run-lock gated
    spends: str = ""      # what it costs, if anything ("a cloud browser session")
    #: Stamp logs/.last_run when the job ends, as docker/run_once.sh does after the scheduled run.
    #: Only for a tool that IS the run: it holds the same run lock the schedule checks, so a hand
    #: run and a scheduled one are one and the same event to the heartbeat.
    heartbeat: bool = False

    def argv(self, form: dict) -> list[str]:
        """The command-line arguments for a submitted form, validated: an unknown field or a bad
        number raises ValueError with the message the page shows."""
        args: list[str] = []
        for f in self.fields:
            raw = str(form.get(f.name or f.label, "") or "").strip()
            if f.kind == "flag":
                if raw in ("on", "1", "true"):
                    args.append(f.name)
                continue
            if not raw:
                if f.required:
                    raise ValueError(f"{f.label} is required")
                continue
            if f.kind == "int":
                if not raw.lstrip("-").isdigit():
                    raise ValueError(f"{f.label} must be a whole number")
            if f.kind == "select" and f.choices and raw not in f.choices:
                raise ValueError(f"{f.label}: not one of {', '.join(c for c in f.choices if c)}")
            if f.kind == "list":
                for item in [x.strip() for x in raw.replace(",", " ").split() if x.strip()]:
                    if f.name:
                        args += [f.name, item]
                    else:
                        args.append(item)
                continue
            if f.name:
                args += [f.name, raw]
            else:
                args.append(raw)
        return args


APPLY = Field("--apply", "Apply", "flag", "write for real (unticked = dry run: shows what it would do)")
RETAILER = Field("--retailer", "Retailer", "select", "one retailer only", choices=RETAILER_CHOICES)

# What is NOT here: the audit (the Audit page), the tax report (the Taxes page), and the one-time migrations
# that have run (commitments to Expected Payout, receipts home from OCI -- both scripts deleted).
TOOLS: tuple[Tool, ...] = (
    # A real run, exactly as the schedule does it:
    # `python -m main [retailer]` -- main.py takes its own run lock and exits quietly if one is
    # held, and the page refuses to start it while the lock is live anyway. It scrapes, upserts
    # the ledger, then runs the buying-group sync: tracking is SUBMITTED and insurance FILED.
    Tool("run_once", "main", "Run once",
         "A full run now, as the schedule does it: scrape every retailer (or one), update the ledger, then the "
         "buying-group sync -- which submits tracking numbers and files insurance for real. Not a dry run.",
         "Run", (Field("", "Retailer", "select", "blank = every retailer", choices=RETAILER_CHOICES),),
         writes=True, spends="cloud browser sessions, and it submits tracking and files insurance",
         heartbeat=True),
    Tool("preflight", "scripts.preflight", "Preflight check",
         "Config and dependency check: the misconfigurations that would otherwise fail silently. Offline.",
         "Checks", (Field("--strict", "Strict", "flag", "treat warnings as failures"),)),
    Tool("bg_probe", "scripts.bg_probe", "Buying-group probe",
         "Read-only recon against the BFMR and MaxOutDeals APIs: what they know about your packages. Submits nothing.",
         "Checks", (Field("--tracking", "Tracking numbers", "list", "space-separated; blank = the ledger's open ones"),
                    Field("--mod", "Probe MaxOutDeals", "flag"),
                    Field("--skip-bfmr", "Skip BFMR", "flag"))),
    Tool("receipt_verify", "scripts.receipt_verify", "Receipt audit",
         "Audits the stored receipt files against the ledger: own order id, final, a total, a payment method. "
         "Read-only unless Purge.",
         "Checks", (RETAILER, Field("--purge", "Purge orphans", "flag", "DELETE receipts no ledger row points at")),
         writes=True),
    Tool("costco_token", "scripts.costco_token", "Costco refresh token",
         "Save or inspect the refresh token the Costco API path signs in with. Grab reads it from the profile's logged-in session.",
         "Accounts", (Field("--label", "Profile", "text", "the profile label that owns the Costco membership", required=True),
                      Field("--grab", "Grab from the profile", "flag", "read the token from the profile's browser storage (opens a cloud session)"),
                      Field("--token", "Token", "text", "or paste the refresh token secret by hand"),
                      Field("--warehouses", "Warehouses", "text", "comma-separated warehouse numbers, e.g. 847"),
                      Field("--show", "Show only", "flag", "print what is stored (token masked)")),
         writes=True, spends="a cloud browser session when grabbing"),
    Tool("backfill_tracking", "scripts.backfill_tracking", "Backfill tracking numbers",
         "Fill blank Tracking Number cells from the buying groups, matched by order number.",
         "Ledger Fixes", (APPLY,), writes=True),
    Tool("backfill_receipts", "scripts.backfill_receipts", "Backfill receipts",
         "Capture receipts for orders already on the ledger that have none.",
         "Ledger Fixes", (APPLY, RETAILER, Field("--limit", "Limit", "int", "orders per profile x retailer")),
         writes=True, spends="a cloud browser session per profile"),
    Tool("backfill_gift_cards", "scripts.backfill_gift_cards", "Backfill gift cards",
         "Fill the Gift Card and Sales Tax cells for Amazon orders already on the ledger.",
         "Ledger Fixes", (APPLY, Field("--retailer", "Retailer", "select", choices=("", "amazon", "amazon-business")),
                          Field("--orders", "Orders", "list", "limit to these order numbers")),
         writes=True, spends="a cloud browser session"),
    Tool("backfill_amazon_promo", "scripts.backfill_amazon_promo", "Backfill Amazon promo cashback",
         "Fill the Amazon promo cashback and gift-card netting on rows already on the ledger.",
         "Ledger Fixes", (APPLY, Field("--retailer", "Retailer", "select", choices=("", "amazon", "amazon-business")),
                          Field("--order", "Orders", "list", "limit to these order numbers"),
                          Field("--limit", "Limit", "int")),
         writes=True, spends="a cloud browser session"),
    Tool("retag_buying_groups", "scripts.retag_buying_groups", "Retag buying groups",
         "Re-apply the warehouse classification to every row (after adding a warehouse or jig).",
         "Ledger Fixes", (APPLY,), writes=True),
    Tool("fix_superseded_shipments", "scripts.fix_superseded_shipments", "Fix a superseded shipment",
         "Mark a dead (re-labelled) tracking number's row superseded, or delete it.",
         "Ledger Fixes", (Field("--order", "Orders", "list", "the order number(s) to repair", required=True),
                          APPLY, Field("--delete", "Delete the row", "flag", "instead of marking it superseded")),
         writes=True),
    Tool("sort_ledger", "scripts.sort_ledger", "Sort the ledger",
         "Sort newest-first and re-stamp the formulas (the run does this itself after an append).",
         "Ledger Fixes", (APPLY,), writes=True),
    Tool("hand_edits", "scripts.hand_edits", "Hand-edited cells",
         "The cells typed on the dashboard that no run may overwrite. Lists them; Forget releases an order's "
         "cells (or one field of them) so the next run may write them again.",
         "Ledger Fixes", (Field("--forget", "Forget order", "text", "an order number; blank = just list"),
                          Field("--field", "Field", "text", "with Forget: release only this field, e.g. cashback_rate")),
         writes=True),
)

GROUPS = ("Run", "Accounts", "Checks", "Ledger Fixes")


def tool(key: str) -> Tool:
    for t in TOOLS:
        if t.key == key:
            return t
    raise KeyError(key)


# --------------------------------------------------------------------------------------------------
# Jobs: one subprocess per run, output kept in logs/tools/
# --------------------------------------------------------------------------------------------------


@dataclass
class Job:
    id: str
    tool_key: str
    argv: list[str]
    started_at: datetime
    log_path: Path
    returncode: int | None = None
    finished_at: datetime | None = None
    error: str = ""
    heartbeat: bool = False  # stamp logs/.last_run when this job ends (Tool.heartbeat)
    _process: Any = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return self.returncode is None and not self.error

    def output(self, limit: int = 200_000) -> str:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return text[-limit:]


class JobRunner:
    """Runs tools as subprocesses (`python -m scripts.<module>`), one at a time per tool, and
    remembers every job of this dashboard process. `launch` is the injection point for tests."""

    def __init__(self, logs_dir: Path, root: Path | None = None, python: str | None = None,
                 clock: Callable[[], datetime] | None = None, launch=None):
        self.logs_dir = Path(logs_dir)
        self.dir = self.logs_dir / "tools"
        self.root = Path(root) if root else ROOT
        self.python = python or sys.executable
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.launch = launch or self._launch
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def _launch(self, command: list[str], log_handle):
        return subprocess.Popen(command, cwd=str(self.root), stdout=log_handle, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True)

    def running_for(self, tool_key: str) -> Job | None:
        return next((j for j in self.jobs.values() if j.tool_key == tool_key and j.running), None)

    def start(self, t: Tool, argv: list[str]) -> Job:
        with self._lock:
            if self.running_for(t.key):
                raise RuntimeError(f"{t.title} is already running")
            self.dir.mkdir(parents=True, exist_ok=True)
            job_id = self.clock().strftime("%Y%m%dT%H%M%SZ") + "-" + t.key + "-" + uuid.uuid4().hex[:6]
            job = Job(id=job_id, tool_key=t.key, argv=list(argv), started_at=self.clock(),
                      log_path=self.dir / f"{job_id}.log", heartbeat=t.heartbeat)
            command = [self.python, "-m", t.module, *argv]
            try:
                handle = job.log_path.open("w", encoding="utf-8")
                handle.write(f"$ python -m {t.module} {' '.join(argv)}\n\n")
                handle.flush()
                job._process = self.launch(command, handle)
            except Exception as exc:  # noqa: BLE001 -- the page shows why it could not start
                job.error = f"{type(exc).__name__}: {exc}"
                job.finished_at = self.clock()
            self.jobs[job_id] = job
        if job._process is not None:
            threading.Thread(target=self._wait, args=(job, handle), daemon=True).start()
        return job

    def _wait(self, job: Job, handle) -> None:
        try:
            job.returncode = job._process.wait()
        except Exception as exc:  # noqa: BLE001
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.finished_at = self.clock()
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass
            if job.heartbeat:
                self.stamp_heartbeat(job)

    def stamp_heartbeat(self, job: Job) -> None:
        """logs/.last_run, exactly as docker/run_once.sh writes it: after the run, whatever its
        exit code (the heartbeat says the run HAPPENED; a failed run alerts on its own)."""
        try:
            stamp = self.clock().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            (self.logs_dir / ".last_run").write_text(stamp, encoding="utf-8")
        except OSError as exc:
            log.warning("could not stamp the heartbeat after %s: %s", job.id, exc)

    def recent(self, limit: int = 20) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda j: j.started_at, reverse=True)[:limit]


# --------------------------------------------------------------------------------------------------
# The profile login session (scripts.create_profile, embedded)
# --------------------------------------------------------------------------------------------------


class ProfileSessions:
    """One live Browser-Use session at a time, for logging a profile in from the page.

    `client_factory`, `clock` and `save` are injection points; the defaults are the real ones
    (scripts.create_profile). State is written to logs/tools/profile_session.json.
    """

    def __init__(self, logs_dir: Path, *, minutes: int = 60, clock: Callable[[], datetime] | None = None,
                 client_factory=None, timer: bool = True):
        self.path = Path(logs_dir) / "tools" / "profile_session.json"
        self.minutes = max(1, int(minutes or 60))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.client_factory = client_factory
        self._timer_enabled = timer
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    # --- state ---------------------------------------------------------------------------------
    def current(self) -> dict | None:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return state if isinstance(state, dict) and state.get("session_id") else None

    def _write(self, state: dict | None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if state is None:
            try:
                self.path.unlink()
            except OSError:
                pass
        else:
            self.path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _client(self):
        if self.client_factory is not None:
            return self.client_factory()
        from scripts.create_profile import make_client

        return make_client()

    def remaining_seconds(self, state: dict | None = None) -> int:
        state = state or self.current()
        if not state:
            return 0
        try:
            expires = datetime.fromisoformat(state["expires_at"])
        except (KeyError, ValueError):
            return 0
        return max(0, int((expires - self.clock()).total_seconds()))

    # --- the three moves -----------------------------------------------------------------------
    def start(self, label: str, add_retailers=()) -> dict:
        from scripts.create_profile import (ensure_profile_id, find_profile, login_guidance,
                                            open_live_session)

        with self._lock:
            if self.current():
                raise RuntimeError("a profile session is already open; close it first")
            profiles, profile = find_profile(label)  # ValueError = the operator's message
            client = self._client()
            try:
                profile_id, created = ensure_profile_id(client, profile)
                session = open_live_session(client, profile, profile_id)
            finally:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
            now = self.clock()
            state = {
                "label": label, "profile_id": profile_id, "created": created,
                "session_id": str(session.id), "live_url": str(session.live_url),
                "add_retailers": [r for r in add_retailers if r],
                "proxy": f"{profile.proxy.host}:{profile.proxy.port}",
                "started_at": now.isoformat(timespec="seconds"),
                "expires_at": (now + timedelta(minutes=self.minutes)).isoformat(timespec="seconds"),
                "guidance": login_guidance(profile, add_retailers),
            }
            self._write(state)
            self._arm_timer()
            return state

    def finish(self, reason: str = "closed from the page") -> dict | None:
        """Stop the session (saving the cookies) and write the profile id into config.json.
        Returns the closed state, or None when nothing was open."""
        from scripts.create_profile import close_session, find_profile, save_profile

        with self._lock:
            state = self.current()
            if not state:
                return None
            warning = ""
            client = self._client()
            try:
                warning = close_session(client, state["session_id"])
            finally:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
            saved = ""
            try:
                profiles, profile = find_profile(state["label"])
                added = save_profile(profiles, profile, state["profile_id"], state.get("add_retailers") or [])
                saved = f"profile_id saved" + (f", added {', '.join(added)}" if added else "")
            except Exception as exc:  # noqa: BLE001 -- the session is closed either way; say what failed
                saved = f"could not save the profile id: {exc}"
            self._write(None)
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            return {**state, "closed_at": self.clock().isoformat(timespec="seconds"), "reason": reason,
                    "warning": warning, "saved": saved}

    def expire_if_due(self) -> dict | None:
        """Close the session once its time is up (called on every Tools page load and by the
        timer, so a session outlives the tab but not the limit)."""
        state = self.current()
        if state and self.remaining_seconds(state) <= 0:
            return self.finish(reason=f"left open for {self.minutes} minutes")
        return None

    def _arm_timer(self) -> None:
        if not self._timer_enabled:
            return
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self.minutes * 60 + 1, self.expire_if_due)
        self._timer.daemon = True
        self._timer.start()
