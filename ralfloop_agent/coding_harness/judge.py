from __future__ import annotations

import json
import time
from typing import Any, Literal, Mapping

import requests
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


CodingIssueType = Literal[
    "correctness",
    "regression",
    "scope_violation",
    "test_gap",
    "security",
    "unsafe_change",
    "other",
]


class CodingIssue(_StrictModel):
    type: CodingIssueType
    severity: Literal["medium", "high"]
    file: str = Field(default="", max_length=240)
    reason: str = Field(min_length=1, max_length=600)
    repair_instruction: str = Field(min_length=1, max_length=600)


class CodingReview(_StrictModel):
    verdict: Literal["pass", "repair"]
    issues: list[CodingIssue] = Field(default_factory=list, max_length=3)
    summary: str = Field(default="", max_length=600)

    @model_validator(mode="after")
    def verdict_matches_issues(self) -> "CodingReview":
        if self.verdict == "pass" and self.issues:
            raise ValueError("coding_pass_with_issues")
        if self.verdict == "repair" and not self.issues:
            raise ValueError("coding_repair_without_issues")
        return self


def parse_coding_review(raw: bytes | str, *, max_bytes: int = 16_384) -> CodingReview:
    data = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    if len(data) > max_bytes:
        raise ValueError("coding_review_output_too_long")

    try:
        text = data.decode("utf-8", errors="strict")
        value = json.loads(text.strip())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("coding_review_malformed_json") from exc

    if not isinstance(value, dict):
        raise ValueError("coding_review_not_object")

    try:
        return CodingReview.model_validate(value)
    except ValidationError as exc:
        raise ValueError("coding_review_schema_invalid") from exc


def coding_prompt(packet: Mapping[str, Any]) -> str:
    schema = {
        "verdict": "pass|repair",
        "issues": [{
            "type": "correctness|regression|scope_violation|test_gap|security|unsafe_change|other",
            "severity": "medium|high",
            "file": "affected path or empty",
            "reason": "specific defect",
            "repair_instruction": "minimal concrete repair",
        }],
        "summary": "",
    }

    return "\n".join((
        "You are the final coding judge. Review only; never use tools and never modify files.",
        "Deterministic validator results are authoritative facts.",
        "Judge whether the resulting change safely satisfies TASK without regression or unjustified scope.",
        "Do not reject merely for style. Report at most ONE most important actionable defect.",
        "If evidence is sufficient and no material defect exists, verdict=pass and issues=[].",
        "If a material defect exists, verdict=repair with one concrete minimal repair instruction.",
        "Return strict JSON only, no markdown.",
        "SCHEMA=" + json.dumps(schema, separators=(",", ":")),
        "DATA=" + json.dumps(dict(packet), ensure_ascii=False, separators=(",", ":"), default=str),
    ))


class ResidentDs4CodingJudge:
    """Judge-only client for an already-running loopback DS4 server."""

    provider = "deepseek_v4_flash"
    model = "deepseek-chat"

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:19194",
        timeout_sec: float = 1800.0,
        max_output_tokens: int = 256,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = float(timeout_sec)
        self.max_output_tokens = int(max_output_tokens)
        self.http = session or requests.Session()

    def review(self, packet: Mapping[str, Any]) -> tuple[CodingReview, dict[str, Any]]:
        prompt = coding_prompt(packet)
        started = time.monotonic()

        try:
            response = self.http.post(
                f"{self.base_url}/v1/chat/completions",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": self.max_output_tokens,
                    "temperature": 0,
                    "top_p": 1,
                    "stream": False,
                    "thinking": {"type": "disabled"},
                    "think": False,
                },
                timeout=(3, self.timeout_sec),
            )
            response.raise_for_status()
            payload = response.json()
        except requests.Timeout as exc:
            raise TimeoutError("coding_judge_timeout") from exc
        except (requests.RequestException, ValueError) as exc:
            raise RuntimeError("coding_judge_runtime_error") from exc

        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("coding_judge_response_invalid") from exc

        review = parse_coding_review(content)
        usage = payload.get("usage") or {}

        return review, {
            "provider": self.provider,
            "model": self.model,
            "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "raw": content,
        }
