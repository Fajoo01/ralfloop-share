from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable, Iterator

from .domain_opinion import DomainReasoningInput, parse_domain_opinion, validate_domain_opinion
from .recursive_mas_domain_prompts import (
    build_domain_critic_prompt_with_slot,
    build_domain_planner_prompt,
    build_domain_planner_prompt_with_feedback_slot,
    build_domain_solver_prompt_with_slots,
)
from .recursive_mas_profiles import DOMAIN_PROFILE, MATH_PROFILE, checkpoint_status, get_profile


UPSTREAM_ROOT = Path("/home/sibilla-cumana/RecursiveMAS")


def request_from_dict(payload: dict[str, Any]) -> DomainReasoningInput:
    return DomainReasoningInput(
        domain_id=str(payload.get("domain_id") or ""),
        domain_version=str(payload.get("domain_version") or ""),
        question=str(payload.get("question") or ""),
        facts=tuple(dict(item) for item in payload.get("facts", [])),
        rules=tuple(dict(item) for item in payload.get("rules", [])),
        sources=tuple(dict(item) for item in payload.get("sources", [])),
        constraints=tuple(str(item) for item in payload.get("constraints", [])),
        known_contradictions=tuple(dict(item) for item in payload.get("known_contradictions", [])),
        required_output=dict(payload.get("required_output") or {}),
        reason_codes=tuple(str(item) for item in payload.get("reason_codes", [])),
    )


def run_domain_request(
    payload: dict[str, Any],
    *,
    executor_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    request = request_from_dict(dict(payload.get("request") or {}))
    profile_id = str(payload.get("profile_id") or DOMAIN_PROFILE.profile_id)
    checkpoint_mode = str(payload.get("checkpoint_mode") or "prompt_only_math_checkpoints")
    if profile_id != DOMAIN_PROFILE.profile_id:
        raise ValueError("domain_worker_requires_domain_profile")
    if checkpoint_mode not in {"prompt_only_math_checkpoints", "domain_checkpoints"}:
        raise ValueError("invalid_checkpoint_mode")
    if checkpoint_mode == "domain_checkpoints" and not checkpoint_status(profile_id)["ready"]:
        raise FileNotFoundError("domain_checkpoint_incomplete")

    from ralfloop_agent.integration import recursive_mas_worker as native

    root = Path(str(payload.get("upstream_root") or UPSTREAM_ROOT)).resolve()
    native._prepare_import_path(root)
    factory = executor_factory or native.StagewiseGPUExecutor
    executor = factory(root=root, style="sequential_light", device="cuda:0", dtype="float16")
    executor.guard.install()
    rounds = max(1, min(int(payload.get("rounds") or 2), 3))
    canonical_input = json.dumps(request.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    try:
        executor.load_cpu_resident_system()
        if checkpoint_mode == "domain_checkpoints":
            _load_domain_adapters(executor.system, get_profile(profile_id))
        with patched_domain_runtime(native):
            raw = executor.run(question=canonical_input, rounds=rounds, profile_name="domain_reasoning_canary")
        opinion = parse_domain_opinion(raw.get("raw_answer"))
        validation = validate_domain_opinion(opinion, request)
        return {
            "ok": bool(raw.get("native_execution_success")),
            "profile_id": profile_id,
            "checkpoint_mode": checkpoint_mode,
            "output": raw.get("raw_answer"),
            "validation": validation,
            "native_latent_verified": bool(raw.get("native_latent_verified")),
            "closed_loop_verified": bool(raw.get("closed_loop_verified")),
            "one_model_at_a_time_verified": bool(raw.get("one_model_at_a_time_verified")),
            "intermediate_decode_count": int((raw.get("instrumentation") or {}).get("intermediate_decode_count") or 0),
            "final_decode_count": int((raw.get("instrumentation") or {}).get("final_decode_count") or 0),
            "duration_ms": raw.get("duration_ms"),
            "stage_summary": raw.get("stage_summary") or {},
            "memory": raw.get("cpu_resident_load") or {},
            "network_attempted": bool(raw.get("network_attempted")),
            "download_attempted": bool(raw.get("download_attempted")),
        }
    finally:
        executor.unload()


@contextmanager
def patched_domain_runtime(native: Any) -> Iterator[None]:
    from inference_utils import inference_mas as base  # type: ignore

    replacements = {
        "build_math_planner_prompt": build_domain_planner_prompt,
        "build_math_planner_prompt_with_feedback_slot": build_domain_planner_prompt_with_feedback_slot,
        "build_math_refiner_prompt_with_slot": build_domain_critic_prompt_with_slot,
        "build_math_solver_prompt_with_slots": build_domain_solver_prompt_with_slots,
    }
    originals = {name: getattr(base, name) for name in replacements}
    original_profile = native._generation_profile

    def generation_profile(name: str) -> dict[str, Any]:
        if name != "domain_reasoning_canary":
            return original_profile(name)
        return {
            "name": name,
            "do_sample": False,
            "temperature": 0.6,
            "top_p": 0.95,
            "latent_length": 32,
            "max_new_tokens": 768,
            "batch_size": 1,
            "enable_thinking": False,
            "seed": 42,
            "dtype": "float16",
        }

    try:
        for name, replacement in replacements.items():
            setattr(base, name, replacement)
        native._generation_profile = generation_profile
        yield
    finally:
        native._generation_profile = original_profile
        for name, original in originals.items():
            setattr(base, name, original)


def _load_domain_adapters(system: Any, profile: Any) -> None:
    import torch

    for role, path in profile.inner_adapter_checkpoints.items():
        state = torch.load(path, map_location="cpu", weights_only=True)
        system.agents[role].inner_adapter.load_state_dict(state, strict=True)
    for name, path in profile.outer_adapter_checkpoints.items():
        state = torch.load(path, map_location="cpu", weights_only=True)
        system.outer_adapters[name].load_state_dict(state, strict=True)


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        result = run_domain_request(payload)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("ok") else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "error": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
