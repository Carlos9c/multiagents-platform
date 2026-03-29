from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class OrchestratorTraceEvent(BaseModel):
    timestamp_utc: str
    event_type: str
    step_count: int
    task_id: int
    payload: dict[str, Any] = Field(default_factory=dict)


class OrchestratorTrace(BaseModel):
    task_id: int
    events: list[OrchestratorTraceEvent] = Field(default_factory=list)

    def add_event(
        self,
        *,
        event_type: str,
        step_count: int,
        task_id: int,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.events.append(
            OrchestratorTraceEvent(
                timestamp_utc=datetime.now(timezone.utc).isoformat(),
                event_type=event_type,
                step_count=step_count,
                task_id=task_id,
                payload=payload or {},
            )
        )

    def to_notes(self) -> list[str]:
        notes: list[str] = []
        for event in self.events:
            notes.append(f"[{event.timestamp_utc}] {event.event_type}: {event.payload}")
        return notes
