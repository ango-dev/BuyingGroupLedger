"""Who may reach the dashboard, and who may send it a write.

Three rules, each a middleware in web/app.py:

  * THE HOST. A request is answered only under a name the operator knows: an IP address,
    localhost, the host of `web.public_url`, or one listed in `web.allowed_hosts`. A web page
    that re-points its own domain at this machine (DNS rebinding) reaches the dashboard under
    ITS domain name, which is none of those, so it is refused before it can read a page.

  * THE ORIGIN. A write (anything but GET / HEAD / OPTIONS) is refused when the browser says it
    came from another site. A page on any website the operator happens to visit could otherwise
    submit a form to http://127.0.0.1:8765 -- the dashboard's own address on the operator's own
    machine -- and without a password nothing stops it. Browsers attest `Sec-Fetch-Site`, which
    page script cannot forge; older ones send `Origin`, compared against the Host. A request with
    neither is not a browser's cross-site form, so it passes (curl, the test client).

  * THE FIRST PASSWORD. Reachable beyond this machine with no password set, the dashboard is
    anyone's: the backups hold every credential. So it serves only a page that sets the password,
    and that page wants a one-time token printed to the dashboard's log -- which only the person
    who can read the host's logs has.
"""

from __future__ import annotations

import ipaddress
from typing import Iterable, Mapping
from urllib.parse import urlsplit

#: Names always answered: loopback by name, and the name Starlette's test client sends.
ALWAYS_ALLOWED = ("localhost", "testserver")
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def hostname(host: str) -> str:
    """The host part of a Host header (or a bind address): lower case, no port, no brackets."""
    text = str(host or "").strip().lower()
    if text.startswith("["):
        return text[1:text.find("]")] if "]" in text else text[1:]
    if text.count(":") == 1:  # name:port or v4:port; a bare IPv6 address has several colons
        text = text.split(":", 1)[0]
    return text.rstrip(".")


def is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def is_loopback(host: str) -> bool:
    """True for an address only this machine can reach (127.x, ::1, localhost)."""
    name = hostname(host)
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def parse_hosts(text) -> tuple[str, ...]:
    """`web.allowed_hosts` as names: commas or spaces between them, a leading dot = any subdomain."""
    return tuple(h.strip().lower().rstrip(".") for h in str(text or "").replace(",", " ").split() if h.strip())


def allowed_names(allowed_hosts, public_url: str = "") -> tuple[str, ...]:
    names = list(parse_hosts(allowed_hosts))
    public = (urlsplit(str(public_url or "")).hostname or "").lower()
    if public:
        names.append(public)
    return tuple(names)


def _named(name: str, allowed: Iterable[str]) -> bool:
    for entry in allowed:
        if entry.startswith("."):
            if name == entry[1:] or name.endswith(entry):
                return True
        elif name == entry:
            return True
    return False


def host_allowed(host_header: str, allowed: Iterable[str]) -> bool:
    name = hostname(host_header)
    if not name or is_ip(name) or name in ALWAYS_ALLOWED or name.endswith(".localhost"):
        return True
    return _named(name, allowed)


def cross_site(method: str, headers: Mapping[str, str], allowed: Iterable[str] = ()) -> bool:
    """True when a write request came from another site's page."""
    if method.upper() in SAFE_METHODS:
        return False
    site = str(headers.get("sec-fetch-site", "") or "").strip().lower()
    if site:
        return site not in ("same-origin", "none")  # "none": typed or bookmarked, not a page's doing
    origin = headers.get("origin")
    if origin is None:
        return False
    origin = str(origin).strip().lower()
    if origin == "null":  # a sandboxed frame or a file: page -- never the dashboard's own
        return True
    parts = urlsplit(origin)
    if parts.netloc == str(headers.get("host", "") or "").strip().lower():
        return False
    # Behind a reverse proxy the Host can be rewritten; an origin the operator named is theirs.
    return not (parts.hostname and _named(parts.hostname, allowed))
