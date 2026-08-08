"""Shared primitives: provenance, freshness, and source references.

Every fact that enters the scoring engine carries where it came from and when
it was true. Missing data is represented as ``None`` plus a
:class:`DataQualityFlag` -- never estimated or back-filled.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import DataProvider, DataQualitySeverity


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Base(BaseModel):
    model_config = ConfigDict(use_enum_values=False, validate_assignment=True, extra="forbid")


T = TypeVar("T")


class Provenance(Base):
    """Where a value came from and when."""

    provider: DataProvider
    endpoint: str | None = None
    as_of: datetime | None = Field(
        default=None, description="Timestamp the value was true according to the provider."
    )
    retrieved_at: datetime = Field(default_factory=utcnow)
    request_id: str | None = None

    def age_seconds(self, now: datetime | None = None) -> float | None:
        if self.as_of is None:
            return None
        return ((now or utcnow()) - self.as_of).total_seconds()


class Observed(Base, Generic[T]):
    """A single measured value plus its provenance.

    Used wherever staleness or attribution matters to a scoring rule.
    """

    value: T | None
    provenance: Provenance
    stale: bool = False

    @property
    def present(self) -> bool:
        return self.value is not None


class SourceReference(Base):
    """A citable external source. Agents must attach one to any factual claim."""

    title: str
    url: str | None = None
    publisher: str | None = None
    published_at: datetime | None = None
    retrieved_at: datetime = Field(default_factory=utcnow)
    tickers: list[str] = Field(default_factory=list)
    excerpt: str | None = None


class DataQualityFlag(Base):
    """Something the system noticed about its own inputs.

    These are persisted; a scan with many flags is itself a signal.
    """

    code: str
    severity: DataQualitySeverity = DataQualitySeverity.WARNING
    message: str
    provider: DataProvider | None = None
    ticker: str | None = None
    field: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class MissingData(Base):
    """Explicit record that a field could not be obtained.

    The system treats missing data as missing. It never substitutes a
    default, an average, or a model-generated guess.
    """

    field: str
    provider: DataProvider | None = None
    reason: str


__all__ = [
    "Base",
    "DataQualityFlag",
    "MissingData",
    "Observed",
    "Provenance",
    "SourceReference",
    "new_id",
    "utcnow",
]
