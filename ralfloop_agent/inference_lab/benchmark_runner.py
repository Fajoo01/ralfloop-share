from __future__ import annotations

from dataclasses import dataclass
import statistics
import time
from typing import Any, Callable

from ralfloop_agent.providers.chat import ChatProvider


@dataclass(frozen=True)
class BenchmarkPrompt:
    id: str
    text: str
    category: str


def _one_stream(provider: ChatProvider, prompt: BenchmarkPrompt) -> dict[str, Any]:
    started = time.monotonic()
    first_token_at: float | None = None
    output: list[str] = []
    metadata: dict[str, Any] = {}
    error: str | None = None
    try:
        for chunk in provider.stream_chat([{"role": "user", "content": prompt.text}]):
            if chunk.text:
                if first_token_at is None:
                    first_token_at = time.monotonic()
                output.append(chunk.text)
            if chunk.done:
                metadata = chunk.metadata
                break
    except Exception as exc:
        error = getattr(exc, "code", type(exc).__name__)
    ended = time.monotonic()
    text = "".join(output)
    elapsed = ended - started
    token_estimate = len(text.split())
    return {
        "prompt_id": prompt.id,
        "category": prompt.category,
        "context_bytes": len(prompt.text.encode("utf-8")),
        "ttft_ms": (first_token_at - started) * 1000 if first_token_at else None,
        "wall_ms": elapsed * 1000,
        "output_token_estimate": token_estimate,
        "token_estimate_per_sec": token_estimate / elapsed if elapsed else 0.0,
        "empty_output": not bool(text),
        "error": error,
        "metadata": metadata,
    }


def run_benchmark(
    provider_factory: Callable[[], ChatProvider],
    prompts: list[BenchmarkPrompt],
    *,
    warmups: int = 3,
    measured_runs: int = 5,
) -> dict[str, Any]:
    provider = provider_factory()
    if prompts:
        for _ in range(max(0, warmups)):
            _one_stream(provider, prompts[0])
    samples = [_one_stream(provider, prompt) for prompt in prompts for _ in range(max(1, measured_runs))]
    ttft = [sample["ttft_ms"] for sample in samples if sample["ttft_ms"] is not None]
    throughput = [sample["token_estimate_per_sec"] for sample in samples]
    return {
        "warmups": warmups,
        "measured_runs": measured_runs,
        "samples": samples,
        "summary": {
            "ttft_median_ms": statistics.median(ttft) if ttft else None,
            "token_estimate_per_sec_median": statistics.median(throughput) if throughput else 0.0,
            "error_rate": sum(sample["error"] is not None for sample in samples) / len(samples) if samples else 0.0,
        },
    }
