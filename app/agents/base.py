"""Agent tracing.

Every agent invocation produces an :class:`AgentRunRecord`: what went in, what
came out, which providers were touched, what was missing, what failed. That
record is persisted, and it is what makes "why was this recommended?"
answerable after the fact.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from pydantic import Field

from app.models.common import Base, new_id, utcnow
from app.models.enums import AgentName, AgentStatus


class AgentRunRecord(Base):
    agent_run_id: str = Field(default_factory=lambda: new_id("agrun"))
    run_id: str
    agent: AgentName
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None
    duration_ms: float | None = None
    status: AgentStatus = AgentStatus.SUCCESS

    llm_backend: str = "scripted"
    reasoning_mode: str = Field(
        default="heuristic",
        description="'llm' when a model produced the output, 'heuristic' when the "
        "documented offline fallback did.",
    )

    input_summary: dict[str, Any] = Field(default_factory=dict)
    output_summary: dict[str, Any] = Field(default_factory=dict)
    tools_used: list[str] = Field(default_factory=list)
    providers_queried: list[str] = Field(default_factory=list)
    providers_failed: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


@contextmanager
def trace(run_id: str, agent: AgentName, llm_backend: str, reasoning_mode: str):
    """Wrap an agent body so a record is produced whether it succeeds or not."""
    record = AgentRunRecord(
        run_id=run_id, agent=agent, llm_backend=llm_backend, reasoning_mode=reasoning_mode
    )
    started = time.perf_counter()
    try:
        yield record
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
        record.status = AgentStatus.FAILED
        record.errors.append(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        record.ended_at = utcnow()
        record.duration_ms = round((time.perf_counter() - started) * 1000, 2)
        if record.status is AgentStatus.SUCCESS and (record.errors or record.providers_failed):
            record.status = AgentStatus.PARTIAL


__all__ = ["AgentRunRecord", "trace"]
