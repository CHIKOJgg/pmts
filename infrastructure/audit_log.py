"""infrastructure/audit_log.py — Immutable audit logging for trading actions."""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class AuditEvent:
    timestamp: int
    event_type: str
    operator: str
    details: Dict[str, Any]
    event_id: str = ""

    def __post_init__(self) -> None:
        if not self.event_id:
            self.event_id = f"{self.timestamp}-{self.event_type}-{id(self)}"


class AuditLogger:
    def __init__(self, log_dir: str = "audit_logs", max_file_size_mb: int = 100) -> None:
        self._log_dir = log_dir
        self._max_file_size = max_file_size_mb * 1024 * 1024
        os.makedirs(log_dir, exist_ok=True)
        self._current_file: Optional[str] = None
        self._current_size: int = 0
        self._event_count: int = 0

    def log(self, event_type: str, operator: str = "system", **details: Any) -> AuditEvent:
        event = AuditEvent(
            timestamp=int(time.time() * 1000),
            event_type=event_type,
            operator=operator,
            details=details,
        )
        self._write_event(event)
        self._event_count += 1
        return event

    def _write_event(self, event: AuditEvent) -> None:
        if self._current_file is None or self._current_size >= self._max_file_size:
            self._rotate_file()

        line = json.dumps(asdict(event), separators=(",", ":")) + "\n"
        assert self._current_file is not None
        with open(self._current_file, "a") as f:
            f.write(line)
        self._current_size += len(line.encode("utf-8"))

    def _rotate_file(self) -> None:
        timestamp = int(time.time())
        self._current_file = os.path.join(self._log_dir, f"audit_{timestamp}.log")
        self._current_size = 0
        if os.path.exists(self._current_file):
            self._current_file = os.path.join(self._log_dir, f"audit_{timestamp}_{os.getpid()}.log")
            self._current_size = os.path.getsize(self._current_file)

    def get_events(self, event_type: Optional[str] = None, limit: int = 100) -> list[AuditEvent]:
        events = []
        log_files = sorted(
            [f for f in os.listdir(self._log_dir) if f.startswith("audit_") and f.endswith(".log")],
            reverse=True,
        )
        for filename in log_files:
            filepath = os.path.join(self._log_dir, filename)
            with open(filepath, "r") as f:
                for line in f:
                    if line.strip():
                        try:
                            data = json.loads(line)
                            if event_type is None or data.get("event_type") == event_type:
                                events.append(AuditEvent(**data))
                                if len(events) >= limit:
                                    return events
                        except json.JSONDecodeError:
                            continue
        return events

    @property
    def event_count(self) -> int:
        return self._event_count
