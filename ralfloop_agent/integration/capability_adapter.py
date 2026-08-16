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
    local_maintenance = "local_maintenance" in route.skills_used
    write_policy = {
        "check_only": "no_write",
        "read_only_system_inspection": "no_write",
        "patch_allowed": "sandbox_write_allowed_after_repro",
        "external_action": "external_side_effect_requires_confirmation",
    }[route.mode]
    workflow = {
        "check_only": ["collect_evidence", "summarize_findings"],
        "read_only_system_inspection": ["validate_read_only_plan", "collect_real_evidence", "summarize_findings"],
        "patch_allowed": ["reproduce_failure", "apply_minimal_patch", "run_targeted_tests"],
        "external_action": ["prepare_action", "request_human_confirmation", "execute_after_confirmation"],
    }[route.mode]
    shell_evidence_tools = {
        "check_only": ["rg", "find", "git status"],
        "read_only_system_inspection": ["df", "du", "find", "stat", "ls", "journalctl --disk-usage", "docker system df"],
        "patch_allowed": ["rg", "find", "git status", "python3 -m py_compile", "pytest"],
        "external_action": [],
    }[route.mode]
    jury = route.jury_policy.model_dump(mode="json")
    collaboration = route.collaboration_backend.model_dump(mode="json")
    verification = route.verification_policy.model_dump(mode="json")
    if route.jury_policy.enabled:
        if route.jury_policy.mode == "required":
            workflow = ["text_mas_deliberation", *workflow, "text_mas_final_review"]
        else:
            workflow = [*workflow, "text_mas_advisory_review"]
    blocked_actions = []
    if route.mode == "read_only_system_inspection":
        blocked_actions.extend(["unvalidated_shell_command", "filesystem_write", "invented_verification"])
    if route.mode == "patch_allowed":
        blocked_actions.append("patch_without_repro")
    if route.mode == "external_action":
        blocked_actions.append("send_without_human_confirmation")
    if route.jury_policy.mode == "required":
        blocked_actions.append("final_answer_without_jury_review")
    if local_maintenance:
        blocked_actions.extend(["generic_arbitrary_shell", "unapproved_local_mutation"])
    return {
        **route.model_dump(),
        "task_mode": route.mode,
        "write_policy": write_policy,
        "evidence_first": True,
        "domain_skills": list(route.skills_used),
        "mcp_connectors": list(route.mcp_used),
        "needs_human_confirmation": route.requires_confirmation,
        "needs_jury": route.jury_policy.enabled,
        "jury_mode": route.jury_policy.mode,
        "jury": jury,
        "jury_policy": jury,
        "collaboration_backend": collaboration,
        "verification_policy": verification,
        "shell_evidence_tools": shell_evidence_tools,
        "required_output_fields": ["command", "path", "exit_code", "stdout", "stderr"],
        "workflow": workflow,
        "blocked_actions": blocked_actions,
        "capability": "local_software_maintenance" if local_maintenance else "capability_router",
        "canonical_actions_only": local_maintenance,
        "local_maintenance_policy": (
            "preview_check_hash_persistent_approval_one_shot_audit"
            if local_maintenance else None
        ),
    }


__all__ = ["mcp_client", "route_task", "route_to_legacy_dict", "skills_registry"]
