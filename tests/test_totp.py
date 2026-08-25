"""RFC 6238 conformance for the TOTP generator.

Pinned to the RFC's own published vectors because a wrong code is indistinguishable from a wrong
password at Best Buy's sign-in screen — it would be diagnosed as a credential problem and send someone
resetting a password that was never wrong.
"""

import hashlib

import pytest

from scrapers.totp import TotpError, normalize_secret, seconds_remaining, totp

# RFC 6238 Appendix B: the SHA-1 seed is the ASCII "12345678901234567890".
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


@pytest.mark.parametrize("when, expected", [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
])
def test_rfc6238_sha1_vectors(when, expected):
    assert totp(RFC_SECRET, for_time=when, digits=8, digest=hashlib.sha1) == expected


def test_six_digits_is_the_last_six_of_the_rfc_vector():
    # Authenticator apps show 6 digits; the RFC publishes 8, and 6 is the same number truncated.
    assert totp(RFC_SECRET, for_time=59, digits=6) == "287082"


def test_a_code_is_zero_padded_not_shortened():
    """str(int) would submit a 5-character code for any value below 100000 and be rejected as wrong
    — the failure would look like a bad secret rather than a formatting bug."""
    code = totp(RFC_SECRET, for_time=1111111109, digits=6)
    assert code == "081804" and len(code) == 6


def test_the_code_is_stable_within_its_window_and_changes_across_it():
    # Anchored to a window BOUNDARY (1111111110 % 30 == 0). Picking an arbitrary instant would land
    # near the end of a window and make "+20s" cross into the next one.
    start = 1111111110
    a = totp(RFC_SECRET, for_time=start)
    assert totp(RFC_SECRET, for_time=start + 20) == a, "same 30s window -> same code"
    assert totp(RFC_SECRET, for_time=start + 40) != a, "next window -> new code"


@pytest.mark.parametrize("messy", [
    "gezd gnbv gy3t qojq gezd gnbv gy3t qojq",   # as an enrolment screen displays it
    "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
    "gezdgnbvgy3tqojqgezdgnbvgy3tqojq",
])
def test_a_secret_is_accepted_however_it_was_pasted(messy):
    assert totp(messy, for_time=59, digits=8) == "94287082"


def test_a_secret_needing_padding_still_decodes():
    # 26 chars: valid base32 once padded, which is what a real enrolment key often looks like.
    assert len(totp("JBSWY3DPEHPK3PXPJBSWY3DPEH", for_time=59)) == 6


@pytest.mark.parametrize("bad", ["", "   ", "not-base32-!!"])
def test_an_unusable_secret_says_so_rather_than_returning_a_wrong_code(bad):
    # Silently returning a wrong code would be diagnosed as a bad password.
    with pytest.raises(TotpError):
        totp(bad)


def test_seconds_remaining_tracks_the_window():
    assert seconds_remaining(for_time=1020) == pytest.approx(30), "at a boundary, a full window"
    assert seconds_remaining(for_time=1049) == pytest.approx(1), "one second before it rolls"
