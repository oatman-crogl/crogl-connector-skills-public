"""Pure-Python Darktrace DTAPI signing helpers.

The algorithm and accepted date formats are documented by Darktrace. This file
has no Crogl dependencies so it can be unit-tested independently of the
injected connector runtime.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone


def utc_date() -> str:
    """Return the compact UTC date format accepted by Darktrace."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def signature(private_token: str, request: str, public_token: str, date: str) -> str:
    """Return HMAC-SHA1(private, request + LF + public + LF + date)."""
    message = f"{request}\n{public_token}\n{date}".encode("ascii")
    return hmac.new(private_token.encode("ascii"), message, hashlib.sha1).hexdigest()


def signed_headers(private_token: str, request: str, public_token: str, date: str) -> dict[str, str]:
    """Build the DTAPI headers for one exact request."""
    return {
        "DTAPI-Token": public_token,
        "DTAPI-Date": date,
        "DTAPI-Signature": signature(private_token, request, public_token, date),
    }
