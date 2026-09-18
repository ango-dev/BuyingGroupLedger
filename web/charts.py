"""Server-rendered donut charts for the overview: inline SVG, no JavaScript, every segment a link.

A donut here is a part-to-whole of ROW COUNTS (rows by status, by retailer, by buying group), and
each segment is an <a href="/orders?...">: clicking a slice applies that filter on the Orders page.
Arcs are stroked circles with a dash array, which keeps the geometry trivial and the segments
separated by a real gap of surface colour (the 2px spacer the data-viz method asks for).

COLOUR RULES (the data-viz method): the STATUS donut wears the Sheet's own status hues, stepped
stronger so they read on a thin ring but recognisably the row colours; the retailer and buying-group
donuts wear the categorical palette in FIXED slot order (a name keeps its colour whatever else is
present, never re-cycled), and past the eighth distinct name the rest fold into "Other". Text is
never coloured by series; identity is carried by the legend and the direct label beside every
count, so it is never colour-alone.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from urllib.parse import urlencode

from models.order import STATUSES

#: The Sheet's status colours, stepped for a ring (row backgrounds are the pastel originals).
STATUS_COLORS = {
    "ordered": "#cc4b4b", "shipped": "#e0731f", "delivered": "#c9a800", "paid": "#4f9d3a",
    "return": "#b8472f", "cancelled": "#9a9a9a", "superseded": "#4d4d4d",
}
#: Categorical slots, fixed order (the data-viz reference palette; dark steps in style.css).
CATEGORICAL = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7",
               "#e34948")
OTHER_COLOR = "#8a8a8a"
MAX_SLOTS = len(CATEGORICAL)


@dataclass(frozen=True)
class Segment:
    label: str
    count: int
    href: str  # "" for a fold-up slice that maps to no single filter
    color: str
    #: Stroke geometry for a circle of circumference `circumference`.
    dash: float
    offset: float
    share: float  # 0..1


@dataclass(frozen=True)
class Donut:
    title: str
    total: int
    segments: list[Segment]
    size: int = 168
    stroke: int = 22

    @property
    def radius(self) -> float:
        return (self.size - self.stroke) / 2

    @property
    def circumference(self) -> float:
        return 2 * math.pi * self.radius

    @property
    def center(self) -> float:
        return self.size / 2


def _orders_href(param: str, value: str) -> str:
    return "/orders?" + urlencode({param: value})


def donut(title: str, counts: list[tuple[str, int]], *, param: str,
          colors: dict[str, str] | None = None, size: int = 168, stroke: int = 22,
          gap: float = 2.0) -> Donut:
    """Build a donut from (label, count) pairs, already in the order they should be drawn.

    `colors` maps a label to a fixed colour (the status donut); otherwise labels take the
    categorical slots in the order given, and everything past MAX_SLOTS folds into "Other".
    """
    items = [(label, int(n)) for label, n in counts if int(n) > 0]
    if colors is None and len(items) > MAX_SLOTS:
        head, tail = items[: MAX_SLOTS - 1], items[MAX_SLOTS - 1:]
        items = head + [("Other", sum(n for _l, n in tail))]
    total = sum(n for _l, n in items)
    shell = Donut(title=title, total=total, segments=[], size=size, stroke=stroke)
    circumference = shell.circumference
    segments: list[Segment] = []
    consumed = 0.0
    for index, (label, n) in enumerate(items):
        share = n / total if total else 0.0
        length = circumference * share
        # A real gap between slices, but never eat a slice smaller than the gap itself, and no
        # gap at all on a lone full ring.
        visible = length - gap if len(items) > 1 and length > gap * 2 else length
        if colors is not None:
            color = colors.get(label, OTHER_COLOR)
        else:
            color = CATEGORICAL[index] if label != "Other" else OTHER_COLOR
        href = "" if label == "Other" else _orders_href(param, label if label != "(blank)" else "")
        segments.append(Segment(label=label, count=n, href=href, color=color, dash=round(visible, 3),
                                offset=round(-consumed, 3), share=share))
        consumed += length
    return Donut(title=title, total=total, segments=segments, size=size, stroke=stroke)


def status_donut(status_counts: list[tuple[str, int]]) -> Donut:
    """Rows by status, in the ledger's lifecycle order, in the Sheet's colours."""
    order = {s: i for i, s in enumerate(STATUSES)}
    ordered = sorted(status_counts, key=lambda item: order.get(item[0], len(order)))
    return donut("Rows by status", ordered, param="status", colors=STATUS_COLORS)


# --------------------------------------------------------------------------------------------------
# Stacked bars: open rows by buying group, segmented by status
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BarSegment:
    label: str  # the status
    count: int
    href: str
    color: str
    x: float
    width: float


@dataclass(frozen=True)
class Bar:
    label: str  # the buying group
    total: int
    href: str
    segments: list[BarSegment]
    y: float


@dataclass(frozen=True)
class StackedBars:
    title: str
    bars: list[Bar]
    legend: list[tuple[str, str]]  # (status, colour)
    width: int
    height: int
    label_width: int
    bar_height: int
    total: int


def open_rows_bars(open_table: dict, *, width: int = 360, bar_height: int = 18, gap: int = 8,
                   label_width: int = 96, spacer: float = 2.0) -> StackedBars:
    """One horizontal bar per buying group (largest first), stacked by status in lifecycle order,
    from summary.overview's `open_table`. Every segment links to /orders?status=..&group=..,
    the bar's label to the group alone. A 2px surface gap separates stacked segments."""
    groups = list(open_table["groups"])
    statuses = [r["status"] for r in open_table["rows"]]
    matrix = {r["status"]: dict(zip(groups, r["cells"])) for r in open_table["rows"]}
    totals = {g: sum(matrix[s][g] for s in statuses) for g in groups}
    ordered = sorted(groups, key=lambda g: (-totals[g], g))
    scale_max = max(totals.values(), default=0) or 1
    plot_width = width - label_width - 40  # room for the total at the right
    bars: list[Bar] = []
    for row_index, group in enumerate(ordered):
        y = row_index * (bar_height + gap)
        x = float(label_width)
        segments: list[BarSegment] = []
        for status in statuses:
            n = matrix[status][group]
            if n <= 0:
                continue
            w = plot_width * n / scale_max
            visible = max(w - spacer, 0.5) if w > spacer * 2 else w
            group_param = "" if group == "(blank)" else group
            href = "/orders?" + urlencode({"status": status, "group": group_param})
            segments.append(BarSegment(label=status, count=n, href=href,
                                       color=STATUS_COLORS.get(status, OTHER_COLOR), x=round(x, 2),
                                       width=round(visible, 2)))
            x += w
        bars.append(Bar(label=group, total=totals[group],
                        href="/orders?" + urlencode({"group": "" if group == "(blank)" else group}),
                        segments=segments, y=y))
    height = max(len(ordered) * (bar_height + gap) - gap, bar_height)
    legend = [(s, STATUS_COLORS.get(s, OTHER_COLOR)) for s in statuses]
    return StackedBars(title="Open Rows", bars=bars, legend=legend,
                       width=width, height=height, label_width=label_width, bar_height=bar_height,
                       total=int(open_table.get("total", 0)))
