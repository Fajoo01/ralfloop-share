from __future__ import annotations

from src.mcp_client import MCPClient
from src.models import CapabilityRoute, TaskRequest
from src.router import CapabilityRouter
from src.skills import SkillsRegistry


try:
    from ralfloop_agent.experimental.reasoning_cycle_node import run_reasoning_cycle
except Exception:  # pragma: no cover - import guard for stripped runtimes
    run_reasoning_cycle = None


skills_registry = SkillsRegistry()
mcp_client = MCPClient()
_router = CapabilityRouter(skills_registry)


def route_task(user_goal: str, mode: str | None = None) -> CapabilityRoute:
    request = TaskRequest(user_goal=user_goal, mode=mode)
    route = _router.route(request.user_goal)
    if run_reasoning_cycle is None:
        return route
    try:
        packet = run_reasoning_cycle(
            user_goal=request.user_goal,
            constraints=["external_action requires human confirmation"],
            memory={"capability_route": route.model_dump()},
        )
    except Exception:
        return route
    return route.model_copy(
        update={
            "reasoning": (
                f"{route.reasoning}; reasoning_cycle_status={packet.decision.status}; "
                f"reasoning_cycle_action={packet.selected_next_action.get('action_type')}"
            )
        }
    )


def route_to_legacy_dict(route: CapabilityRoute) -> dict:
    write_policy = {
        "check_only": "no_write",
        "patch_allowed": "sandbox_write_allowed_after_repro",
        "external_action": "external_side_effect_requires_confirmation",
    }[route.mode]
    workflow = {
        "check_only": ["collect_evidence", "summarize_findings"],
        "patch_allowed": ["reproduce_failure", "apply_minimal_patch", "run_targeted_tests"],
        "external_action": ["prepare_action", "request_human_confirmation", "execute_after_confirmation"],
    }[route.mode]
    blocked_actions = []
    if route.mode == "patch_allowed":
        blocked_actions.append("patch_without_repro")
    if route.mode == "external_action":
        blocked_actions.append("send_without_human_confirmation")
    return {
        **route.model_dump(),
        "task_mode": route.mode,
        "write_policy": write_policy,
        "evidence_first": True,
        "domain_skills": list(route.skills_used),
        "mcp_connectors": list(route.mcp_used),
        "needs_human_confirmation": route.requires_confirmation,
        "required_output_fields": ["command", "path", "exit_code"],
        "workflow": workflow,
        "blocked_actions": blocked_actions,
    }


__all__ = ["mcp_client", "route_task", "route_to_legacy_dict", "skills_registry"]
