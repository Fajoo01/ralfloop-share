from __future__ import annotations

from collections import Counter
from threading import Lock
from typing import Mapping


class OperationalMetrics:
    """Process-local counters; exporters may scrape snapshots without mutation hooks."""

    def __init__(self) -> None:
        self._values: Counter[str] = Counter()
        self._lock = Lock()

    def increment(self, name: str, value: int = 1) -> None:
        if not name or value < 0:
            raise ValueError("metric_increment_invalid")
        with self._lock:
            self._values[name] += value

    def snapshot(self) -> Mapping[str, int]:
        with self._lock:
            return dict(self._values)


__all__ = ["OperationalMetrics"]
