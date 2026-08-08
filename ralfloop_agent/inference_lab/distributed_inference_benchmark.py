from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import statistics
import time
from typing import Any, Callable


CONFIGURATIONS = (
    "llama_cpp_baseline",
    "remote_token_speculation",
    "distributed_dspark",
    "local_dspark_existing",
)


@dataclass(frozen=True)
class InferencePrompt:
    id: str
    category: str
    prompt: str
    max_tokens: int


def load_inference_dataset(path: str | Path) -> list[InferencePrompt]:
    rows: list[InferencePrompt] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        rows.append(InferencePrompt(str(data["id"]), str(data["category"]), str(data["prompt"]), int(data["max_tokens"])))
    expected = {"short", "medium", "long", "code", "json"}
    if {row.category for row in rows} != expected or any(sum(item.category == category for item in rows) != 5 for category in expected):
        raise ValueError("inference_dataset_must_have_five_per_category")
    return rows


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [row for row in samples if row.get("ok")]
    result: dict[str, Any] = {"count": len(samples), "success_count": len(successful), "error_rate": 1 - len(successful) / len(samples) if samples else 0.0}
    for key in ("ttft_ms", "wall_ms", "tokens_per_second", "acceptance_rate"):
        values = [float(row[key]) for row in successful if isinstance(row.get(key), (int, float))]
        result[key] = {
            "median": statistics.median(values) if values else None,
            "mean": statistics.mean(values) if values else None,
            "min": min(values) if values else None,
            "max": max(values) if values else None,
        }
    return result


def run_configuration(
    name: str,
    dataset: list[InferencePrompt],
    runner: Callable[[InferencePrompt], dict[str, Any]] | None,
    *,
    warmups: int = 3,
    measured_runs: int = 5,
    unavailable_reason: str | None = None,
) -> dict[str, Any]:
    if name not in CONFIGURATIONS:
        raise ValueError("unknown_inference_configuration")
    if runner is None:
        return {"configuration": name, "status": "not_feasible", "reason": unavailable_reason or "runner_unavailable", "samples": [], "summary": summarize([])}
    for prompt in dataset:
        for _ in range(warmups):
            runner(prompt)
    samples: list[dict[str, Any]] = []
    for prompt in dataset:
        for index in range(measured_runs):
            started = time.monotonic()
            try:
                row = dict(runner(prompt))
                row.setdefault("ok", True)
                row.setdefault("wall_ms", (time.monotonic() - started) * 1000)
            except Exception as exc:
                row = {"ok": False, "error": type(exc).__name__, "wall_ms": (time.monotonic() - started) * 1000}
            row.update({"configuration": name, "prompt_id": prompt.id, "category": prompt.category, "run": index + 1})
            samples.append(row)
    return {"configuration": name, "status": "completed", "warmups": warmups, "measured_runs": measured_runs, "samples": samples, "summary": summarize(samples)}


def write_manifest(path: str | Path, payload: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    safe = {key: value for key, value in payload.items() if key.lower() not in {"prompt", "secret", "token", "authorization"}}
    destination.write_text(json.dumps(safe, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
