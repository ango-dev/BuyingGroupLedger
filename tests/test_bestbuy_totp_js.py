"""Correctness of the in-browser TOTP snippet the agent runs for Best Buy 2FA.

The agent computes the 6-digit code in the page (Web Crypto), not in Python, because the prompt is
built once at run start and the code is only needed minutes later — a Python-generated code would be
stale. That makes _TOTP_JS load-bearing and un-obvious, so we execute it with Node (same JS engine
family) against the RFC 6238 Appendix B SHA-1 test vectors and check it returns the published codes.

Skipped automatically if Node isn't installed.
"""

import shutil
import subprocess

import pytest

from scrapers.bestbuy import _TOTP_JS

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

# base32("12345678901234567890"), the RFC 6238 Appendix B SHA-1 seed.
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
# (unix time T, expected last-6-digits of the published 8-digit TOTP).
VECTORS = [
    (59, "287082"),
    (1111111109, "081804"),
    (1111111111, "050471"),
    (1234567890, "005924"),
    (2000000000, "279037"),
    (20000000000, "353130"),
]


def _run_totp_at(unix_time: int) -> str:
    body = _TOTP_JS.replace("__SECRET__", RFC_SECRET)
    script = (
        "if(!globalThis.crypto){globalThis.crypto=require('node:crypto').webcrypto;}"
        f"Date.now=()=>{unix_time*1000};"
        f"(async()=>{{ {body} }})().then(c=>process.stdout.write(c))"
        ".catch(e=>{console.error(e);process.exit(1);});"
    )
    out = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, f"node failed: {out.stderr}"
    return out.stdout.strip()


@pytest.mark.parametrize("unix_time,expected", VECTORS)
def test_totp_matches_rfc6238(unix_time, expected):
    assert _run_totp_at(unix_time) == expected
