from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

GIB = 1024 ** 3


@dataclass(frozen=True)
class Thresholds:
    warn_percent: float = 85.0
    critical_percent: float = 92.0
    warn_free_gib: float = 100.0
    critical_free_gib: float = 60.0

    @classmethod
    def from_env(cls) -> "Thresholds":
        return cls(
            warn_percent=float(os.getenv("STORAGE_WARN_PERCENT", cls.warn_percent)),
            critical_percent=float(os.getenv("STORAGE_CRITICAL_PERCENT", cls.critical_percent)),
            warn_free_gib=float(os.getenv("STORAGE_WARN_FREE_GIB", cls.warn_free_gib)),
            critical_free_gib=float(os.getenv("STORAGE_CRITICAL_FREE_GIB", cls.critical_free_gib)),
        )

    def validate(self) -> None:
        if not (0 <= self.warn_percent <= self.critical_percent <= 100):
            raise ValueError("percent thresholds must satisfy 0 <= warn <= critical <= 100")
        if not (0 <= self.critical_free_gib <= self.warn_free_gib):
            raise ValueError("free-space thresholds must satisfy 0 <= critical <= warn")


@dataclass(frozen=True)
class DiskSample:
    path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    used_percent: float
    free_gib: float
    severity: str


def classify(used_percent: float, free_gib: float, thresholds: Thresholds) -> str:
    thresholds.validate()
    if used_percent >= thresholds.critical_percent or free_gib <= thresholds.critical_free_gib:
        return "critical"
    if used_percent >= thresholds.warn_percent or free_gib <= thresholds.warn_free_gib:
        return "warning"
    return "ok"


def sample_path(path: str | Path, thresholds: Thresholds) -> DiskSample:
    resolved = str(Path(path).resolve())
    usage = shutil.disk_usage(resolved)
    used_percent = (usage.used / usage.total * 100.0) if usage.total else 0.0
    free_gib = usage.free / GIB
    return DiskSample(
        path=resolved,
        total_bytes=usage.total,
        used_bytes=usage.used,
        free_bytes=usage.free,
        used_percent=round(used_percent, 2),
        free_gib=round(free_gib, 2),
        severity=classify(used_percent, free_gib, thresholds),
    )


def parse_paths(raw: str | None) -> list[str]:
    if not raw:
        return ["/"]
    paths = [item.strip() for item in raw.split(",") if item.strip()]
    return paths or ["/"]


def build_report(paths: Iterable[str], thresholds: Thresholds) -> dict:
    thresholds.validate()
    samples: list[DiskSample] = []
    errors: list[dict[str, str]] = []
    for path in paths:
        try:
            samples.append(sample_path(path, thresholds))
        except OSError as exc:
            errors.append({"path": path, "error": str(exc)})

    severities = {sample.severity for sample in samples}
    overall = (
        "critical"
        if errors or "critical" in severities
        else "warning"
        if "warning" in severities
        else "ok"
    )
    critical_paths = [sample.path for sample in samples if sample.severity == "critical"]
    trigger = None
    if critical_paths:
        trigger = {
            "type": "disk_purchase_research",
            "reason": "storage_critical",
            "paths": critical_paths,
            "requested_action": "research_replacement_or_expansion_disks",
        }

    return {
        "host": socket.gethostname(),
        "overall": overall,
        "thresholds": asdict(thresholds),
        "filesystems": [asdict(sample) for sample in samples],
        "errors": errors,
        "research_trigger": trigger,
    }


def exit_code(report: dict) -> int:
    return {"ok": 0, "warning": 1, "critical": 2}[report["overall"]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check filesystem capacity and emit a machine-readable alert.")
    parser.add_argument("--paths", default=os.getenv("STORAGE_CHECK_PATHS", "/"), help="Comma-separated paths/mounts")
    parser.add_argument("--warn-percent", type=float, default=None)
    parser.add_argument("--critical-percent", type=float, default=None)
    parser.add_argument("--warn-free-gib", type=float, default=None)
    parser.add_argument("--critical-free-gib", type=float, default=None)
    args = parser.parse_args(argv)

    base = Thresholds.from_env()
    thresholds = Thresholds(
        warn_percent=args.warn_percent if args.warn_percent is not None else base.warn_percent,
        critical_percent=args.critical_percent if args.critical_percent is not None else base.critical_percent,
        warn_free_gib=args.warn_free_gib if args.warn_free_gib is not None else base.warn_free_gib,
        critical_free_gib=args.critical_free_gib if args.critical_free_gib is not None else base.critical_free_gib,
    )
    report = build_report(parse_paths(args.paths), thresholds)
    print(json.dumps(report, indent=2, sort_keys=True))
    return exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
