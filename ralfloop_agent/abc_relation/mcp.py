from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Mapping

from pydantic import ValidationError

from .intake import build_proposal
from .models import EvidenceKind, Hypothesis, SourceKind, StrategyRule
from .references import reference_library
from .service import RelationService


MCP_PROTOCOL_VERSION = "2025-03-26"


def _schema(properties: Mapping[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


LIMIT = {"type": "integer", "minimum": 1, "maximum": 1000}
EVENT_ID = {"type": "string", "pattern": r"^abc_evt_[a-f0-9]{16}$"}
TEXT = {"type": "string", "minLength": 1, "maxLength": 2000}

TOOLS: dict[str, dict[str, Any]] = {
    "abc_get_state": _schema({}),
    "abc_get_timeline": _schema({
        "limit": LIMIT,
        "kind": {"type": "string", "enum": [item.value for item in EvidenceKind]},
    }),
    "abc_search_events": _schema({"query": TEXT, "limit": LIMIT}, ("query",)),
    "abc_analyze": _schema({}),
    "abc_explain_event": _schema({"event_id": EVENT_ID}, ("event_id",)),
    "abc_list_snapshots": _schema({"limit": {"type": "integer", "minimum": 1, "maximum": 100}}),
    "abc_get_reference_library": _schema({}),
    "abc_policy_status": _schema({}),
    "abc_propose_event": _schema({
        "text": TEXT,
        "occurred_at": {"type": "string", "minLength": 10, "maxLength": 64},
        "actor": {"type": "string", "maxLength": 120},
        "source_kind": {"type": "string", "enum": [item.value for item in SourceKind]},
        "source_ref": {"type": "string", "minLength": 1, "maxLength": 1000},
    }, ("text",)),
    "abc_record_event": _schema({
        "occurred_at": {"type": "string", "minLength": 10, "maxLength": 64},
        "kind": {"type": "string", "enum": [item.value for item in EvidenceKind]},
        "summary": TEXT,
        "source_kind": {"type": "string", "enum": [item.value for item in SourceKind]},
        "source_ref": {"type": "string", "minLength": 1, "maxLength": 1000},
        "actor": {"type": "string", "maxLength": 120},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "weight": {"type": "number", "minimum": -100.0, "maximum": 100.0},
        "tags": {"type": "array", "items": {"type": "string", "maxLength": 120}, "maxItems": 40},
        "raw_excerpt": {"type": "string", "maxLength": 600},
        "source_hash": {"type": "string", "pattern": r"^[a-f0-9]{64}$"},
        "supersedes_event_id": EVENT_ID,
    }, ("occurred_at", "kind", "summary", "source_kind", "source_ref")),
    "abc_create_snapshot": _schema({
        "label": {"type": "string", "minLength": 1, "maxLength": 300},
        "state_summary": {"type": "string", "minLength": 1, "maxLength": 5000},
        "hypotheses": {"type": "array", "items": {"type": "object"}, "maxItems": 50},
        "strategy_rules": {"type": "array", "items": {"type": "object"}, "maxItems": 50},
        "warnings": {"type": "array", "items": {"type": "string", "maxLength": 500}, "maxItems": 50},
        "source_refs": {"type": "array", "items": {"type": "string", "maxLength": 1000}, "maxItems": 50},
    }, ("label", "state_summary")),
}

READ_ONLY_TOOLS = {
    "abc_get_state", "abc_get_timeline", "abc_search_events", "abc_analyze",
    "abc_explain_event", "abc_list_snapshots", "abc_get_reference_library",
    "abc_policy_status", "abc_propose_event",
}
LOCAL_WRITE_TOOLS = {"abc_record_event", "abc_create_snapshot"}


class RelationMCPServer:
    """MCP facade for the structured ABC relationship domain.

    Writes are local analytical records only.  There are deliberately no tools
    for messaging, contacting, tracking, browser automation or status polling.
    """

    def __init__(self, service: RelationService, *, allow_local_writes: bool = True) -> None:
        self.service = service
        self.allow_local_writes = allow_local_writes

    def list_tools(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for name, schema in TOOLS.items():
            if name in LOCAL_WRITE_TOOLS and not self.allow_local_writes:
                continue
            output.append({
                "name": name,
                "description": self._description(name),
                "inputSchema": schema,
            })
        return output

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name not in TOOLS:
            return _error("NOT_FOUND")
        if name in LOCAL_WRITE_TOOLS and not self.allow_local_writes:
            return _error("POLICY_DENIED")
        malformed = _validate_top_level(arguments, TOOLS[name])
        if malformed:
            return _error(malformed)
        try:
            value = self._dispatch(name, dict(arguments))
            return _result(value)
        except KeyError:
            return _error("MALFORMED_REQUEST")
        except (TypeError, ValueError, ValidationError):
            return _error("MALFORMED_REQUEST")
        except (sqlite3.Error, OSError):
            return _error("SOURCE_UNAVAILABLE")

    def _dispatch(self, name: str, args: dict[str, Any]) -> Any:
        if name == "abc_get_state":
            return self.service.get_state()
        if name == "abc_get_timeline":
            kind = EvidenceKind(args["kind"]) if args.get("kind") else None
            return {"events": self.service.timeline(limit=int(args.get("limit", 100)), kind=kind)}
        if name == "abc_search_events":
            return {"events": self.service.search(str(args["query"]), limit=int(args.get("limit", 50)))}
        if name == "abc_analyze":
            return self.service.analyze().model_dump(mode="json")
        if name == "abc_explain_event":
            value = self.service.explain_event(str(args["event_id"]))
            if value is None:
                raise KeyError("event_not_found")
            return value
        if name == "abc_list_snapshots":
            rows = self.service.store.list_snapshots(limit=int(args.get("limit", 20)))
            return {"snapshots": [row.model_dump(mode="json") for row in rows]}
        if name == "abc_get_reference_library":
            return {
                "references": reference_library(),
                "rule": "frameworks guide interpretation/communication; they are never evidence of hidden mental states",
            }
        if name == "abc_policy_status":
            return self.service.policy_status()
        if name == "abc_propose_event":
            return build_proposal(
                str(args["text"]),
                occurred_at=str(args["occurred_at"]) if args.get("occurred_at") else None,
                actor=str(args["actor"]) if args.get("actor") else None,
                source_kind=str(args.get("source_kind", SourceKind.MANUAL.value)),
                source_ref=str(args.get("source_ref", "manual:natural_intake")),
            )
        if name == "abc_record_event":
            occurred = _aware_datetime(str(args["occurred_at"]))
            event = self.service.record_event(
                occurred_at=occurred,
                kind=EvidenceKind(str(args["kind"])),
                summary=str(args["summary"]),
                source_kind=SourceKind(str(args["source_kind"])),
                source_ref=str(args["source_ref"]),
                actor=str(args["actor"]) if args.get("actor") else None,
                confidence=float(args.get("confidence", 1.0)),
                weight=float(args.get("weight", 0.0)),
                tags=tuple(str(v) for v in args.get("tags", ())),
                raw_excerpt=str(args["raw_excerpt"]) if args.get("raw_excerpt") else None,
                source_hash=str(args["source_hash"]) if args.get("source_hash") else None,
                supersedes_event_id=str(args["supersedes_event_id"]) if args.get("supersedes_event_id") else None,
            )
            return {"event": event.model_dump(mode="json"), "side_effects": "local_db_only"}
        if name == "abc_create_snapshot":
            hypotheses = tuple(Hypothesis.model_validate(row) for row in args.get("hypotheses", ()))
            rules = tuple(StrategyRule.model_validate(row) for row in args.get("strategy_rules", ()))
            snapshot = self.service.create_snapshot(
                label=str(args["label"]),
                state_summary=str(args["state_summary"]),
                hypotheses=hypotheses,
                strategy_rules=rules,
                warnings=tuple(str(v) for v in args.get("warnings", ())),
                source_refs=tuple(str(v) for v in args.get("source_refs", ())),
            )
            return {"snapshot": snapshot.model_dump(mode="json"), "side_effects": "local_db_only"}
        raise KeyError(name)

    @staticmethod
    def _description(name: str) -> str:
        descriptions = {
            "abc_get_state": "Read the latest ABC snapshot plus current deterministic analysis.",
            "abc_get_timeline": "Read normalized observable/inference evidence in chronological order.",
            "abc_search_events": "Search normalized ABC evidence without reading arbitrary raw chat files.",
            "abc_analyze": "Recalculate deterministic relational score and bias warnings from active evidence.",
            "abc_explain_event": "Explain provenance, evidence kind, confidence and cautions for one event.",
            "abc_list_snapshots": "List recent state/strategy snapshots.",
            "abc_get_reference_library": "List psychology and strategic-dialogue frameworks and their usage limits.",
            "abc_policy_status": "Show privacy, non-surveillance and non-outbound-action policy.",
            "abc_propose_event": "Convert one natural-language update into a normalized preview plus digest; no write or side effect.",
            "abc_record_event": "Record one normalized evidence atom locally with provenance; never sends anything.",
            "abc_create_snapshot": "Persist one analytical state/strategy snapshot locally; never sends anything.",
        }
        return descriptions[name]


def _aware_datetime(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone_required")
    return value


def _validate_top_level(arguments: Mapping[str, Any], schema: Mapping[str, Any]) -> str:
    if not isinstance(arguments, Mapping):
        return "MALFORMED_REQUEST"
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or ())
    if set(arguments) - set(properties) or required - set(arguments):
        return "MALFORMED_REQUEST"
    for key, value in arguments.items():
        spec = properties[key]
        typ = spec.get("type")
        if typ == "string" and not isinstance(value, str):
            return "MALFORMED_REQUEST"
        if typ == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
            return "MALFORMED_REQUEST"
        if typ == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
            return "MALFORMED_REQUEST"
        if typ == "array" and not isinstance(value, list):
            return "MALFORMED_REQUEST"
        if "enum" in spec and value not in spec["enum"]:
            return "MALFORMED_REQUEST"
        if isinstance(value, str):
            if len(value) < int(spec.get("minLength", 0)) or len(value) > int(spec.get("maxLength", 10**9)):
                return "MALFORMED_REQUEST"
        if isinstance(value, int) and typ in {"integer", "number"}:
            if value < spec.get("minimum", value) or value > spec.get("maximum", value):
                return "MALFORMED_REQUEST"
        if isinstance(value, float) and typ == "number":
            if value < spec.get("minimum", value) or value > spec.get("maximum", value):
                return "MALFORMED_REQUEST"
        if isinstance(value, list) and len(value) > int(spec.get("maxItems", 10**9)):
            return "MALFORMED_REQUEST"
    return ""


def _result(value: Any) -> dict[str, Any]:
    payload = {"ok": True, "result": value}
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)}],
        "structuredContent": payload,
        "isError": False,
    }


def _error(code: str) -> dict[str, Any]:
    payload = {"ok": False, "error": code}
    return {
        "content": [{"type": "text", "text": code}],
        "structuredContent": payload,
        "isError": True,
    }


__all__ = [
    "MCP_PROTOCOL_VERSION", "TOOLS", "READ_ONLY_TOOLS", "LOCAL_WRITE_TOOLS",
    "RelationMCPServer",
]
