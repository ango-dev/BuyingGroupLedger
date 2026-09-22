"""One rule for a number a person types into the dashboard.

Every typed number -- a grid cell, the add row, a tax amount, a dated log's entry, a settings
field -- used to go through its own `float()` or a decoration-stripping parser, and each one let
something through that no one typed on purpose: `1e3` stored as 13 (the letter dropped, the digits
kept), `0x10` as 10, `1o0` as 10, `2.5` truncated to 2 in a whole-number column, `nan` on the tax
summary as "$nan", and a `nan` sign-in length that made every /login a 500 after the restart.
This module is the one parser they
all use now. It reads exactly the shapes a person writes -- digits, a decimal point, thousands
commas, a sign, a `$`, a `%`, accounting parentheses -- and refuses everything else with a message
that names the text, so a typo is a refusal rather than a quietly different number.

`parse_number` returns a float, or an int when `integer` is asked for (a fraction is then
refused, never rounded). `percent=True` reads "4%" as 0.04 and "0.04" as 0.04; the bounds are
inclusive and checked after the percent conversion. Non-finite values cannot arise from the
grammar, but are refused all the same, and every magnitude is capped at MOST_MAGNITUDE so an
integer column never meets SQLite's 64-bit limit.
"""

from __future__ import annotations

import math
import re

__all__ = ["MOST_MAGNITUDE", "NumberError", "parse_number", "whole_number"]

#: Larger than any ledger amount, smaller than what SQLite refuses to store as an INTEGER.
MOST_MAGNITUDE = 1_000_000_000_000

#: Digits with optional thousands commas (all groups of three once a comma appears), an optional
#: fraction; at least one digit somewhere.
_PLAIN = re.compile(r"^(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)?(?:\.[0-9]*)?$")  # ASCII digits only


class NumberError(ValueError):
    """The text is not a number a person would write, or is out of the bounds asked for."""


def parse_number(text, *, integer: bool = False, percent: bool = False,
                 least: float | None = None, most: float | None = None):
    """The number `text` spells, or NumberError.

    Accepted: "3", "-3", "+3", "1,230.50", "$1,230.50", "$ 12", "(5)" (accounting negative,
    -5), ".5", "4%" (with `percent`: 0.04). Refused, naming the text: letters anywhere
    ("1e3", "0x10", "1o0", "3 units", "nan", "inf"), underscores, a second sign, a comma in the
    wrong place, a fraction where a whole number is asked for, and anything outside
    [least, most] or beyond MOST_MAGNITUDE.
    """
    if isinstance(text, bool):
        raise NumberError(f"{text!r} is not a number")
    if isinstance(text, (int, float)):
        number = float(text)
        shown = repr(text)
    else:
        shown = str(text if text is not None else "").strip()
        body = shown.replace(" ", "")
        if not body:
            raise NumberError("a number is required")
        negative = False
        if body.startswith("(") and body.endswith(")"):
            negative, body = True, body[1:-1]
        sign = ""
        if body and body[0] in "+-":
            sign, body = body[0], body[1:]
        if body.startswith("$"):
            body = body[1:]
            if body and body[0] in "+-" and not sign:  # "$-5" is how some statements print it
                sign, body = body[0], body[1:]
        is_percent = body.endswith("%")
        if is_percent:
            body = body[:-1]
        if not body or not _PLAIN.match(body) or not any(c in "0123456789" for c in body):
            raise NumberError(f"{shown!r} is not a {'whole number' if integer else 'number'}")
        number = float(body.replace(",", ""))
        if sign == "-" or negative:
            number = -number
        if is_percent:
            if not percent:
                raise NumberError(f"{shown!r} is not a number (no % here)")
            number /= 100
    if math.isnan(number) or math.isinf(number):
        raise NumberError(f"{shown!r} is not a number")
    if abs(number) > MOST_MAGNITUDE:
        raise NumberError(f"{shown!r} is too large a number")
    if integer:
        if number != int(number):
            raise NumberError(f"{shown!r} is not a whole number")
        number = int(number)
    if least is not None and number < least:
        raise NumberError(f"{shown!r} is below {_bound(least, percent)}: must be at least {_bound(least, percent)}")
    if most is not None and number > most:
        raise NumberError(f"{shown!r} is above {_bound(most, percent)}: must be at most {_bound(most, percent)}")
    return number


def whole_number(text, *, least: int | None = None, most: int | None = None) -> int:
    """parse_number(integer=True) for the common case."""
    return parse_number(text, integer=True, least=least, most=most)


def _bound(value: float, percent: bool) -> str:
    if percent:
        return f"{value * 100:g}%"
    return f"{value:g}"
