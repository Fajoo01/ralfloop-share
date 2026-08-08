from __future__ import annotations

from src.models import CapabilityRoute


def build_text_mas_trace(user_goal: str, route: CapabilityRoute) -> dict | None:
    policy = route.jury_policy
    backend = route.collaboration_backend
    if not policy.enabled or backend.implementation_level not in {"text_proxy", "routing_only"}:
        return None

    rounds = max(1, int(backend.recursion_rounds or 1))
    phases = _phases_for_style(backend.style)
    loop = []
    for index in range(1, rounds + 1):
        final_round = index == rounds
        phase = phases[min(index - 1, len(phases) - 1)]
        loop.append(
            {
                "round": index,
                "phase": phase,
                "roles": _roles_for_phase(policy.roles, phase),
                "state_key": f"text_mas_round_{index}",
                "decode": "final_review_summary" if final_round else backend.intermediate_decode_policy,
            }
        )

    return {
        "backend_name": backend.backend_name,
        "implementation_level": backend.implementation_level,
        "style": backend.style,
        "mode": policy.mode,
        "reason": policy.reason,
        "triggers": list(policy.triggers),
        "recursion_rounds": rounds,
        "native_latent": backend.native_latent,
        "available": backend.available,
        "availability_reason": backend.availability_reason,
        "intermediate_decode_policy": backend.intermediate_decode_policy,
        "style_selection_source": backend.style_selection_source,
        "fallback_used": backend.fallback_used,
        "fallback_reason": backend.fallback_reason,
        "trace_is_native_recursive_mas": False,
        "goal_preview": user_goal[:240],
        "loop": loop,
    }


def summarize_text_mas_trace(trace: dict | None) -> str | None:
    if not trace:
        return None
    return (
        f"{trace['backend_name']} {trace['implementation_level']}: "
        f"style={trace['style']} rounds={trace['recursion_rounds']} "
        f"native_latent={trace['native_latent']}"
    )


def _phases_for_style(style: str) -> list[str]:
    if style == "mixture":
        return ["parallel_specialists", "text_state_aggregation", "final_review"]
    if style == "deliberation":
        return ["reflect", "tool_risk_review", "final_review"]
    if style == "distillation":
        return ["expert_pass", "learner_refine", "final_review"]
    return ["plan", "critique_refine", "final_review"]


def _roles_for_phase(roles: list[str], phase: str) -> list[str]:
    if not roles:
        return []
    if phase in {"final_review", "text_state_aggregation"}:
        return roles[-2:] if len(roles) >= 2 else roles
    if phase in {"critique_refine", "tool_risk_review"}:
        return roles[1:-1] or roles
    return roles[: max(1, min(3, len(roles)))]
