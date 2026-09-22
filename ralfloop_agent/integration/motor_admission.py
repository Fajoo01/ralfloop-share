from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from ralfloop_agent.integration.bottazzi_motor_judge import JudgeCase
from ralfloop_agent.integration.motor_prompt_budget import (
    MotorPromptBudget,
    count_rendered_tokens,
    render_ds41_judge_prompt,
)
from ralfloop_agent.integration.motor_semantic_skeleton import (
    GrammarSessionFactory,
    SkeletonPolicy,
    compress_judge_case_facts,
)

AdmissionMode = Literal["RAW", "SAFE_CANDIDATE", "REVIEW"]
TokenCounter = Callable[[JudgeCase], int]


@dataclass(frozen=True)
class MotorAdmissionPlan:
    mode: AdmissionMode
    raw_tokens: int
    budget_tokens: int
    safe_tokens: int | None = None
    reason: str = ""
    guard_fallbacks: int = 0

    @property
    def executable_candidate(self) -> bool:
        return self.mode in {"RAW", "SAFE_CANDIDATE"}

    def as_telemetry(self) -> dict[str, Any]:
        return asdict(self)


def make_exact_token_counter(
    *,
    ds4_binary: str | Path,
    model_path: str | Path,
) -> TokenCounter:
    def _count(case: JudgeCase) -> int:
        return count_rendered_tokens(
            render_ds41_judge_prompt(case),
            ds4_binary=ds4_binary,
            model_path=model_path,
        )

    return _count


def plan_motor_admission(
    case: JudgeCase,
    *,
    token_counter: TokenCounter,
    budget: MotorPromptBudget | None = None,
    policy: SkeletonPolicy | None = None,
    use_grammar: bool = True,
    grammar_session_factory: GrammarSessionFactory | None = None,
) -> MotorAdmissionPlan:
    active = budget or MotorPromptBudget()
    raw_tokens = token_counter(case)
    if raw_tokens <= active.observed_safe_tokens:
        return MotorAdmissionPlan(
            mode="RAW",
            raw_tokens=raw_tokens,
            budget_tokens=active.observed_safe_tokens,
            reason="raw_within_budget",
        )

    safe_case, skeleton = compress_judge_case_facts(
        case,
        policy=policy,
        use_grammar=use_grammar,
        grammar_session_factory=grammar_session_factory,
    )
    if skeleton is None:
        return MotorAdmissionPlan(
            mode="REVIEW",
            raw_tokens=raw_tokens,
            budget_tokens=active.observed_safe_tokens,
            reason="over_budget_not_compression_eligible",
        )

    safe_tokens = token_counter(safe_case)
    common = dict(
        raw_tokens=raw_tokens,
        budget_tokens=active.observed_safe_tokens,
        safe_tokens=safe_tokens,
        guard_fallbacks=skeleton.guard_fallbacks,
    )
    if safe_tokens <= active.observed_safe_tokens:
        return MotorAdmissionPlan(
            mode="SAFE_CANDIDATE",
            reason="safe_candidate_within_budget",
            **common,
        )
    return MotorAdmissionPlan(
        mode="REVIEW",
        reason=(
            "safe_candidate_no_gain"
            if safe_tokens >= raw_tokens
            else "safe_candidate_still_over_budget"
        ),
        **common,
    )


__all__ = [
    "AdmissionMode",
    "MotorAdmissionPlan",
    "TokenCounter",
    "make_exact_token_counter",
    "plan_motor_admission",
]
