from __future__ import annotations

import os
from typing import Any

from ralfloop_agent.integration.bottazzi_motor_judge import (
    BotTazziMotorJudge,
    JudgeCase,
    JudgeOutcome,
)
from ralfloop_agent.unified_assistant.judge_context import collect_judge_context
from ralfloop_agent.integration.motor_semantic_skeleton import skeleton_shadow_summary
from ralfloop_agent.integration.motor_admission import make_exact_token_counter, plan_motor_admission
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
    judge_context = context.get("judge_context") or {}
    for item in list(judge_context.get("facts") or [])[:12]:
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
        evidence_refs=[str(item)[:1000] for item in list(judge_context.get("evidence_refs") or [])[:16]],
        side_effect_intent=route.mode == "external_action",
        human_confirmation=bool(context.get("human_confirmation")),
        metadata={"retrieval_context": dict(judge_context.get("metadata") or {})},
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
    context = dict(context or {})
    if "judge_context" not in context:
        try:
            context["judge_context"] = collect_judge_context(user_goal).as_dict()
        except Exception as exc:
            context["judge_context"] = {
                "facts": [f"judge_context_status=unavailable:{type(exc).__name__}"],
                "evidence_refs": [],
                "metadata": {"status": "unavailable"},
            }
    case = build_verification_case(user_goal, route, evidence, candidate_answer, context)
    outcome: JudgeOutcome = (judge or BotTazziMotorJudge()).judge(case)
    trace = outcome.model_dump()
    if os.getenv("BOTTAZZI_MOTOR_SKELETON_SHADOW", "").casefold() in {"1", "true", "yes", "on"}:
        try:
            trace["semantic_skeleton_shadow"] = skeleton_shadow_summary(case)
        except Exception as exc:
            trace["semantic_skeleton_shadow"] = {
                "eligible": False,
                "reason": f"shadow_unavailable:{type(exc).__name__}",
            }
    if os.getenv("BOTTAZZI_MOTOR_ADMISSION_SHADOW", "").casefold() in {"1", "true", "yes", "on"}:
        try:
            ds4_binary = os.getenv("BOTTAZZI_MOTOR_TOKENIZER_BIN", "").strip()
            model_path = os.getenv("BOTTAZZI_MOTOR_MODEL_PATH", "").strip()
            if not ds4_binary or not model_path:
                trace["motor_admission_shadow"] = {
                    "available": False,
                    "reason": "tokenizer_config_missing",
                }
            else:
                counter = make_exact_token_counter(
                    ds4_binary=ds4_binary,
                    model_path=model_path,
                )
                plan = plan_motor_admission(case, token_counter=counter)
                trace["motor_admission_shadow"] = {
                    "available": True,
                    **plan.as_telemetry(),
                }
        except Exception as exc:
            trace["motor_admission_shadow"] = {
                "available": False,
                "reason": f"admission_shadow_unavailable:{type(exc).__name__}",
            }
    return trace


def verification_blocks(route: CapabilityRoute, trace: dict[str, Any] | None) -> bool:
    if not trace or not route.verification_policy.blocking:
        return False
    gate = trace.get("gate") or {}
    return not bool(gate.get("proceed_to_next_stage"))
