from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Callable, Mapping

from ralfloop_agent.domains.email_reply import (
    build_email_reply_domain,
    domain_digest,
    validate_draft_against_domain,
)
from ralfloop_agent.semantic_judge.core import (
    JudgeAvailabilityError,
    SemanticDraftJudge,
    SemanticJudgeConfig,
    build_semantic_judge,
)
from ralfloop_agent.semantic_judge.risk import EmailRiskAssessment, assess_email_risk


DraftValidator = Callable[[str, Mapping[str, Any]], Any]
RepairCallback = Callable[[Mapping[str, Any], str, list[Mapping[str, Any]]], Any]


class ShadowModeDisabled(RuntimeError):
    pass


def run_shadow_email_case(
    *,
    case_id: str,
    context_packet: Mapping[str, Any],
    draft: str,
    artifact_root: str | Path,
    semantic_judge: SemanticDraftJudge | None = None,
    semantic_judge_config: SemanticJudgeConfig | None = None,
    repair_callback: RepairCallback | None = None,
    hard_guard: DraftValidator | None = None,
    final_validator: DraftValidator | None = None,
    risk_classification: str | None = None,
    shadow_enabled: bool | None = None,
) -> dict[str, Any]:
    """Run an observational email review. Only its redacted artifact is persisted."""

    enabled = _env_bool("RALFLOOP_SEMANTIC_JUDGE_SHADOW", False) if shadow_enabled is None else shadow_enabled
    if not enabled:
        raise ShadowModeDisabled("semantic_judge_shadow_disabled")

    started = time.monotonic()
    packet = deepcopy(dict(context_packet))
    domain = build_email_reply_domain(packet)
    packet["email_reply_domain_v1"] = domain.model_dump(mode="json")
    packet["email_reply_domain_sha256"] = domain_digest(domain)
    risk = assess_email_risk(packet, draft, classification=risk_classification)
    hard_guard_fn = hard_guard or _domain_validator
    final_validator_fn = final_validator or _domain_validator
    result = _base_result(case_id, risk, packet["email_reply_domain_sha256"], draft)

    try:
        hard_guard_fn(draft, packet)
    except Exception as exc:  # guard implementations expose different stable exception types
        result["hard_guard"] = _failure_code(exc)
        result["ds4_skipped_reason"] = "hard_guard_block"
        result["final_validator"] = "not_run"
        result["overall_result"] = "blocked"
        return _finish(result, packet, artifact_root, started)
    result["hard_guard"] = "passed"

    candidate = draft
    if risk.level != "high":
        result["ds4_skipped_reason"] = f"risk_{risk.level}"
    else:
        result["ds4_invoked"] = True
        config = semantic_judge_config or SemanticJudgeConfig.from_env()
        try:
            judge = semantic_judge or build_semantic_judge(config)
            semantic = judge.review(packet, draft)
        except TimeoutError:
            result.update({
                "ds4_verdict": "timeout",
                "ds4_error": "semantic_judge_timeout",
                "final_validator": "not_run",
                "overall_result": "blocked",
            })
            return _finish(result, packet, artifact_root, started)
        except (JudgeAvailabilityError, ValueError) as exc:
            result.update({
                "ds4_verdict": "unavailable" if isinstance(exc, JudgeAvailabilityError) else "invalid_output",
                "ds4_error": _failure_code(exc),
                "final_validator": "not_run",
                "overall_result": "blocked",
            })
            return _finish(result, packet, artifact_root, started)
        except Exception as exc:
            result.update({
                "ds4_verdict": "error",
                "ds4_error": _failure_code(exc),
                "final_validator": "not_run",
                "overall_result": "blocked",
            })
            return _finish(result, packet, artifact_root, started)

        review = semantic.review
        issues = [item.model_dump(mode="json") for item in review.issues]
        result.update({
            "ds4_runtime_ms": semantic.latency_ms,
            "ds4_verdict": review.verdict,
            "ds4_issues": issues,
            "ds4_input_tokens": semantic.input_tokens,
            "ds4_output_tokens": semantic.output_tokens,
        })
        if review.verdict == "repair":
            if repair_callback is None:
                result.update({
                    "final_validator": "not_run",
                    "overall_result": "blocked",
                    "repair_error": "semantic_repair_unavailable",
                })
                return _finish(result, packet, artifact_root, started)
            result["qwen_repair_attempted"] = True
            repair_started = time.monotonic()
            try:
                repaired = repair_callback(packet, draft, issues)
                candidate, fallback_used = _unpack_repair(repaired)
                if fallback_used:
                    raise ValueError("reply_generator_fallback_forbidden")
            except Exception as exc:
                result.update({
                    "qwen_repair_ms": max(0, int((time.monotonic() - repair_started) * 1000)),
                    "repair_error": _failure_code(exc),
                    "final_validator": "not_run",
                    "overall_result": "blocked",
                })
                return _finish(result, packet, artifact_root, started)
            result.update({
                "qwen_repair_ms": max(0, int((time.monotonic() - repair_started) * 1000)),
                "draft_repaired": candidate,
            })

    try:
        final_validator_fn(candidate, packet)
    except Exception as exc:
        result["final_validator"] = _failure_code(exc)
        result["overall_result"] = "blocked"
    else:
        result["final_validator"] = "passed"
        result["overall_result"] = "pass"
        result["qwen_repair_success"] = bool(result["qwen_repair_attempted"])
        result["approval_candidate"] = {"eligible": True, "created": False}
    return _finish(result, packet, artifact_root, started)


def _domain_validator(body: str, packet: Mapping[str, Any]) -> str:
    return validate_draft_against_domain(body, packet["email_reply_domain_v1"])


def _base_result(
    case_id: str,
    risk: EmailRiskAssessment,
    digest: str,
    draft: str,
) -> dict[str, Any]:
    return {
        "schema_version": "email_semantic_shadow_v1",
        "shadow": True,
        "case_id": case_id,
        "risk": risk.model_dump(mode="json"),
        "domain_digest": digest,
        "draft_original": draft,
        "hard_guard": "not_run",
        "ds4_invoked": False,
        "ds4_skipped_reason": None,
        "ds4_runtime_ms": 0,
        "ds4_verdict": None,
        "ds4_issues": [],
        "ds4_error": None,
        "ds4_input_tokens": None,
        "ds4_output_tokens": None,
        "qwen_repair_attempted": False,
        "qwen_repair_ms": 0,
        "qwen_repair_success": False,
        "repair_error": None,
        "draft_repaired": None,
        "final_validator": "not_run",
        "overall_result": "blocked",
        "approval_candidate": {"eligible": False, "created": False},
        "would_reach_approval": False,
        "gmail_sent": False,
        "telegram_sent": False,
    }


def _finish(
    result: dict[str, Any],
    packet: Mapping[str, Any],
    artifact_root: str | Path,
    started: float,
) -> dict[str, Any]:
    result["workload_runtime_ms"] = max(0, int((time.monotonic() - started) * 1000))
    redacted = _redact_value(result, packet)
    root = Path(artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / (_safe_case_id(str(result["case_id"])) + ".json")
    _atomic_json(path, redacted)
    returned = dict(result)
    returned["artifact_path"] = str(path)
    return returned


def _unpack_repair(value: Any) -> tuple[str, bool]:
    if isinstance(value, str):
        return value, False
    text = getattr(value, "text", None)
    if not isinstance(text, str):
        raise ValueError("semantic_repair_invalid_result")
    return text, bool(getattr(value, "fallback_used", False))


def _failure_code(exc: Exception) -> str:
    return str(getattr(exc, "reason_code", "") or str(exc) or type(exc).__name__)[:160]


def _redact_value(value: Any, packet: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        return _redact_text(value, packet)
    if isinstance(value, list):
        return [_redact_value(item, packet) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _redact_value(item, packet) for key, item in value.items()}
    return value


def _redact_text(text: str, packet: Mapping[str, Any]) -> str:
    redacted = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[EMAIL]", text, flags=re.I)
    redacted = re.sub(r"https?://\S+", "[URL]", redacted, flags=re.I)
    redacted = re.sub(r"\b(?:\+?\d[\d .()-]{7,}\d)\b", "[PHONE_OR_ID]", redacted)
    redacted = re.sub(
        r"(?i)\b(?:token|password|secret|api[_ -]?key)\s*[:=]\s*\S+",
        "[SECRET]",
        redacted,
    )
    for index, subject in enumerate(_subject_names(packet)):
        redacted = re.sub(re.escape(subject), f"[SUBJECT_{index}]", redacted, flags=re.I)
    return redacted


def _subject_names(packet: Mapping[str, Any]) -> list[str]:
    domain = packet.get("email_reply_domain_v1")
    if not isinstance(domain, Mapping):
        return []
    names = []
    for item in domain.get("subjects") or []:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        if len(name) >= 4 and "[" not in name:
            names.append(name)
    return sorted(set(names), key=lambda item: (-len(item), item.casefold()))


def _safe_case_id(value: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-.")[:96]
    return safe or "case-" + hashlib.sha256(value.encode()).hexdigest()[:12]


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().casefold() in {"1", "true", "yes", "on"}


__all__ = ["ShadowModeDisabled", "run_shadow_email_case"]
