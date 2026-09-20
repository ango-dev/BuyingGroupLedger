"""The Activity page's filter state (diagnostics/activity.py holds the log itself)."""

from __future__ import annotations

from dataclasses import dataclass

from diagnostics.activity import KINDS

DEFAULT_DAYS = 7  # the last week by default
DAY_CHOICES = (1, 7, 30, 90, 0)
SORTS = ("at", "kind", "run", "summary")  # the table's sortable columns, by event field


def _values(params, name: str) -> tuple[str, ...]:
    if hasattr(params, "getlist"):
        raw = params.getlist(name)
    else:
        value = params.get(name)
        raw = value if isinstance(value, (list, tuple)) else ([value] if value is not None else [])
    return tuple(dict.fromkeys(str(v).strip() for v in raw if str(v).strip()))


@dataclass(frozen=True)
class ActivityFilters:
    kinds: tuple[str, ...] = ()
    days: int = DEFAULT_DAYS
    q: str = ""
    run_id: str = ""
    desc: bool = True
    #: A column to sort by ("" = time): at / kind / run / summary, the header arrows.
    sort: str = ""
    #: Types hidden from the feed unless picked explicitly -- browser state (the `activity-hide`
    #: cookie), not URL state. `hide_set` says the form carried the hide boxes this time,
    #: so an empty `hidden` means "hide nothing" rather than "the form did not say".
    hidden: tuple[str, ...] = ()
    hide_set: bool = False
    #: Only the alerts / dossiers still to acknowledge -- what the overview's card links to.
    unacked: bool = False

    @classmethod
    def from_query(cls, params) -> "ActivityFilters":
        try:
            days = int(str(params.get("days", DEFAULT_DAYS) or 0))
        except ValueError:
            days = DEFAULT_DAYS
        if days not in DAY_CHOICES:
            days = DEFAULT_DAYS
        chosen = _values(params, "type") or _values(params, "kind")  # `kind` = an older link
        return cls(
            kinds=tuple(k for k in chosen if k in KINDS),
            days=days,
            q=str(params.get("q") or "").strip(),
            run_id=str(params.get("run") or "").strip(),
            desc=str(params.get("dir") or "desc").strip().lower() != "asc",
            sort=(lambda s: s if s in SORTS else "")(str(params.get("sort") or "").strip()),
            hidden=tuple(k for k in _values(params, "hide") if k in KINDS),
            hide_set=str(params.get("hide_set") or "") == "1",
            unacked=str(params.get("unacked") or "").strip().lower() in ("1", "true", "yes"),
        )

    def with_hidden(self, hidden) -> "ActivityFilters":
        from dataclasses import replace

        return replace(self, hidden=tuple(k for k in hidden if k in KINDS))

    def as_query(self, **overrides) -> dict:
        values = {"type": list(self.kinds), "days": str(self.days) if self.days != DEFAULT_DAYS else "",
                  "q": self.q, "run": self.run_id, "dir": "" if self.desc else "asc", "sort": self.sort,
                  "unacked": "1" if self.unacked else ""}
        values = {**values, **overrides}
        return {k: v for k, v in values.items() if v not in ("", None, [], ())}
