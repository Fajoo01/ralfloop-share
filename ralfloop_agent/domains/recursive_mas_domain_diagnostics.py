from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable

from .domain_opinion import parse_domain_opinion


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_verified_checkpoint(
    module: Any,
    path: str | Path,
    expected_sha256: str,
    *,
    loader: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    import torch

    checkpoint = Path(path).resolve()
    observed = sha256_file(checkpoint)
    if observed != expected_sha256:
        raise RuntimeError("checkpoint_hash_mismatch")
    load = loader or torch.load
    state = load(checkpoint, map_location="cpu", weights_only=True)
    module.load_state_dict(state, strict=True)
    parameters = list(module.parameters())
    return {
        "configured_path": str(path),
        "opened_path": str(checkpoint),
        "expected_sha256": expected_sha256,
        "loaded_sha256": observed,
        "parameter_count": sum(item.numel() for item in parameters),
        "trainable_parameter_count": sum(item.numel() for item in parameters if item.requires_grad),
        "dtype": sorted({str(item.dtype) for item in parameters}),
        "device": sorted({str(item.device) for item in parameters}),
        "mtime": checkpoint.stat().st_mtime,
    }


def tensor_statistics(value: Any) -> dict[str, Any]:
    import torch

    flat = value.detach().float().reshape(-1)
    return {
        "mean": float(flat.mean()),
        "std": float(flat.std(unbiased=False)),
        "min": float(flat.min()),
        "max": float(flat.max()),
        "nan": bool(torch.isnan(flat).any()),
        "inf": bool(torch.isinf(flat).any()),
    }


def compare_hidden_states(before: Any, after: Any) -> dict[str, Any]:
    import torch

    left = before.detach().float().reshape(-1)
    right = after.detach().float().reshape(-1)
    delta = float(torch.linalg.vector_norm(right - left))
    return {
        "l2_delta": delta,
        "cosine_similarity": float(torch.nn.functional.cosine_similarity(left.unsqueeze(0), right.unsqueeze(0))),
        "before": tensor_statistics(left),
        "after": tensor_statistics(right),
        "classification": "domain_adapter_not_applied" if delta < 1e-8 else "adapter_applied",
    }


def diagnose_final_decode(
    raw: Any,
    *,
    token_ids: list[int],
    eos_token_id: int | None,
    max_new_tokens: int,
    rendered_prompt: str,
    required_prompt_marker: str = "domain_opinion_v1",
    validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text = str(raw or "").strip()
    parsed = parse_domain_opinion(text)
    eos_seen = bool(eos_token_id is not None and eos_token_id in token_ids)
    if required_prompt_marker not in rendered_prompt:
        primary = "wrong_chat_template"
    elif not text:
        primary = "empty_decode"
    elif eos_seen and len(token_ids) <= 4:
        primary = "early_eos"
    elif any(marker in text.lower() for marker in ("\\boxed", "boxed{", "sympy", "to solve the problem", "step by step")):
        primary = "math_style_decode"
    elif len(token_ids) >= max_new_tokens and not eos_seen:
        primary = "truncated_decode"
    elif parsed is None:
        primary = "invalid_json_only"
    elif validation and validation.get("errors"):
        errors = set(validation["errors"])
        if errors & {"invented_source", "invented_fact", "evidence_provenance_invalid"}:
            primary = "missing_source_provenance"
        elif "invented_rule" in errors:
            primary = "missing_rule_provenance"
        else:
            primary = "valid_json_wrong_schema"
    else:
        primary = "valid"
    return {
        "primary": primary,
        "raw_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "raw_captured": bool(text),
        "generated_token_count": len(token_ids),
        "eos_seen": eos_seen,
        "parsed": parsed,
    }


def select_ablation_extremes(results: dict[str, dict[str, Any]]) -> dict[str, str]:
    def quality(item: tuple[str, dict[str, Any]]) -> tuple[float, float]:
        aggregate = item[1]["aggregate"]
        semantic = sum(float(aggregate.get(name) or 0.0) for name in ("schema_validity", "rule_accuracy", "source_accuracy", "contradiction_recall"))
        cosine = item[1].get("mean_cosine_vs_baseline", 1.0)
        return semantic, float(cosine)

    best = max(results.items(), key=quality)[0]
    worst = min(results.items(), key=quality)[0]
    return {"best": best, "worst": worst}


def micro_overfit_gate(metrics: dict[str, Any]) -> dict[str, Any]:
    success = bool(
        int(metrics.get("valid_count") or 0) >= 7
        and float(metrics.get("schema_validity") or 0.0) == 1.0
        and float(metrics.get("rule_accuracy") or 0.0) == 1.0
        and float(metrics.get("source_accuracy") or 0.0) == 1.0
    )
    return {
        "success": success,
        "classification": "trainable_for_domain_contract" if success else "current_native_architecture_not_trainable_for_domain_contract",
    }
