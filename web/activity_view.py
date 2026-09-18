"""The Activity page's filter state (diagnostics/activity.py holds the log itself)."""

from __future__ import annotations

from dataclasses import dataclass

from diagnostics.activity import KINDS

DEFAULT_DAYS = 30
DAY_CHOICES = (1, 7, 30, 90, 0)


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

    @classmethod
    def from_query(cls, params) -> "ActivityFilters":
        try:
            days = int(str(params.get("days", DEFAULT_DAYS) or 0))
        except ValueError:
            days = DEFAULT_DAYS
        if days not in DAY_CHOICES:
            days = DEFAULT_DAYS
        return cls(
            kinds=tuple(k for k in _values(params, "kind") if k in KINDS),
            days=days,
            q=str(params.get("q") or "").strip(),
            run_id=str(params.get("run") or "").strip(),
            desc=str(params.get("dir") or "desc").strip().lower() != "asc",
        )

    def as_query(self, **overrides) -> dict:
        values = {"kind": list(self.kinds), "days": str(self.days) if self.days != DEFAULT_DAYS else "",
                  "q": self.q, "run": self.run_id, "dir": "" if self.desc else "asc"}
        values = {**values, **overrides}
        return {k: v for k, v in values.items() if v not in ("", None, [], ())}
