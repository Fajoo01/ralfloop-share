from __future__ import annotations

from dataclasses import dataclass
import math
import re


PING_TIME_RE = re.compile(r"time[=<]([0-9.]+)\s*ms")
PING_LOSS_RE = re.compile(r"([0-9.]+)% packet loss")


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


@dataclass(frozen=True)
class NetworkMetrics:
    samples: int
    p50_ms: float
    p95_ms: float
    jitter_ms: float
    packet_loss_pct: float

    def to_dict(self) -> dict[str, float | int]:
        return {
            "samples": self.samples,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "jitter_ms": self.jitter_ms,
            "packet_loss_pct": self.packet_loss_pct,
        }


def parse_ping_output(output: str) -> NetworkMetrics:
    samples = [float(match.group(1)) for match in PING_TIME_RE.finditer(output)]
    mean = sum(samples) / len(samples) if samples else 0.0
    jitter = (sum((value - mean) ** 2 for value in samples) / len(samples)) ** 0.5 if samples else 0.0
    loss_match = PING_LOSS_RE.search(output)
    return NetworkMetrics(
        samples=len(samples),
        p50_ms=percentile(samples, 0.50),
        p95_ms=percentile(samples, 0.95),
        jitter_ms=jitter,
        packet_loss_pct=float(loss_match.group(1)) if loss_match else 100.0,
    )


def throughput_mbps(byte_count: int, elapsed_sec: float) -> float:
    if byte_count < 0 or elapsed_sec <= 0:
        raise ValueError("invalid_throughput_sample")
    return byte_count * 8 / elapsed_sec / 1_000_000
