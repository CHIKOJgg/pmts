from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class LatencySample:
    operation: str
    duration_ms: float
    timestamp: float
    success: bool


class LatencyTracker:
    def __init__(self, window_size: int = 1000) -> None:
        self._window_size = window_size
        self._samples: Dict[str, deque[LatencySample]] = {}

    def record(self, operation: str, duration_ms: float, success: bool = True) -> None:
        if operation not in self._samples:
            self._samples[operation] = deque(maxlen=self._window_size)
        self._samples[operation].append(
            LatencySample(
                operation=operation,
                duration_ms=duration_ms,
                timestamp=time.time(),
                success=success,
            )
        )

    def _samples_for(self, operation: str) -> list[LatencySample]:
        return list(self._samples.get(operation, []))

    def avg_ms(self, operation: str) -> Optional[float]:
        samples = self._samples_for(operation)
        if not samples:
            return None
        return sum(s.duration_ms for s in samples) / len(samples)

    def p50_ms(self, operation: str) -> Optional[float]:
        samples = self._samples_for(operation)
        if not samples:
            return None
        sorted_durations = sorted(s.duration_ms for s in samples)
        return sorted_durations[len(sorted_durations) // 2]

    def p99_ms(self, operation: str) -> Optional[float]:
        samples = self._samples_for(operation)
        if not samples:
            return None
        sorted_durations = sorted(s.duration_ms for s in samples)
        idx = int(len(sorted_durations) * 0.99)
        return sorted_durations[min(idx, len(sorted_durations) - 1)]

    def error_rate(self, operation: str) -> float:
        samples = self._samples_for(operation)
        if not samples:
            return 0.0
        errors = sum(1 for s in samples if not s.success)
        return errors / len(samples)

    def total_calls(self, operation: str) -> int:
        return len(self._samples_for(operation))

    def summary(self, operation: str) -> str:
        avg = self.avg_ms(operation)
        p50 = self.p50_ms(operation)
        p99 = self.p99_ms(operation)
        err = self.error_rate(operation)
        total = self.total_calls(operation)
        avg_s = f"{avg:.1f}" if avg is not None else "N/A"
        p50_s = f"{p50:.1f}" if p50 is not None else "N/A"
        p99_s = f"{p99:.1f}" if p99 is not None else "N/A"
        return (
            f"{operation}: {total} calls, "
            f"avg={avg_s}ms p50={p50_s}ms p99={p99_s}ms "
            f"err={err:.1%}"
        )

    @property
    def operations(self) -> list[str]:
        return list(self._samples.keys())

    def all_summaries(self) -> str:
        lines = ["─── Latency Report ───"]
        for op in sorted(self.operations):
            lines.append(f"  {self.summary(op)}")
        if not self.operations:
            lines.append("  No data")
        lines.append("─────────────────────")
        return "\n".join(lines)


class Timer:
    def __init__(self, tracker: LatencyTracker, operation: str) -> None:
        self._tracker = tracker
        self._operation = operation
        self._start: float = 0.0
        self._success = True

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        duration_ms = (time.perf_counter() - self._start) * 1000
        self._tracker.record(self._operation, duration_ms, success=exc_type is None)
