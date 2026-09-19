"""The dashboard's sign-in.

ONE PASSWORD, `web.password` / WEB_PASSWORD. Blank = no sign-in, the dashboard as it was (loopback
or a private network is then the whole protection, as before). Set, every page except the login
page, the static assets and /health (the container's healthcheck probes it; it carries no ledger
data) needs a session cookie.

THE COOKIE IS A SIGNED, EXPIRING TOKEN, not a server-side session table: `<kind>.<expires>.<hmac>`,
signed with a random secret the app keeps in `.state.json` (`web.session_secret`, made on first use
-- the state file is what the app rewrites, and a restart must not sign everyone out of a two-year
"remember me"). The signature also covers a fingerprint of the password, so changing the password
signs every device out without touching the secret. Nothing is stored per session, so there is
nothing to prune and nothing a backup has to carry but the secret.

THE RATE LIMIT is per client address: `web.login_attempts` wrong passwords (5) lock that address
out for `web.login_lockout_minutes` (15); a right password resets it, and a lock is recorded as an
activity ALERT, since an internet-facing dashboard being guessed at is exactly what the overview's
attention cards are for. In memory: a dashboard restart forgets the counts, which is fine -- the
lock is a brake on guessing, not a ban list.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Callable

__all__ = ["COOKIE", "LoginLimiter", "Sessions", "describe", "safe_next", "session_secret"]

#: The cookie's name.
COOKIE = "ledger_session"

_KINDS = {"s": "session", "r": "remembered"}


def describe(seconds: float) -> str:
    """A duration as the login page and the settings say it: 730 days -> "2 years", 6 hours ->
    "6 hours", 90 minutes -> "90 minutes". Whole units only, the largest that divides."""
    seconds = float(seconds)
    for unit, label in ((365 * 86400, "year"), (86400, "day"), (3600, "hour"), (60, "minute")):
        count = seconds / unit
        if count >= 1 and abs(count - round(count)) < 1e-9:
            count = int(round(count))
            return f"{count} {label}{'' if count == 1 else 's'}"
    if seconds >= 3600:
        return f"{seconds / 3600:g} hours"
    return f"{max(seconds, 0) / 60:g} minutes"


def safe_next(value: str | None) -> str:
    """Where to land after signing in: a path on this dashboard, never another host (`//evil`
    or `http://evil` would make the login page an open redirect)."""
    value = (value or "").strip()
    if not value.startswith("/") or value.startswith("//") or value.startswith("/\\"):
        return "/"
    if value.startswith("/login") or value.startswith("/logout"):
        return "/"
    return value


def session_secret() -> bytes:
    """The signing secret from `.state.json` (`web.session_secret`), made on first use."""
    from config import loader

    state = loader.load_state()
    web = state.get("web")
    if not isinstance(web, dict):
        web = {}
        state["web"] = web
    value = web.get("session_secret")
    if not isinstance(value, str) or len(value) < 32:
        value = secrets.token_hex(32)
        web["session_secret"] = value
        loader.save_state(state)
    return value.encode("ascii")


@dataclass(frozen=True)
class Sessions:
    """Issues and checks the signed session tokens."""

    secret: bytes
    password: str
    session_hours: float = 6
    remember_days: float = 730
    clock: Callable[[], float] = time.time

    def _fingerprint(self) -> str:
        return hashlib.sha256(self.password.encode("utf-8")).hexdigest()

    def _sign(self, kind: str, expires: int) -> str:
        message = f"{kind}.{expires}.{self._fingerprint()}".encode("ascii")
        return hmac.new(self.secret, message, hashlib.sha256).hexdigest()

    def lifetime(self, remember: bool) -> int:
        """Seconds a new token lives: the remember-me days, or the session hours."""
        seconds = self.remember_days * 86400 if remember else self.session_hours * 3600
        return max(int(seconds), 60)

    def issue(self, remember: bool) -> tuple[str, int]:
        """A new token and how long it lives (the cookie's max-age), in seconds."""
        kind = "r" if remember else "s"
        lifetime = self.lifetime(remember)
        expires = int(self.clock()) + lifetime
        return f"{kind}.{expires}.{self._sign(kind, expires)}", lifetime

    def verify(self, token: str | None) -> str:
        """"session" / "remembered" for a live, correctly signed token, "" otherwise."""
        if not token:
            return ""
        parts = token.split(".")
        if len(parts) != 3 or parts[0] not in _KINDS or not parts[1].isdigit():
            return ""
        kind, expires, signature = parts[0], int(parts[1]), parts[2]
        if not hmac.compare_digest(signature, self._sign(kind, expires)):
            return ""
        if expires <= self.clock():
            return ""
        return _KINDS[kind]


class LoginLimiter:
    """Wrong passwords per client address: `max_attempts` of them within a lockout window lock
    the address out for `lockout_minutes`; a right password clears it."""

    def __init__(self, max_attempts: int = 5, lockout_minutes: float = 15,
                 clock: Callable[[], float] = time.time) -> None:
        self.max_attempts = max(int(max_attempts), 1)
        self.lockout_seconds = max(float(lockout_minutes), 0) * 60
        self.clock = clock
        self._state: dict[str, dict] = {}  # address -> {"failures", "last", "locked_until"}

    def retry_after(self, address: str) -> float:
        """Seconds until this address may try again; 0 = now."""
        entry = self._state.get(address)
        if not entry:
            return 0
        return max(0.0, entry["locked_until"] - self.clock())

    def failed(self, address: str) -> int:
        """Record a wrong password. Returns the attempts left before the lock; 0 = locked now."""
        now = self.clock()
        entry = self._state.get(address)
        if entry and entry["locked_until"] > now:
            return 0  # still locked: nothing to count
        # A lock that ran out, or a failure older than the window, starts the count over.
        if not entry or now - entry["last"] > self.lockout_seconds:
            entry = {"failures": 0, "last": now, "locked_until": 0.0}
            self._state[address] = entry
        entry["failures"] += 1
        entry["last"] = now
        left = self.max_attempts - entry["failures"]
        if left <= 0:
            entry["locked_until"] = now + self.lockout_seconds
            entry["failures"] = 0
            left = 0
        self._prune(now)
        return left

    def succeeded(self, address: str) -> None:
        self._state.pop(address, None)

    def _prune(self, now: float) -> None:
        if len(self._state) < 1000:
            return
        stale = [a for a, e in self._state.items()
                 if e["locked_until"] <= now and now - e["last"] > self.lockout_seconds]
        for address in stale:
            del self._state[address]
