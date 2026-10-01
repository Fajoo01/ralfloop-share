from __future__ import annotations

from dataclasses import dataclass
import re

from src.router import route_task
from ralfloop_agent.unified_assistant.email_search import is_email_search_request


_EMAIL_DRAFT_OR_REVISION_RE = re.compile(
    r"\b(?:scrivi\s+(?:una\s+mail\s+)?a|prepara\s+(?:una\s+)?(?:mail|email)|"
    r"bozza\s+(?:mail|email|gmail)|rendila|cambiala|accorciala|allungala|riscrivila|"
    r"pi[uù]\s+formale|meno\s+formale|pi[uù]\s+breve|modifica\s+la\s+bozza)\b",
    re.I,
)
_EMAIL_SEND_RE = re.compile(r"\b(?:invia|manda|send)\b.*\b(?:mail|email)\b", re.I)


def _is_email_draft_turn(text: str) -> bool:
    return bool(_EMAIL_DRAFT_OR_REVISION_RE.search(text) and not _EMAIL_SEND_RE.search(text))


@dataclass(frozen=True)
class InteractionDecision:
    interaction_mode: str
    capability: str
    route_mode: str
    approval_required: bool
    reason: str


def classify_interaction(user_goal: str) -> InteractionDecision:
    route = route_task(user_goal)
    if _is_email_draft_turn(user_goal):
        return InteractionDecision("agent", "email.draft.editable", "check_only", False, "editable_email_draft_route")
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
    if _is_email_draft_turn(user_goal):
        return InteractionDecision("agent", "email.draft.editable", "check_only", False, "editable_email_draft_route")
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
