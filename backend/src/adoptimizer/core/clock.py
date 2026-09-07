"""The single sanctioned source of "now".

``date.today()`` and ``datetime.now()`` read the server's local timezone, so the
same request would bucket into a different day depending on where the process
happens to run. Every metric window, retention cutoff and seed date in this
codebase is a UTC day, so these helpers are the only approved way to ask for the
current instant.
"""

from __future__ import annotations

from datetime import UTC, date, datetime


def utcnow() -> datetime:
    """Timezone-aware current instant."""
    return datetime.now(UTC)


def utc_today() -> date:
    """Current UTC calendar day, the bucket every metric window is keyed on."""
    return datetime.now(UTC).date()


def as_utc(value: datetime) -> datetime:
    """Normalise a timestamp that came back out of the database to aware UTC.

    ``DateTime(timezone=True)`` is honoured by PostgreSQL but silently ignored by
    SQLite, which hands back naive values. Every stored instant in this codebase
    is UTC wall-clock time, so a naive value can be tagged rather than converted.
    Comparisons against :func:`utcnow` must not depend on which engine is
    deployed, and without this they raise ``TypeError`` on SQLite.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


__all__ = ["as_utc", "utc_today", "utcnow"]
