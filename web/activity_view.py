"""The Activity page's filter state (diagnostics/activity.py holds the log itself)."""

from __future__ import annotations

from dataclasses import dataclass

from diagnostics.activity import KINDS

DEFAULT_DAYS = 7  # the last week by default
DAY_CHOICES = (1, 7, 30, 90, 0)
SORTS = ("at", "kind", "run", "summary")  # the table's sortable columns, by event field
#: The page holds a slice, never the whole log. 0 = all of them, for whoever wants the old behaviour.
PER_CHOICES = (50, 100, 250, 500, 0)
DEFAULT_PER = 100


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
    #: The page size (a preset from PER_CHOICES; 0 = everything) and the page. `as_query` never
    #: carries the page, so every filter, sort and run link lands back on page 1 by itself; only
    #: the pager's own links name one.
    per: int = DEFAULT_PER
    page: int = 1

    @classmethod
    def from_query(cls, params) -> "ActivityFilters":
        try:
            days = int(str(params.get("days", DEFAULT_DAYS) or 0))
        except ValueError:
            days = DEFAULT_DAYS
        if days not in DAY_CHOICES:
            days = DEFAULT_DAYS
        try:
            per = int(str(params.get("per", DEFAULT_PER) or 0))
        except ValueError:
            per = DEFAULT_PER
        if per not in PER_CHOICES:
            per = DEFAULT_PER
        try:
            page = max(1, int(str(params.get("page") or 1)))
        except ValueError:
            page = 1
        chosen = _values(params, "type") or _values(params, "kind")  # `kind` = an older link
        return cls(
            per=per,
            page=page,
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
                  "unacked": "1" if self.unacked else "",
                  "per": str(self.per) if self.per != DEFAULT_PER else ""}
        values = {**values, **overrides}
        return {k: v for k, v in values.items() if v not in ("", None, [], ())}
