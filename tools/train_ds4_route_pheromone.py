#!/usr/bin/env python3
"""Train DS4 V4.1 cross-layer expert prefetch priors with pheromone dynamics.

The output is byte-compatible with the BTMTRN1 route-predictor format already
consumed by the experimental DS4 V4.1 prefetch path.  Deterministic model
routing remains authoritative: this file only predicts what is worth loading
early.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
import struct
import time
from typing import Iterable

try:
    import numpy as np
except ImportError as exc:  # pragma: no cover - CLI dependency guard
    raise SystemExit("train_ds4_route_pheromone.py requires NumPy") from exc

MAGIC = b"BTMTRN1\0"
VERSION = 1


@dataclass(frozen=True)
class FeedbackSpec:
    route: Path
    outcome: str = "success"
    quality: float = 1.0
    latency_ms: float | None = None
    observed_at: float = 0.0


def load_trace(path: Path, layers: int) -> list[dict[int, dict[int, list[int]]]]:
    chunks: list[dict[int, dict[int, list[int]]]] = []
    rows: dict[int, dict[int, list[int]]] = {}
    last_layer: int | None = None
    with path.open(encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                layer, row, _slot, expert = map(int, line.split(","))
            except ValueError as exc:
                raise ValueError(f"invalid route trace {path}:{line_number}") from exc
            if not (0 <= layer < layers) or row < 0:
                raise ValueError(f"route trace index out of range {path}:{line_number}")
            if last_layer is not None and layer == 0 and last_layer != 0 and rows:
                if len(rows) == layers:
                    chunks.append(rows)
                rows = {}
            rows.setdefault(layer, {}).setdefault(row, []).append(expert)
            last_layer = layer
    if len(rows) == layers:
        chunks.append(rows)
    return chunks


def transition_counts(
    chunks: Iterable[dict[int, dict[int, list[int]]]],
    layer: int,
    horizon: int,
    experts: int,
) -> np.ndarray:
    x_idx: list[int] = []
    y_idx: list[int] = []
    for chunk in chunks:
        src_rows = chunk.get(layer) or {}
        dst_rows = chunk.get(layer + horizon) or {}
        for row in sorted(set(src_rows) & set(dst_rows)):
            xs = [value for value in src_rows[row] if 0 <= value < experts]
            ys = [value for value in dst_rows[row] if 0 <= value < experts]
            if not xs or not ys:
                continue
            x_idx.extend(value for value in xs for _ in ys)
            y_idx.extend(value for _ in xs for value in ys)
    counts = np.zeros((experts, experts), dtype=np.float32)
    if x_idx:
        np.add.at(
            counts,
            (np.asarray(x_idx, dtype=np.intp), np.asarray(y_idx, dtype=np.intp)),
            1.0,
        )
    return counts


def _feedback_from_manifest(path: Path) -> list[FeedbackSpec]:
    specs: list[FeedbackSpec] = []
    base = path.parent
    with path.open(encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid manifest JSON {path}:{line_number}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"manifest row must be an object {path}:{line_number}")
            route = Path(str(item.get("route") or ""))
            if not route.is_absolute():
                route = (base / route).resolve()
            outcome = str(item.get("outcome") or "success").strip().lower()
            if outcome not in {"success", "failure", "timeout"}:
                raise ValueError(f"invalid outcome {path}:{line_number}")
            quality = min(max(float(item.get("quality", 1.0)), 0.0), 1.0)
            raw_latency = item.get("latency_ms")
            latency_ms = None if raw_latency is None else max(float(raw_latency), 0.0)
            observed_at = float(item.get("observed_at") or route.stat().st_mtime)
            specs.append(
                FeedbackSpec(
                    route=route,
                    outcome=outcome,
                    quality=quality,
                    latency_ms=latency_ms,
                    observed_at=observed_at,
                )
            )
    return specs


def _route_specs(paths: list[Path]) -> list[FeedbackSpec]:
    return [
        FeedbackSpec(route=path, observed_at=path.stat().st_mtime)
        for path in paths
    ]


def train(
    specs: list[FeedbackSpec],
    *,
    output: Path,
    horizon: int = 2,
    layers: int = 40,
    experts: int = 384,
    smoothing: float = 0.01,
    half_life_seconds: float = 21_600.0,
    success_deposit: float = 1.0,
    failure_retention: float = 0.50,
    timeout_retention: float = 0.25,
    latency_reference_ms: float = 5_000.0,
    now: float | None = None,
) -> dict[str, object]:
    if not specs:
        raise ValueError("at least one route trace is required")
    if horizon < 1 or horizon >= layers:
        raise ValueError("horizon must be between 1 and layers-1")
    if layers < 2 or experts < 1 or smoothing <= 0:
        raise ValueError("invalid layers/experts/smoothing")
    if half_life_seconds <= 0 or latency_reference_ms <= 0:
        raise ValueError("half-life and latency reference must be positive")
    if not 0 <= failure_retention <= 1 or not 0 <= timeout_retention <= 1:
        raise ValueError("failure retention values must be in [0, 1]")
    for spec in specs:
        if not spec.route.is_file():
            raise ValueError(f"missing route trace: {spec.route}")

    n_layers = layers - horizon
    matrix = np.full((n_layers, experts, experts), smoothing, dtype=np.float32)
    ordered = sorted(specs, key=lambda item: (item.observed_at, str(item.route)))
    previous_at = ordered[0].observed_at
    stats = {
        "success": 0,
        "failure": 0,
        "timeout": 0,
        "transition_observations": 0,
        "route_traces": len(ordered),
    }

    for spec in ordered:
        elapsed = max(0.0, spec.observed_at - previous_at)
        if elapsed:
            retention = math.exp(-math.log(2.0) * elapsed / half_life_seconds)
            matrix[:] = smoothing + (matrix - smoothing) * retention
        previous_at = max(previous_at, spec.observed_at)
        chunks = load_trace(spec.route, layers)
        latency_factor = (
            1.0
            if spec.latency_ms is None
            else 1.0 / (1.0 + spec.latency_ms / latency_reference_ms)
        )
        deposit = success_deposit * spec.quality * latency_factor
        for layer in range(n_layers):
            counts = transition_counts(chunks, layer, horizon, experts)
            observed = int(counts.sum())
            if not observed:
                continue
            stats["transition_observations"] += observed
            if spec.outcome == "success":
                matrix[layer] += counts * deposit
            else:
                route_retention = (
                    timeout_retention if spec.outcome == "timeout" else failure_retention
                )
                mask = counts > 0
                matrix[layer][mask] = np.maximum(
                    smoothing,
                    matrix[layer][mask] * route_retention,
                )
        stats[spec.outcome] += 1

    target_now = time.time() if now is None else float(now)
    final_elapsed = max(0.0, target_now - previous_at)
    if final_elapsed:
        retention = math.exp(-math.log(2.0) * final_elapsed / half_life_seconds)
        matrix[:] = smoothing + (matrix - smoothing) * retention

    row_sums = matrix.sum(axis=2, keepdims=True)
    matrix /= row_sums
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as handle:
        handle.write(MAGIC)
        handle.write(struct.pack("<4I", VERSION, horizon, n_layers, experts))
        handle.write(matrix.astype("<f4", copy=False).tobytes())

    return {
        **stats,
        "output": str(output),
        "bytes": output.stat().st_size,
        "version": VERSION,
        "horizon": horizon,
        "layers": layers,
        "matrix_layers": n_layers,
        "experts": experts,
        "half_life_seconds": half_life_seconds,
        "policy_boundary": "prefetch_prediction_only_model_router_authoritative",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--route", action="append", default=[], type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--stats-json", type=Path)
    parser.add_argument("--horizon", type=int, default=2)
    parser.add_argument("--layers", type=int, default=40)
    parser.add_argument("--experts", type=int, default=384)
    parser.add_argument("--smoothing", type=float, default=0.01)
    parser.add_argument("--half-life-seconds", type=float, default=21_600.0)
    parser.add_argument("--success-deposit", type=float, default=1.0)
    parser.add_argument("--failure-retention", type=float, default=0.50)
    parser.add_argument("--timeout-retention", type=float, default=0.25)
    parser.add_argument("--latency-reference-ms", type=float, default=5_000.0)
    args = parser.parse_args(argv)

    specs = _route_specs(args.route)
    if args.manifest:
        specs.extend(_feedback_from_manifest(args.manifest))
    if not specs:
        parser.error("provide --route and/or --manifest")

    result = train(
        specs,
        output=args.output,
        horizon=args.horizon,
        layers=args.layers,
        experts=args.experts,
        smoothing=args.smoothing,
        half_life_seconds=args.half_life_seconds,
        success_deposit=args.success_deposit,
        failure_retention=args.failure_retention,
        timeout_retention=args.timeout_retention,
        latency_reference_ms=args.latency_reference_ms,
    )
    rendered = json.dumps(result, sort_keys=True, indent=2) + "\n"
    print(rendered, end="")
    if args.stats_json:
        args.stats_json.parent.mkdir(parents=True, exist_ok=True)
        args.stats_json.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
