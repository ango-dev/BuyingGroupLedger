"""models/numbers.py: the ONE rule for a number a person types.
Before it, `1e3` reached the ledger as 13, `2.5` as 2 in a whole-number column, `nan` reached
the tax summary and a Settings sign-in length."""

from __future__ import annotations

import pytest

from models.numbers import MOST_MAGNITUDE, NumberError, parse_number, whole_number


class TestShapes:
    @pytest.mark.parametrize("text, expected", [
        ("3", 3.0), ("-3", -3.0), ("+3", 3.0), ("1,230.50", 1230.5), ("$1,230.50", 1230.5),
        ("$ 12", 12.0), ("(5)", -5.0), ("($5)", -5.0), (".5", 0.5), ("-$5", -5.0), ("$-5", -5.0),
        ("  7  ", 7.0), ("1,000,000", 1_000_000.0), (12, 12.0), (2.5, 2.5),
    ])
    def test_what_a_person_writes_is_read(self, text, expected):
        assert parse_number(text) == expected

    @pytest.mark.parametrize("text", [
        "1e3", "1E3", "0x10", "1o0", "12abc", "3 units", "nan", "NaN", "inf", "-inf", "infinity",
        "1_000", "10^30", "1,23", "1,2345", "--3", "+-3", "3-", "$", "%", ".", "1e400", "١٢",
        "１２",  # fullwidth digits: not the ledger's
        True, None,
    ])
    def test_everything_else_is_refused_by_name(self, text):
        with pytest.raises(NumberError) as info:
            parse_number(text)
        assert "is not a number" in str(info.value) or "required" in str(info.value)

    def test_blank_is_required_not_zero(self):
        with pytest.raises(NumberError, match="required"):
            parse_number("")
        with pytest.raises(NumberError, match="required"):
            parse_number("   ")


class TestPercentWholeAndBounds:
    def test_percent_only_where_asked(self):
        assert parse_number("4%", percent=True) == 0.04
        assert parse_number("0.04", percent=True) == 0.04
        assert parse_number("12.5 %", percent=True) == 0.125
        with pytest.raises(NumberError, match="no % here"):
            parse_number("4%")

    def test_a_whole_number_is_never_rounded(self):
        assert whole_number("3") == 3 and isinstance(whole_number("3"), int)
        assert whole_number("3.0") == 3
        with pytest.raises(NumberError, match="'2.5' is not a whole number"):
            whole_number("2.5")
        with pytest.raises(NumberError, match="'abc' is not a whole number"):
            whole_number("abc")

    def test_bounds_are_inclusive_and_named(self):
        assert parse_number("0", least=0) == 0 and parse_number("1", percent=True, most=1) == 1
        with pytest.raises(NumberError, match="must be at least 1"):
            whole_number("0", least=1)
        with pytest.raises(NumberError, match="must be at most 100%"):
            parse_number("150%", percent=True, most=1)
        with pytest.raises(NumberError, match="must be at least 0%"):
            parse_number("-4%", percent=True, least=0)

    def test_the_magnitude_cap_keeps_sqlite_integers_safe(self):
        assert parse_number(str(MOST_MAGNITUDE)) == MOST_MAGNITUDE
        with pytest.raises(NumberError, match="too large"):
            parse_number("99999999999999999999")
        with pytest.raises(NumberError, match="too large"):
            parse_number(float("1e300"))
        with pytest.raises(NumberError, match="is not a number"):
            parse_number(float("nan"))
