from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Annotated, Any, Callable, Literal, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
import requests

from ralfloop_agent.providers.gpu_engine_scheduler import GpuEngineTransitionError, TransactionalGpuScheduler
from ralfloop_agent.providers.llama_cpp_server import (
    LlamaCppServerConfig,
    LlamaCppServerError,
    LlamaCppServerManager,
)
from ralfloop_agent.semantic_judge.ds4_server import (
    Ds4ServerError,
    Ds4ServerProfile,
    Ds4ServerSession,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


SemanticIssueType = Literal[
    "missing_required_meaning",
    "contradiction",
    "unsupported_claim",
    "unsupported_commitment",
    "invented_decision",
    "invented_date",
    "invented_amount",
    "meaning_changed",
    "overstated_evidence",
    "tone_changes_meaning",
    "other_semantic_incongruity",
]
SemanticDomainRef = Annotated[str, Field(min_length=1, max_length=160)]


class SemanticIssue(StrictModel):
    type: SemanticIssueType
    severity: Literal["low", "medium", "high"] = "medium"
    draft_text: str = Field(default="", max_length=320)
    reason: str = Field(min_length=1, max_length=600)
    domain_refs: list[SemanticDomainRef] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def issue_is_actionable(self) -> "SemanticIssue":
        if self.type != "missing_required_meaning" and not self.draft_text.strip():
            raise ValueError("semantic_issue_missing_draft_text")
        if not self.domain_refs:
            raise ValueError("semantic_issue_missing_domain_reference")
        vague = (
            "could be improved", "be clearer", "check consistency",
            "potrebbe essere miglior", "sii più chiar", "verifica la coerenza",
        )
        folded = " ".join(self.reason.casefold().split())
        if len(folded) < 12 or any(value in folded for value in vague):
            raise ValueError("semantic_issue_not_actionable")
        return self


class SemanticReview(StrictModel):
    verdict: Literal["pass", "repair"]
    issues: list[SemanticIssue] = Field(default_factory=list, max_length=6)
    summary: str = Field(default="", max_length=600)

    @model_validator(mode="after")
    def verdict_matches_issues(self) -> "SemanticReview":
        if self.verdict == "pass" and self.issues:
            raise ValueError("pass_with_issues")
        if self.verdict == "repair" and not self.issues:
            raise ValueError("repair_without_issues")
        return self


@dataclass(frozen=True)
class SemanticReviewResult:
    review: SemanticReview
    provider: str
    model: str
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class SemanticDraftJudge(Protocol):
    provider: str
    model: str

    def review(self, context_packet: Mapping[str, Any], draft: str) -> SemanticReviewResult: ...


class JudgeAvailabilityError(RuntimeError):
    pass


class ReviewRisk(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"


@dataclass(frozen=True)
class SemanticJudgeConfig:
    semantic_judge_enabled: bool = False
    semantic_judge_provider: str = "deepseek_v4_flash"
    semantic_judge_timeout_sec: float = 1_800.0
    semantic_judge_max_latency_for_normal_path_sec: float = 1_800.0
    semantic_judge_benchmark_sec: float | None = None
    semantic_judge_fail_open_normal: bool = False
    semantic_judge_allow_normal: bool = False
    deepseek_executable: str = "/home/sibilla-cumana/src/ds4-cuda-stream-pr739/ds4-server"
    deepseek_model_path: str = (
        "/home/sibilla-cumana/Dati/ralfloop-models/deepseek-v4-flash-pr739/gguf/"
        "DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf"
    )
    deepseek_context_tokens: int = 1_024
    deepseek_prefill_chunk: int = 128
    deepseek_max_output_tokens: int = 128
    deepseek_max_output_bytes: int = 16_384
    deepseek_threads: int = 8
    deepseek_stage_mb: int = 1_280
    deepseek_reserve_mb: int = 384
    deepseek_weight_cache_verbose: bool = True
    deepseek_weight_cache_limit_gb: int = 3
    deepseek_server_host: str = "127.0.0.1"
    deepseek_server_port: int = 19_194
    deepseek_startup_timeout_sec: float = 180.0

    @classmethod
    def from_env(cls) -> "SemanticJudgeConfig":
        benchmark = os.getenv("RALFLOOP_SEMANTIC_JUDGE_BENCHMARK_SEC", "").strip()
        return cls(
            semantic_judge_enabled=_env_bool("RALFLOOP_SEMANTIC_JUDGE_ENABLED", False),
            semantic_judge_provider=os.getenv("RALFLOOP_SEMANTIC_JUDGE_PROVIDER", "deepseek_v4_flash").strip(),
            semantic_judge_timeout_sec=_env_float("RALFLOOP_SEMANTIC_JUDGE_TIMEOUT_SEC", 1_800, 1, 7_200),
            semantic_judge_max_latency_for_normal_path_sec=float(
                os.getenv("RALFLOOP_SEMANTIC_JUDGE_MAX_LATENCY_FOR_NORMAL_PATH_SEC", "1800")
            ),
            semantic_judge_benchmark_sec=float(benchmark) if benchmark else None,
            semantic_judge_fail_open_normal=_env_bool("RALFLOOP_SEMANTIC_JUDGE_FAIL_OPEN_NORMAL", False),
            semantic_judge_allow_normal=_env_bool("RALFLOOP_SEMANTIC_JUDGE_ALLOW_NORMAL", False),
            deepseek_executable=os.getenv(
                "RALFLOOP_DS4_SERVER_EXECUTABLE", "/home/sibilla-cumana/src/ds4-cuda-stream-pr739/ds4-server"
            ).strip(),
            deepseek_model_path=os.getenv(
                "RALFLOOP_DS4_MODEL_PATH",
                "/home/sibilla-cumana/Dati/ralfloop-models/deepseek-v4-flash-pr739/gguf/"
                "DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf",
            ).strip(),
            deepseek_context_tokens=_env_int("RALFLOOP_DS4_CONTEXT_TOKENS", 1_024, 1_024, 16_384),
            deepseek_prefill_chunk=_env_int("RALFLOOP_DS4_PREFILL_CHUNK", 128, 32, 4_096),
            deepseek_max_output_tokens=_env_int("RALFLOOP_DS4_MAX_OUTPUT_TOKENS", 128, 64, 256),
            deepseek_max_output_bytes=_env_int("RALFLOOP_DS4_MAX_OUTPUT_BYTES", 16_384, 1_024, 65_536),
            deepseek_threads=_env_int("RALFLOOP_DS4_THREADS", 8, 1, 64),
            deepseek_stage_mb=_env_int("RALFLOOP_DS4_STAGE_MB", 1_280, 512, 4_096),
            deepseek_reserve_mb=_env_int("RALFLOOP_DS4_RESERVE_MB", 384, 256, 4_096),
            deepseek_weight_cache_verbose=_env_bool("RALFLOOP_DS4_WEIGHT_CACHE_VERBOSE", True),
            deepseek_weight_cache_limit_gb=_env_int("RALFLOOP_DS4_WEIGHT_CACHE_LIMIT_GB", 3, 1, 8),
            deepseek_server_host=os.getenv("RALFLOOP_DS4_SERVER_HOST", "127.0.0.1").strip(),
            deepseek_server_port=_env_int("RALFLOOP_DS4_SERVER_PORT", 19_194, 1_024, 65_535),
            deepseek_startup_timeout_sec=_env_float("RALFLOOP_DS4_STARTUP_TIMEOUT_SEC", 180, 1, 1_800),
        )

    def server_profile(self) -> Ds4ServerProfile:
        return Ds4ServerProfile(
            executable=Path(self.deepseek_executable),
            model_path=Path(self.deepseek_model_path),
            host=self.deepseek_server_host,
            port=self.deepseek_server_port,
            context_tokens=self.deepseek_context_tokens,
            prefill_chunk=self.deepseek_prefill_chunk,
            threads=self.deepseek_threads,
            max_output_tokens=self.deepseek_max_output_tokens,
            stage_mb=self.deepseek_stage_mb,
            reserve_mb=self.deepseek_reserve_mb,
            weight_cache_verbose=self.deepseek_weight_cache_verbose,
            weight_cache_limit_gb=self.deepseek_weight_cache_limit_gb,
            startup_timeout_sec=self.deepseek_startup_timeout_sec,
            request_timeout_sec=self.semantic_judge_timeout_sec,
        )

    def should_use(self, risk: ReviewRisk) -> bool:
        if risk is ReviewRisk.LOW:
            return False
        if risk is ReviewRisk.NORMAL:
            return self.semantic_judge_enabled and self.semantic_judge_allow_normal
        return self.semantic_judge_enabled


def parse_semantic_review(raw: bytes | str, *, max_bytes: int = 16_384) -> SemanticReview:
    if isinstance(raw, bytes):
        if len(raw) > max_bytes:
            raise ValueError("semantic_review_output_too_long")
    elif len(raw.encode("utf-8")) > max_bytes:
        raise ValueError("semantic_review_output_too_long")
    try:
        text = raw.decode("utf-8", errors="strict") if isinstance(raw, bytes) else raw
        if not text.strip():
            raise ValueError("semantic_review_empty_output")
        value = json.loads(text.strip())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("semantic_review_malformed_json") from exc
    if not isinstance(value, dict):
        raise ValueError("semantic_review_not_object")
    try:
        return SemanticReview.model_validate(value)
    except ValidationError as exc:
        raise ValueError("semantic_review_schema_invalid") from exc


def semantic_prompt(context_packet: Mapping[str, Any], draft: str) -> str:
    packet = _review_only_packet(context_packet)
    schema = {
        "verdict": "pass|repair",
        "issues": [{
            "type": "TYPE", "severity": "high",
            "draft_text": "exact <=8 words; empty only for omission",
            "reason": "specific <=10 words", "domain_refs": ["one domain ref"],
        }],
    }
    issue_types = (
        "missing_required_meaning", "contradiction", "unsupported_claim", "unsupported_commitment",
        "invented_decision", "invented_date", "invented_amount", "meaning_changed",
        "overstated_evidence", "tone_changes_meaning", "other_semantic_incongruity",
    )
    return "\n".join((
        "Semantic email critic only; never rewrite, act, authorize, or use tools.",
        "DOMAIN is authoritative. Detect missing required meaning, contradiction, unsupported fact/commitment/decision/date/amount, lost uncertainty, stronger meaning, or wrong actor.",
        "Return at most ONE most important issue. Quote <=8 words (empty only for omission), reason <=10 words, exactly one domain_ref. No vague advice. No issue => pass with []. Omit summary.",
        "TYPES=" + ",".join(issue_types),
        "Return only strict JSON, no markdown. SCHEMA=" + json.dumps(schema, separators=(",", ":")),
        "DATA=" + json.dumps({"intent": packet["normalized_intent"], "domain": packet["email_reply_domain_v1"], "draft": draft}, ensure_ascii=False, separators=(",", ":")),
    ))


class DeepSeekV4FlashJudge:
    provider = "deepseek_v4_flash"
    model = "deepseek-v4-flash"

    def __init__(
        self,
        *,
        config: SemanticJudgeConfig | None = None,
        scheduler: TransactionalGpuScheduler | None = None,
        server_factory: Callable[[Ds4ServerProfile], Any] | None = None,
    ) -> None:
        self.config = config or SemanticJudgeConfig()
        self.scheduler = scheduler
        self.server_factory = server_factory or Ds4ServerSession

    def review(self, context_packet: Mapping[str, Any], draft: str) -> SemanticReviewResult:
        prompt = semantic_prompt(context_packet, draft)
        profile = self.config.server_profile()
        if not profile.executable.is_file() or not os.access(profile.executable, os.X_OK):
            raise JudgeAvailabilityError("deepseek_v4_flash_server_unavailable")
        if not profile.model_path.is_file():
            raise JudgeAvailabilityError("deepseek_v4_flash_model_unavailable")
        task_id = "email-critic-" + hashlib.sha256(prompt.encode()).hexdigest()[:16]
        started = time.monotonic()
        scheduler = self.scheduler or TransactionalGpuScheduler()
        try:
            with scheduler.engine_session("deepseek", task_id=task_id):
                with self.server_factory(profile) as server:
                    reply = server.review_prompt(prompt, max_tokens=self.config.deepseek_max_output_tokens)
        except TimeoutError:
            raise
        except Ds4ServerError as exc:
            raise JudgeAvailabilityError(str(exc)) from exc
        except GpuEngineTransitionError as exc:
            raise JudgeAvailabilityError(f"deepseek_v4_flash_gpu_unavailable:{exc}") from exc
        except OSError as exc:
            raise JudgeAvailabilityError("deepseek_v4_flash_runtime_unavailable") from exc
        review = parse_semantic_review(reply.content, max_bytes=self.config.deepseek_max_output_bytes)
        latency_ms = max(0, int((time.monotonic() - started) * 1000))
        return SemanticReviewResult(
            review, self.provider, self.model, latency_ms,
            input_tokens=reply.input_tokens or _rough_tokens(prompt),
            output_tokens=reply.output_tokens or _rough_tokens(reply.content),
            metadata={
                "server_startup_ms": getattr(server, "startup_ms", None),
                "request_ms": reply.request_ms,
                "prefill_ms": getattr(reply, "prefill_ms", None),
                "decode_ms": getattr(reply, "decode_ms", None),
                "diagnostics": reply.diagnostics.as_dict(),
                "single_resident_session": True,
                "critic_protocol": "semantic_review_v1",
            },
        )

    def command(self) -> list[str]:
        return self.config.server_profile().command()

    def environment_overrides(self) -> dict[str, str]:
        return self.config.server_profile().environment_overrides()

    def request_payload(self, prompt: str) -> dict[str, Any]:
        return self.config.server_profile().request_payload(prompt)


class LlamaCppSemanticJudge:
    """Strict review-only judge reusing the already healthy local chat server."""

    provider = "llama_cpp"

    def __init__(
        self,
        *,
        config: LlamaCppServerConfig | None = None,
        manager: LlamaCppServerManager | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config or LlamaCppServerConfig.from_env()
        self.session = session or requests.Session()
        self.manager = manager or LlamaCppServerManager(
            self.config, session=self.session,
        )
        self.model = self.config.model

    def review(
        self, context_packet: Mapping[str, Any], draft: str
    ) -> SemanticReviewResult:
        prompt = semantic_prompt(context_packet, draft)
        started = time.monotonic()
        try:
            self.manager.ensure_available()
            response = self.session.post(
                f"{self.config.base_url}/v1/chat/completions",
                json={
                    "model": self.config.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Review only. Return the requested strict JSON "
                                "object; never rewrite, authorize, or execute actions."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0,
                    "max_tokens": 128,
                    "stream": False,
                    "chat_template_kwargs": {"enable_thinking": False},
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "semantic_review",
                            "strict": True,
                            "schema": SemanticReview.model_json_schema(),
                        },
                    },
                },
                timeout=(2.0, min(self.config.request_timeout_sec, 180.0)),
            )
            response.raise_for_status()
        except (LlamaCppServerError, requests.RequestException) as exc:
            raise JudgeAvailabilityError(
                "llama_cpp_semantic_judge_unavailable"
            ) from exc
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            usage = payload.get("usage") or {}
            reply_model = str(payload.get("model") or self.model)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError("semantic_judge_invalid_response") from exc
        finally:
            response.close()
        review = parse_semantic_review(content)
        latency_ms = max(0, int((time.monotonic() - started) * 1000))
        return SemanticReviewResult(
            review,
            self.provider,
            reply_model,
            latency_ms,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            metadata={
                "critic_protocol": "semantic_review_v1",
                "json_schema_constrained": True,
            },
        )


def build_semantic_judge(config: SemanticJudgeConfig) -> SemanticDraftJudge:
    if config.semantic_judge_provider == "deepseek_v4_flash":
        return DeepSeekV4FlashJudge(config=config)
    if config.semantic_judge_provider == "llama_cpp":
        return LlamaCppSemanticJudge()
    raise JudgeAvailabilityError("semantic_judge_unknown_provider")


def classify_email_risk(context_packet: Mapping[str, Any], *, classification: str | None = None) -> ReviewRisk:
    from ralfloop_agent.semantic_judge.risk import assess_email_risk

    assessment = assess_email_risk(context_packet, classification=classification)
    return ReviewRisk(assessment.level.upper())


def _review_only_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    domain = packet.get("email_reply_domain_v1")
    if not isinstance(domain, Mapping):
        raise JudgeAvailabilityError("semantic_judge_domain_missing")
    return {
        "normalized_intent": [str(item)[:240] for item in (packet.get("user_intent") or [])[:2]],
        "email_reply_domain_v1": _compact_critic_domain(domain),
        "domain_sha256": packet.get("email_reply_domain_sha256"),
    }


def _compact_critic_domain(domain: Mapping[str, Any]) -> dict[str, Any]:
    """Bound critic context while retaining authoritative semantic constraints."""
    def items(name: str, maximum: int) -> list[Mapping[str, Any]]:
        return [item for item in (domain.get(name) or [])[:maximum] if isinstance(item, Mapping)]

    def project(
        values: list[Mapping[str, Any]], fields: tuple[str, ...], *, text_limit: int = 120,
    ) -> list[dict[str, Any]]:
        return [
            {key: (str(item[key])[:text_limit] if key in {"text", "statement", "name"} else item[key])
             for key in fields if item.get(key) not in (None, "", [])}
            for item in values
        ]

    supported_facts = [
        item for item in items("supported_facts", 64)
        if not all(
            str(ref).endswith((".subject", ".date"))
            for ref in (item.get("evidence_refs") or [])
        )
    ]
    supported_facts.sort(key=lambda item: (
        0 if any(str(ref).startswith("structured_artifacts[") for ref in (item.get("evidence_refs") or [])) else
        1 if item.get("certainty") == "uncertain" else
        2 if any(str(ref) == "source_email.body" for ref in (item.get("evidence_refs") or [])) else 3
    ))
    supported_facts = supported_facts[:3]
    value: dict[str, Any] = {
        "required_meanings": project(
            items("required_meanings", 4), ("key", "certainty")
        ),
        "supported_facts": project(
            supported_facts, ("key", "statement", "certainty", "actor_refs"), text_limit=70,
        ),
        "forbidden_claims_without_evidence": list(domain.get("forbidden_claims_without_evidence") or [])[:6],
        "allowed_commitments": list(domain.get("allowed_commitments") or [])[:4],
        "supported_dates": project(items("supported_dates", 3), ("key", "statement", "certainty"), text_limit=60),
        "supported_amounts": project(items("supported_amounts", 3), ("key", "statement", "certainty"), text_limit=60),
        "decisions": project(items("decisions", 3), ("key", "statement", "certainty", "actor_refs"), text_limit=70),
        "subjects": project(items("subjects", 4), ("name", "role"), text_limit=60),
        "user_constraints": [str(item)[:80] for item in (domain.get("user_constraints") or [])[:6]],
    }
    return {key: item for key, item in value.items() if item not in (None, "", [])}


def _rough_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().casefold() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name}_invalid") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name}_out_of_range")
    return value


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name}_invalid") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name}_out_of_range")
    return value
