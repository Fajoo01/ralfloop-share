from __future__ import annotations

from typing import Any

from ralfloop_agent.integration.bottazzi_motor_judge import (
    BotTazziMotorJudge,
    JudgeCase,
    JudgeOutcome,
)
from src.models import CapabilityRoute, Evidence, PatchEvidence



def build_verification_case(
    user_goal: str,
    route: CapabilityRoute,
    evidence: Evidence | PatchEvidence | None,
    candidate_answer: str,
    context: dict[str, Any] | None = None,
) -> JudgeCase:
    context = dict(context or {})
    facts = [f"route_mode={route.mode}"]
    if evidence is not None:
        facts.extend([
            f"evidence_exit_code={evidence.exit_code}",
            f"stdout_present={bool(evidence.stdout)}",
            f"stdout_len={len(evidence.stdout or '')}",
            f"stderr_present={bool(evidence.stderr)}",
            f"stderr_len={len(evidence.stderr or '')}",
        ])
    if isinstance(evidence, PatchEvidence):
        facts.extend([
            f"diff_present={bool(evidence.diff)}",
            f"diff_len={len(evidence.diff or '')}",
            f"tests={evidence.tests}",
        ])
    for item in list(context.get("judge_facts") or [])[:20]:
        facts.append(str(item)[:1000])

    rules = [f"criterion:{item}" for item in route.verification_policy.criteria]
    rules.extend([
        "Deterministic policy and approval gates cannot be overridden.",
        "PASS only advances to the next stage; it never executes an action.",
    ])
    return JudgeCase(
        case_id=str(context.get("task_id") or "verification-case"),
        goal=user_goal,
        facts=facts,
        rules=rules,
        candidate_actions=["PASS", "REQUEST_REVIEW", "REJECT"],
        candidate_answer=candidate_answer,
        side_effect_intent=route.mode == "external_action",
        human_confirmation=bool(context.get("human_confirmation")),
    )


def run_verification_judge(
    user_goal: str,
    route: CapabilityRoute,
    evidence: Evidence | PatchEvidence | None,
    candidate_answer: str,
    context: dict[str, Any] | None = None,
    judge: BotTazziMotorJudge | None = None,
) -> dict[str, Any] | None:
    policy = route.verification_policy
    if not policy.enabled or policy.verifier_type not in {"llm_judge", "combined"}:
        return None
    if policy.judge_provider != "bottazzi_motor":
        return None
    case = build_verification_case(user_goal, route, evidence, candidate_answer, context)
    outcome: JudgeOutcome = (judge or BotTazziMotorJudge()).judge(case)
    return outcome.model_dump()


def verification_blocks(route: CapabilityRoute, trace: dict[str, Any] | None) -> bool:
    if not trace or not route.verification_policy.blocking:
        return False
    gate = trace.get("gate") or {}
    return not bool(gate.get("proceed_to_next_stage"))
