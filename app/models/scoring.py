"""Score representation.

The design goal is auditability: every point is traceable to a named rule and
the measurement that triggered it. If you cannot explain a score by reading
:class:`ScoreBreakdown`, the scoring engine has a bug.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, computed_field

from app.models.common import Base, new_id, utcnow
from app.models.enums import Classification, RejectionReasonCode


class ScoreReason(Base):
    """One rule firing: what it measured, and what it awarded."""

    rule: str
    points: float
    measurement: str = Field(description="The observed value, verbatim, e.g. 'RSI14=61.2'.")
    detail: str | None = None


class ScoreComponent(Base):
    name: str
    points: float
    max_points: float
    reasons: list[ScoreReason] = Field(default_factory=list)
    unscored_due_to_missing_data: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pct(self) -> float:
        if self.max_points <= 0:
            return 0.0
        return round(self.points / self.max_points * 100.0, 1)

    def explain(self) -> str:
        parts = [f"{r.rule} {r.points:+.1f} ({r.measurement})" for r in self.reasons]
        return f"{self.name} {self.points:.1f}/{self.max_points:.0f}: " + "; ".join(parts)


class HardRejection(Base):
    code: RejectionReasonCode
    rule: str
    message: str
    measurement: str | None = None
    threshold: str | None = None


class ScoreBreakdown(Base):
    score_id: str = Field(default_factory=lambda: new_id("score"))
    run_id: str
    candidate_id: str
    structure_id: str | None = None
    computed_at: datetime = Field(default_factory=utcnow)
    methodology_version: str
    methodology_fingerprint: str

    components: list[ScoreComponent] = Field(default_factory=list)
    hard_rejections: list[HardRejection] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total(self) -> float:
        return round(sum(c.points for c in self.components), 2)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def max_total(self) -> float:
        return round(sum(c.max_points for c in self.components), 2)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def hard_rejected(self) -> bool:
        return bool(self.hard_rejections)

    def component(self, name: str) -> ScoreComponent | None:
        return next((c for c in self.components if c.name == name), None)

    def audit_lines(self) -> list[str]:
        lines: list[str] = []
        for c in self.components:
            lines.append(f"{c.name}: {c.points:.1f}/{c.max_points:.0f}")
            for r in c.reasons:
                lines.append(f"    {r.points:+6.1f}  {r.rule}  [{r.measurement}]")
            for m in c.unscored_due_to_missing_data:
                lines.append(f"      0.0  {m} (data unavailable -- no credit given)")
        for hr in self.hard_rejections:
            lines.append(f"HARD REJECT [{hr.code.value}] {hr.message}")
        return lines


class ScoredCandidate(Base):
    """A candidate after validation, structuring, rules, and scoring."""

    run_id: str
    candidate_id: str
    ticker: str
    breakdown: ScoreBreakdown
    classification: Classification
    classification_label: str
    presentable: bool
    rank: int | None = None
    rejection_summary: list[str] = Field(default_factory=list)


__all__ = [
    "HardRejection",
    "ScoreBreakdown",
    "ScoreComponent",
    "ScoreReason",
    "ScoredCandidate",
]
