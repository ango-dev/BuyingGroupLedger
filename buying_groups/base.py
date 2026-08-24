"""The provider-neutral half of the buying-group integration.

Every buying group wants the same thing from us — "here is a tracking number, here is what's in the
box" — and every one of them asks for it differently. The two we support already disagree on all of:

    auth          BFMR: two headers (API-KEY + API-SECRET).   MOD: Authorization: Bearer, plus an
                                                              IP ALLOWLIST on their side.
    identity      BFMR: its own reserve/purchase/shipment ids. MOD: the tracking number itself.
    granularity   BFMR: one object per ledger ROW (each item  MOD: one object per TRACKING NUMBER
                  is its own deal, with its own quantity).    (a 2-item box is ONE object).
    response      BFMR: JSON.                                  MOD: CSV — JSON only on error.
    limits        BFMR: an undocumented 429.                   MOD: 10 and 30 calls PER DAY.

So the abstraction here is deliberately thin: shared transport, shared vocabulary, and nothing else.
Anything a provider does differently belongs in that provider's adapter, not in a flag here.

`TrackingSubmission` is the vocabulary — one per eligible ledger row, built by the pure planner in
sheets/ledger_sync.py, and translated by each adapter into whatever its API wants. `PayoutRecord`
is the return trip: what a group paid, keyed by tracking number, which is the one identifier both
sides always agree on.

WHY THERE IS NO "already posted" COLUMN ON THE SHEET: the authoritative answer to "have I submitted
this?" lives at the group, not in our ledger, and a local mirror drifts both ways (a post that
succeeds while the sheet write fails re-posts forever; a number pasted into their web UI by hand
reads as unposted). Both providers can answer it themselves — MOD ignores duplicates outright, BFMR
has a status endpoint — so `already_submitted()` derives it per run instead.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import requests

from models.card import normalize_retailer

__all__ = [
    "BuyingGroupClient",
    "BuyingGroupError",
    "DailyCallBudget",
    "HttpClient",
    "PayoutRecord",
    "SubmissionResult",
    "TrackingSubmission",
    "normalize_group",
    "parse_money",
]

log = logging.getLogger(__name__)


class BuyingGroupError(RuntimeError):
    """Any failure talking to a buying group. Callers alert and move to the next row/provider."""


def parse_money(value) -> float | None:
    """Read a money field that may arrive as a number OR as display text. `None` if unreadable.

    BOTH providers send formatted strings, and a bare `float()` on them fails in the most dangerous
    possible way — it raises, the caller swallows it into `None`, and the amount silently becomes
    zero. BFMR reports `total_payout` as `"2,210.00"`, so every payout of $1,000 or more parsed as
    nothing while the small unformatted fee lines parsed fine: an order that earned $2,210 booked as
    **−$10.20**, which is a plausible-looking number no downstream check would question.

    Hence one shared parser, and hence it returns None rather than 0 — "unreadable" and "zero" must
    stay distinguishable, because callers write the former to no cell at all.
    """
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip().replace("$", "").replace(",", "")
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")  # accounting negatives
    try:
        number = float(text.strip("()"))
    except ValueError:
        return None
    return -number if negative else number


def normalize_group(name: str) -> str:
    """Fold a buying-group name to a comparison key: lowercase, no punctuation.

    Reuses models.card.normalize_retailer because it is exactly the same problem, and solving it
    twice invites the two solutions to drift. It matters here because the live warehouses.json says
    "MOD" while warehouses.example.json says "MaxOutDeals" — rather than declaring one of them wrong
    and breaking whichever config disagrees, both fold to the same provider through an alias table.
    """
    return normalize_retailer(name)


@dataclass(frozen=True)
class TrackingSubmission:
    """One eligible ledger row, in provider-neutral terms.

    `row_number` is the 1-based sheet row, carried so a per-row failure can name the row the user has
    to look at, and so a payout written back later lands on the right line.
    """

    row_number: int
    order_id: str
    tracking_number: str
    quantity: int
    item_name: str
    total_cost: float | None
    shipment: str
    order_date: str
    buying_group: str
    #: The ledger's Retailer for this row. Carried because provider behaviour differs BY RETAILER —
    #: BFMR's duplicate-tracking suffix applies only to Best Buy, which is the one retailer that
    #: reuses a tracking number across the orders it combines into a carton. Deliberately required
    #: rather than defaulted: a silently-blank retailer would just switch that handling off.
    retailer: str

    def describe(self) -> str:
        """A short human label for logs and alerts — never sent to an API."""
        return f"row {self.row_number}: {self.order_id} / {self.tracking_number} ({self.item_name})"


@dataclass(frozen=True)
class PayoutRecord:
    """What a group reports paying, keyed by the identifier both sides agree on.

    Every field except `tracking_number` is optional because the two providers expose different
    subsets: MOD's CSV has no explicit paid-date column, and MOD insurance is always 0.
    """

    tracking_number: str
    payout_amount: float | None = None
    payout_date: str = ""
    insurance: float | None = None
    order_id: str = ""
    #: The buying group's OUTCOME for this package, in the ledger's vocabulary — "paid", "return", or
    #: "" for "no opinion, leave the row alone". Only these two, deliberately: they are the states a
    #: retailer scrape can never observe, so the group is the sole authority on them. Everything
    #: about the package's journey (shipped/delivered) stays the retailer's to report, or the two
    #: sources would overwrite each other on every run.
    status: str = ""


@dataclass
class SubmissionResult:
    """Per-row outcome of a push. Not a bare count: a partial success is the normal case.

    `skipped` is for rows the provider already had (the good kind of no-op); `failed` is for rows
    that were rejected and need a human. Keeping them apart is what stops a run that legitimately
    did nothing from looking like a run that broke.
    """

    submitted: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    #: Rejected in a way NO retry can fix — only a specific human action will. Kept apart from
    #: `failed` because the two demand different responses: a failure is worth investigating, while
    #: this is a known situation with known steps, and burying it among transient errors is how a
    #: package quietly goes unsubmitted for weeks. The Best Buy combined-carton case is the one that
    #: prompted it; any provider may add others.
    needs_manual: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        """Skips are summarised BY REASON, not lumped together.

        They arrive from genuinely different causes — already recorded there, below the insurance
        threshold, or simply a dry run — and collapsing them into one label ("already known") makes a
        dry run read as though the provider already had everything, which is the opposite of the
        truth and would talk someone out of doing the real submission.
        """
        parts = [f"{len(self.submitted)} submitted"]
        if self.needs_manual:
            parts.append(f"{len(self.needs_manual)} need manual action")
        reasons: dict[str, int] = {}
        for _tracking, reason in self.skipped:
            reasons[reason] = reasons.get(reason, 0) + 1
        parts += [f"{count} skipped ({reason})" for reason, count in sorted(reasons.items())]
        parts.append(f"{len(self.failed)} failed")
        return ", ".join(parts)


class DailyCallBudget:
    """A hard local ceiling on calls to one endpoint.

    MOD publishes limits of 10/day (its receipts search) and 30/day (its tracking push), reset daily.
    A 6-hourly schedule is only 4 runs a day, so the budget is comfortable — but a retry loop or an
    afternoon of manual testing would burn it silently and the next scheduled run would simply fail
    to submit anything. Refusing locally turns that into a loud, attributable error instead of a
    quiet gap in reimbursements.

    Deliberately per-process, not persisted: it bounds a single run's behaviour, which is where the
    runaway risk actually is. The provider remains the real authority on the daily total.
    """

    def __init__(self, limit: int, label: str):
        self.limit = limit
        self.label = label
        self.used = 0

    def spend(self, count: int = 1) -> None:
        if self.used + count > self.limit:
            raise BuyingGroupError(
                f"{self.label}: refusing to exceed the local call budget of {self.limit} "
                f"(already used {self.used}). Batch the work or wait for the daily reset."
            )
        self.used += count


class HttpClient:
    """Shared transport. Auth is a subclass concern; everything else is the same everywhere.

    `dry_run` gates MUTATING calls only. Reads stay live even in a dry run, and that is the point:
    the dry run's job is to show exactly what it *would* change, which it can only do by first
    reading the group's real current state. Note MOD's read is itself a POST, so "mutating" has to
    be stated by the caller rather than inferred from the HTTP verb.
    """

    #: Retry ceiling for 429s. BFMR documents a 429 on every endpoint but never states the limit, so
    #: back off rather than guess a rate.
    max_attempts = 4
    backoff_seconds = 2.0
    timeout_seconds = 30

    def __init__(self, base_url: str, *, dry_run: bool = True):
        self.base_url = (base_url or "").rstrip("/")
        self.dry_run = dry_run

    # --- subclass hooks -------------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _check_configured(self) -> None:
        """Raise if credentials are missing. Called before every request."""
        raise NotImplementedError

    # --- transport ------------------------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        mutating: bool,
        params: dict | None = None,
        json_body: dict | list | None = None,
        data: dict | None = None,
    ) -> Any:
        """Issue one request, retrying only on 429.

        Returns the parsed JSON body, or the raw `requests.Response` when the caller needs to sniff
        the content type itself (MOD answers with CSV on success and JSON on failure).
        """
        self._check_configured()
        url = f"{self.base_url}/{path.lstrip('/')}"

        if mutating and self.dry_run:
            log.info(
                "DRY RUN %s %s — would send %s",
                method, url, json_body if json_body is not None else data,
            )
            return None

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = requests.request(
                    method,
                    url,
                    headers=self._headers(),
                    params=params,
                    json=json_body,
                    data=data,
                    timeout=self.timeout_seconds,
                )
            except requests.RequestException as exc:
                raise BuyingGroupError(f"{method} {url} failed: {exc}") from exc

            if response.status_code == 429 and attempt < self.max_attempts:
                delay = self.backoff_seconds * attempt
                log.warning(
                    "%s %s rate limited (429), retrying in %.1fs (attempt %d/%d)",
                    method, url, delay, attempt, self.max_attempts,
                )
                time.sleep(delay)
                last_error = BuyingGroupError(f"{method} {url} rate limited")
                continue

            if response.status_code >= 400:
                raise BuyingGroupError(
                    f"{method} {url} returned {response.status_code}: {response.text[:400]}"
                )
            return response

        raise BuyingGroupError(str(last_error))

    def get_json(self, path: str, params: dict | None = None) -> dict:
        response = self.request("GET", path, mutating=False, params=params)
        return self._as_json(response, path)

    @staticmethod
    def _as_json(response, path: str) -> dict:
        if response is None:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise BuyingGroupError(
                f"{path} did not return JSON: {response.text[:200]!r}"
            ) from exc


class BuyingGroupClient(Protocol):
    """What sync_tracking.py needs from a provider, and nothing more.

    A new buying group becomes supported by implementing this and registering an alias — no change
    to the planner, the orchestrator, or the sheet.
    """

    group_key: str

    def already_submitted(self, rows: list[TrackingSubmission]) -> set[tuple[str, str]]:
        """Which of these the group already holds, as a set of `(order_id, tracking_number)`.

        KEYED ON THE PAIR, NOT THE TRACKING NUMBER ALONE. Best Buy combines several ORDERS into one
        physical box, so two ledger rows legitimately share a tracking number while needing two
        separate submissions. Keyed on tracking alone, submitting the first order would mark the
        second as done — and that package would never be submitted and never reimbursed.

        May return an empty set for a provider that dedupes server-side.
        """
        ...

    def submit_tracking(self, rows: list[TrackingSubmission]) -> SubmissionResult:
        ...

    def fetch_payouts(self, tracking_numbers: list[str]) -> list[PayoutRecord]:
        ...
