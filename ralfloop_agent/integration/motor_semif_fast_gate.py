from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import time
from typing import Any

import requests

from ralfloop_agent.integration.bottazzi_motor_judge import (
    BotTazziMotorJudge,
    JudgeCase,
    JudgeGate,
    JudgeOutcome,
    JudgeVerdict,
    judge_case_digest,
)

SYSTEM_PROMPT = (
    "Apply the supplied criterion to the supplied evidence. Choose exactly one listed option. "
    "Respond with only its uppercase letter, with no explanation or reasoning."
)
OPTIONS = (
    ("PASS", "All required evidence satisfies the rules; safe to advance the verification stage."),
    ("REQUEST_REVIEW", "Evidence is missing, ambiguous, stale, conflicting, or requires human confirmation."),
    ("REJECT", "Evidence directly violates a required rule or records a failed or forbidden outcome."),
)
LETTERS = "ABC"


@dataclass(frozen=True)
class SemIfFastGateConfig:
    base_url: str = "http://127.0.0.1:19237"
    timeout_sec: float = 15.0
    fast_pass_threshold: float = 0.97


@dataclass(frozen=True)
class SemIfScore:
    decision: str
    probabilities: dict[str, float]
    latency_ms: float
    prompt_sha256: str

    @property
    def pass_probability(self) -> float:
        return float(self.probabilities.get("PASS", 0.0))


def _state_text(case: JudgeCase) -> str:
    parts = [f"Goal: {case.goal}"]
    parts.append("Facts: " + " | ".join(case.facts))
    parts.append("Rules: " + " | ".join(case.rules))
    if case.candidate_answer:
        parts.append("Candidate answer: " + case.candidate_answer)
    if case.side_effect_intent:
        parts.append("Side effect intended: yes")
    return "\n".join(parts)


def render_semif_prompt(case: JudgeCase) -> str:
    payload = {
        "evidence": _state_text(case),
        "criterion": "What is the verification verdict under the supplied rules and evidence?",
        "options": [
            {"letter": LETTERS[index], "description": description}
            for index, (_, description) in enumerate(OPTIONS)
        ],
    }
    user = json.dumps(payload, ensure_ascii=False)
    return (
        f"<|im_start|>system\n{SYSTEM_PROMPT}<|im_end|>\n"
        f"<|im_start|>user\n{user}<|im_end|>\n<|im_start|>assistant\n"
    )


class Qwen35SemIfScorer:
    def __init__(
        self,
        config: SemIfFastGateConfig | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config or SemIfFastGateConfig()
        self.session = session or requests.Session()
        if not 0.0 <= self.config.fast_pass_threshold <= 1.0:
            raise ValueError("fast_pass_threshold must be between 0 and 1")

    def score(self, case: JudgeCase | dict[str, Any]) -> SemIfScore:
        dossier = case if isinstance(case, JudgeCase) else JudgeCase.model_validate(case)
        prompt = render_semif_prompt(dossier)
        grammar = 'root ::= "A" | "B" | "C"'
        payload = {
            "prompt": prompt,
            "n_predict": 1,
            "temperature": 1.0,
            "top_k": 0,
            "top_p": 1.0,
            "min_p": 0.0,
            "n_probs": 3,
            "post_sampling_probs": True,
            "grammar": grammar,
        }
        started = time.perf_counter()
        response = self.session.post(
            f"{self.config.base_url.rstrip('/')}/completion",
            json=payload,
            timeout=self.config.timeout_sec,
        )
        response.raise_for_status()
        latency_ms = (time.perf_counter() - started) * 1000.0
        body = response.json()
        rows = body.get("completion_probabilities")
        if not isinstance(rows, list) or not rows:
            raise ValueError("completion returned no probabilities")
        top_probs = rows[0].get("top_probs")
        if not isinstance(top_probs, list):
            raise ValueError("completion returned invalid probabilities")
        by_letter = {
            str(item.get("token", "")).strip(): float(item.get("prob", 0.0))
            for item in top_probs if isinstance(item, dict)
        }
        if set(LETTERS) - set(by_letter):
            raise ValueError("completion missing option probabilities")
        probabilities = {
            decision: by_letter[LETTERS[index]]
            for index, (decision, _) in enumerate(OPTIONS)
        }
        decision = max(probabilities, key=probabilities.get)
        return SemIfScore(
            decision=decision,
            probabilities=probabilities,
            latency_ms=round(latency_ms, 3),
            prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        )


def semantic_fast_pass_allowed(
    case: JudgeCase,
    score: SemIfScore,
    *,
    threshold: float,
) -> bool:
    metadata = case.metadata if isinstance(case.metadata, dict) else {}
    return (
        not case.side_effect_intent
        and metadata.get("risk_class") == "internal_readonly"
        and metadata.get("semantic_fastpath_eligible") is True
        and score.decision == "PASS"
        and score.pass_probability >= threshold
    )


def semantic_fast_pass_outcome(case: JudgeCase, score: SemIfScore) -> JudgeOutcome:
    verdict = JudgeVerdict(
        decision="PASS",
        confidence=score.pass_probability,
        risk="LOW",
        reason="semif_fast_pass",
        missing_evidence=[],
        provider="semif_qwen35_fastgate",
    )
    gate = JudgeGate(
        proceed_to_next_stage=True,
        execution_authorized=False,
        status="semantic_fast_pass",
        requires_human_confirmation=False,
        requires_human_review=False,
    )
    marker = json.dumps({
        "source": "semif_qwen35_fastgate",
        "pass_probability": round(score.pass_probability, 6),
        "latency_ms": score.latency_ms,
        "prompt_sha256": score.prompt_sha256,
    }, sort_keys=True, separators=(",", ":"))
    return JudgeOutcome(
        case_digest=judge_case_digest(case),
        verdict=verdict,
        gate=gate,
        raw_text=marker,
    )


class SemIfMotorCascade:
    def __init__(
        self,
        *,
        scorer: Qwen35SemIfScorer | None = None,
        motor: Any | None = None,
    ) -> None:
        self.scorer = scorer or Qwen35SemIfScorer()
        self.motor = motor or BotTazziMotorJudge()

    def judge(self, case: JudgeCase | dict[str, Any]) -> JudgeOutcome:
        dossier = case if isinstance(case, JudgeCase) else JudgeCase.model_validate(case)
        try:
            score = self.scorer.score(dossier)
        except Exception:
            return self.motor.judge(dossier)
        threshold = self.scorer.config.fast_pass_threshold
        if not semantic_fast_pass_allowed(dossier, score, threshold=threshold):
            return self.motor.judge(dossier)
        return semantic_fast_pass_outcome(dossier, score)


__all__ = [
    "Qwen35SemIfScorer",
    "SemIfFastGateConfig",
    "SemIfMotorCascade",
    "SemIfScore",
    "render_semif_prompt",
    "semantic_fast_pass_allowed",
    "semantic_fast_pass_outcome",
]
