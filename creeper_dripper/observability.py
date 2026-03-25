from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

LOGGER = logging.getLogger("creeper_dripper.observability")


@dataclass(slots=True)
class Event:
    event_type: str
    reason_code: str
    metadata: dict[str, Any] = field(default_factory=dict)


class EventCollector:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event_type: str, reason_code: str, **metadata: Any) -> None:
        event = Event(event_type=event_type, reason_code=reason_code, metadata=metadata)
        self.events.append(event)
        LOGGER.info("event=%s reason=%s metadata=%s", event_type, reason_code, metadata)

    def to_dicts(self) -> list[dict[str, Any]]:
        return [{"event_type": e.event_type, "reason_code": e.reason_code, "metadata": e.metadata} for e in self.events]
