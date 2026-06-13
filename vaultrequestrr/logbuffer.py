"""In-memory ring buffer of recent log records, surfaced by the web dashboard."""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class LogRecordView:
    created: float
    level: str
    name: str
    message: str


class RingBufferHandler(logging.Handler):
    def __init__(self, capacity: int = 1000) -> None:
        super().__init__(level=logging.NOTSET)
        self.buffer: deque[LogRecordView] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buffer.append(
                LogRecordView(
                    created=record.created,
                    level=record.levelname,
                    name=record.name,
                    message=self.format(record),
                )
            )
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)


_handler: RingBufferHandler | None = None


def install(capacity: int = 1000) -> RingBufferHandler:
    """Attach the ring buffer to the root logger (idempotent)."""
    global _handler
    if _handler is None:
        _handler = RingBufferHandler(capacity)
        # Include exception tracebacks; keep the line compact otherwise.
        _handler.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger().addHandler(_handler)
    return _handler


def get_records() -> list[LogRecordView]:
    return list(_handler.buffer) if _handler is not None else []
