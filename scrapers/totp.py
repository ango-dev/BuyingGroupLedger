"""RFC 6238 time-based one-time passwords, on the standard library.

Best Buy's 2-Step Verification screen says "Enter the code from your authenticator app", so signing in
unattended means generating that code ourselves from the enrolled secret. Written against the stdlib
rather than adding a dependency: it is ~20 lines of HMAC, and `pyotp` is only present transitively
here, so importing it would be relying on something nothing declares.

Correctness is pinned by RFC 6238's own published test vectors (tests/test_totp.py) — the one place
where "looks right" is worthless, because a wrong code is indistinguishable from a wrong password at
the sign-in screen.
"""

import base64
import hashlib
import hmac
import struct
import time

DEFAULT_PERIOD = 30
DEFAULT_DIGITS = 6


class TotpError(ValueError):
    """The configured secret is not usable base32 — a typo in config, not a runtime condition."""


def normalize_secret(secret: str) -> str:
    """Accept a secret as a human pastes it: lowercase, spaced in groups, padding stripped.

    Authenticator enrolment screens show the key in spaced groups ("abcd efgh ..."), and base32
    decoding is case-sensitive and length-fussy, so both are normalised here rather than at every
    call site.
    """
    cleaned = "".join(str(secret or "").split()).upper().replace("-", "")
    if not cleaned:
        raise TotpError("empty TOTP secret")
    padding = "=" * (-len(cleaned) % 8)
    return cleaned + padding


def totp(secret: str, *, for_time: float | None = None, digits: int = DEFAULT_DIGITS,
         period: int = DEFAULT_PERIOD, digest=hashlib.sha1) -> str:
    """The current code for `secret`, zero-padded to `digits`.

    Zero padding matters: a code like "005914" is six characters, and str(int) would submit "5914".
    """
    try:
        key = base64.b32decode(normalize_secret(secret))
    except (ValueError, TypeError) as exc:
        raise TotpError(f"TOTP secret is not valid base32: {exc}") from None
    counter = int((time.time() if for_time is None else for_time) // period)
    mac = hmac.new(key, struct.pack(">Q", counter), digest).digest()
    offset = mac[-1] & 0x0F
    code = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def seconds_remaining(*, for_time: float | None = None, period: int = DEFAULT_PERIOD) -> float:
    """How long the current code stays valid. Used to avoid submitting one that expires mid-flight."""
    now = time.time() if for_time is None else for_time
    return period - (now % period)
