from __future__ import annotations

from dataclasses import dataclass

from src.router import route_task
from ralfloop_agent.unified_assistant.email_search import is_email_search_request


@dataclass(frozen=True)
class InteractionDecision:
    interaction_mode: str
    capability: str
    route_mode: str
    approval_required: bool
    reason: str


def classify_interaction(user_goal: str) -> InteractionDecision:
    route = route_task(user_goal)
    if is_email_search_request(user_goal):
        return InteractionDecision(
            "agent",
            "google_workspace.gmail.read_only",
            route.mode,
            False,
            "deterministic_gmail_read_route",
        )
    if route.mode == "read_only_system_inspection":
        return InteractionDecision(
            "agent",
            "read_only_system_inspection",
            route.mode,
            False,
            "deterministic_read_only_tool_route",
        )
    if route.mode == "external_action":
        return InteractionDecision(
            "agent",
            "protected_external_action",
            route.mode,
            True,
            "deterministic_protected_action_route",
        )
    if route.mode == "patch_allowed":
        return InteractionDecision(
            "unsupported",
            "unsupported_task",
            route.mode,
            False,
            "use_bounded_repair_workflow",
        )
    return InteractionDecision(
        "chat",
        "chat_only",
        route.mode,
        False,
        "deterministic_chat_route",
    )


def explicit_agent_decision(user_goal: str) -> InteractionDecision:
    route = route_task(user_goal)
    capability = {
        "external_action": "protected_external_action",
        "read_only_system_inspection": "read_only_system_inspection",
    }.get(route.mode, route.mode)
    return InteractionDecision(
        "agent",
        capability,
        route.mode,
        route.requires_confirmation,
        "explicit_agent_route",
    )


__all__ = ["InteractionDecision", "classify_interaction", "explicit_agent_decision"]
