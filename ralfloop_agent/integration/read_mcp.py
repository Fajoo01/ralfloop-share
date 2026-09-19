from __future__ import annotations

import json
from typing import Any

from ralfloop_agent.abc_relation.client import RelationMCPClient
from src.models import Evidence


ABC_RELATION_CONNECTOR = "abc_relation"
ABC_RELATION_READ_TOOLS = frozenset(
    {
        "abc_get_state",
        "abc_get_timeline",
        "abc_search_events",
        "abc_analyze",
        "abc_explain_event",
        "abc_list_snapshots",
        "abc_get_reference_library",
        "abc_policy_status",
    }
)
ABC_RELATION_WRITE_TOOLS = frozenset({"abc_record_event", "abc_create_snapshot"})


def _wants_references(user_goal: str) -> bool:
    goal = user_goal.casefold()
    triggers = (
        "manuale",
        "manuali",
        "psicologia",
        "dialogo strategico",
        "nardone",
        "motivational interviewing",
        "rusbult",
        "interdependence",
        "attachment",
        "secure base",
    )
    return any(trigger in goal for trigger in triggers)


def _wants_timeline(user_goal: str) -> bool:
    goal = user_goal.casefold()
    return any(trigger in goal for trigger in ("timeline", "cronologia", "ultimi eventi", "eventi recenti"))


def read_abc_relation(user_goal: str, client: RelationMCPClient | None = None) -> Evidence:
    """Read ABC state through the dedicated MCP without exposing local write tools."""
    relation = client or RelationMCPClient()
    payload: dict[str, Any] = {
        "connector": ABC_RELATION_CONNECTOR,
        "side_effects": 0,
        "writes": 0,
        "sends": 0,
        "state": relation.get_state(),
        "analysis": relation.analyze(),
    }
    if _wants_timeline(user_goal):
        payload["timeline"] = relation.timeline(limit=100)
    if _wants_references(user_goal):
        payload["references"] = relation.references()
    return Evidence(
        command="mcp:abc_relation:read",
        path=relation.socket_path,
        exit_code=0,
        stdout=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        stderr=None,
    )


def run_read_mcp(connector: str, user_goal: str) -> Evidence:
    if connector == ABC_RELATION_CONNECTOR:
        return read_abc_relation(user_goal)
    raise ValueError(f"unsupported_read_mcp:{connector}")


__all__ = [
    "ABC_RELATION_CONNECTOR",
    "ABC_RELATION_READ_TOOLS",
    "ABC_RELATION_WRITE_TOOLS",
    "read_abc_relation",
    "run_read_mcp",
]
