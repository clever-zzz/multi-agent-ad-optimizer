"""Identifier helpers.

Prefixed, lexicographically sortable ULIDs keep ids readable in logs and URL
safe while remaining globally unique without a database round-trip.
"""

from __future__ import annotations

import secrets
import string
import time
import uuid

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_TIME_CHARS = 10
_RANDOM_CHARS = 16
_RANDOM_BYTES = 10


def _encode(value: int, length: int) -> str:
    """Encode an integer as fixed-width Crockford base32."""
    chars: list[str] = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    chars.reverse()
    return "".join(chars)


def ulid() -> str:
    """Return a 26-character ULID string."""
    timestamp = int(time.time() * 1000) & ((1 << 48) - 1)
    randomness = int.from_bytes(secrets.token_bytes(_RANDOM_BYTES), "big")
    return _encode(timestamp, _TIME_CHARS) + _encode(randomness, _RANDOM_CHARS)


def new_id(prefix: str) -> str:
    """Return a prefixed identifier, for example run_01J8ZK..."""
    return prefix + "_" + ulid()


def correlation_id() -> str:
    """Return an opaque correlation identifier for request tracing."""
    return uuid.uuid4().hex


_ALPHANUMERIC = string.ascii_letters + string.digits


def random_token(length: int = 48) -> str:
    """Return a URL-safe random token for API keys and idempotency values."""
    return "".join(secrets.choice(_ALPHANUMERIC) for _ in range(length))
