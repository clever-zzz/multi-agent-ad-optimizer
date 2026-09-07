"""Persistence access objects.

Repositories are the only layer allowed to build SQLAlchemy statements. Agents
and services depend on these interfaces, never on the ORM directly, which is
what makes them unit-testable without a database.
"""

from .alerts import AlertRepository
from .audit import ABTestRepository, AuditRepository, IdempotencyRepository
from .base import BaseRepository
from .campaigns import CampaignRepository, CreativeRepository, MetricRepository
from .runs import RunRepository
from .users import UserRepository

__all__ = [
    "ABTestRepository",
    "AlertRepository",
    "AuditRepository",
    "BaseRepository",
    "CampaignRepository",
    "CreativeRepository",
    "IdempotencyRepository",
    "MetricRepository",
    "RunRepository",
    "UserRepository",
]
