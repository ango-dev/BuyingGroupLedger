"""Failure dossiers — what a deterministic path leaves behind when it breaks, instead of an agent.

The paid Browser-Use agent used to be the answer to "the page changed shape". It has been removed
outright, and this package is what replaced it: on any deterministic-path
failure the run writes a self-contained bundle under `logs/failures/` — the exception and traceback,
a timeline of what the path was doing, the page HTML and a screenshot at the moment it failed, and an
audit of every selector the retailer's parser depends on (how many matches, what they read). That is
exactly what a coding agent needs to fix the selector, and it costs nothing.

Public surface (see dossier.py): `collecting(...)` opens a dossier for a scrape; `current()`,
`note()`, `snapshot()`, `snapshot_html()`, `record_response()`, `problem()` and `add_secrets()` write into whichever
dossier is open and are no-ops when none is — so the API clients and mapping code can call them
unconditionally.
"""

from diagnostics.dossier import (  # noqa: F401
    FailureDossier,
    add_secrets,
    collecting,
    current,
    note,
    problem,
    record_response,
    snapshot,
    snapshot_html,
)
