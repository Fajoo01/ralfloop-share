from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

from .media_quality import MediaEvidence, MediaQualityService, MediaTicketState, MediaTicketType
from .platform import CapabilityDescriptor, CapabilityPermission, PromotionState, SourceRef


class MediaCatalog(Protocol):
    def list_library_item_ids(self, library_id: str, *, user_id: str, limit: int) -> Mapping[str, Any]: ...
    def get_playback_context(self, item_id: str, *, user_id: str) -> Mapping[str, Any]: ...


TOOLS = (
    "media_ticket_create", "media_ticket_get", "media_ticket_list_open",
    "media_ticket_add_evidence", "media_ticket_diagnose", "media_ticket_prepare_fix",
    "media_ticket_verify_fix", "media_ticket_close", "media_scan_item",
    "media_scan_library", "media_get_streams", "media_get_playback_context",
)


class MediaQualityMCPServer:
    def __init__(self, service: MediaQualityService, reader: Any, *, catalog: MediaCatalog | None = None) -> None:
        self.service, self.reader, self.catalog = service, reader, catalog

    def list_tools(self) -> list[dict[str, Any]]:
        identity = {"type": "string", "minLength": 1, "maxLength": 240, "pattern": r"^[A-Za-z0-9_.:-]+$"}
        tools = [
            _tool("media_ticket_create", "Create an idempotent PII-minimized media quality ticket.", {
                "ticket_type": {"type": "string", "enum": [row.value for row in MediaTicketType]}, "jellyfin_item_id": identity,
                "user_report": {"type": "string", "minLength": 1, "maxLength": 2000},
            }, ["ticket_type", "jellyfin_item_id", "user_report"]),
            _tool("media_ticket_get", "Read one exact media ticket.", {"ticket_id": identity}, ["ticket_id"]),
            _tool("media_ticket_list_open", "List bounded open media tickets.", {"limit": {"type": "integer", "minimum": 1, "maximum": 100}}, []),
            _tool("media_ticket_add_evidence", "Append structured evidence to a media ticket.", {
                "ticket_id": identity, "kind": {"type": "string", "minLength": 1, "maxLength": 64},
                "summary": {"type": "string", "minLength": 1, "maxLength": 1000},
            }, ["ticket_id", "kind", "summary"]),
            _tool("media_ticket_diagnose", "Apply deterministic findings to a ticket.", {
                "ticket_id": identity, "findings": {"type": "array", "maxItems": 32, "items": {"type": "string", "enum": [row.value for row in MediaTicketType]}},
            }, ["ticket_id", "findings"]),
            _tool("media_ticket_prepare_fix", "Prepare a non-executable approval-controlled fix proposal.", {
                "ticket_id": identity, "action": {"type": "string", "pattern": r"^[A-Z][A-Z0-9_]{1,95}$"},
                "reason": {"type": "string", "minLength": 1, "maxLength": 1000}, "reversible": {"type": "boolean"},
            }, ["ticket_id", "action", "reason", "reversible"]),
            _tool("media_ticket_verify_fix", "Verify a separately executed fix from fresh evidence.", {
                "ticket_id": identity, "passed": {"type": "boolean"}, "summary": {"type": "string", "minLength": 1, "maxLength": 1000},
            }, ["ticket_id", "passed", "summary"]),
            _tool("media_ticket_close", "Close without execution as RESOLVED, DUPLICATE or WONT_FIX.", {
                "ticket_id": identity, "outcome": {"type": "string", "enum": ["RESOLVED", "DUPLICATE", "WONT_FIX"]},
            }, ["ticket_id", "outcome"]),
            _tool("media_scan_item", "Read Jellyfin stream metadata and run deterministic quality rules.", {"item_id": identity, "user_id": identity}, ["item_id", "user_id"]),
            _tool("media_scan_library", "Paginated deterministic scan; fails closed unless received unique IDs equal total.", {"library_id": identity, "user_id": identity, "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}, ["library_id", "user_id"]),
            _tool("media_get_streams", "Read bounded Jellyfin media streams.", {"item_id": identity, "user_id": identity}, ["item_id", "user_id"]),
            _tool("media_get_playback_context", "Read playback context when a read-only catalog is installed.", {"item_id": identity, "user_id": identity}, ["item_id", "user_id"]),
        ]
        return tools

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        try:
            now = datetime.now(timezone.utc)
            if name == "media_ticket_create" and set(arguments) == {"ticket_type", "jellyfin_item_id", "user_report"}:
                source = _source(str(arguments["jellyfin_item_id"]), "user_report", now)
                row = self.service.create(ticket_type=MediaTicketType(str(arguments["ticket_type"])), jellyfin_item_id=str(arguments["jellyfin_item_id"]), user_report=str(arguments["user_report"]), source=source, reported_at=now)
                return _result({"ticket": row.model_dump(mode="json"), "writes": 0})
            if name == "media_ticket_get" and set(arguments) == {"ticket_id"}:
                row = self.service.get(str(arguments["ticket_id"]))
                return _result({"ticket": row.model_dump(mode="json") if row else None})
            if name == "media_ticket_list_open" and set(arguments) <= {"limit"}:
                rows = self.service.list_open(limit=int(arguments.get("limit", 100)))
                return _result({"tickets": [row.model_dump(mode="json") for row in rows]})
            if name == "media_ticket_add_evidence" and set(arguments) == {"ticket_id", "kind", "summary"}:
                evidence = MediaEvidence(kind=str(arguments["kind"]), summary=str(arguments["summary"]), observed_at=now, source=_source(str(arguments["ticket_id"]), "operator_evidence", now))
                row = self.service.add_evidence(str(arguments["ticket_id"]), evidence)
                return _result({"ticket": row.model_dump(mode="json"), "writes": 0})
            if name == "media_ticket_diagnose" and set(arguments) == {"ticket_id", "findings"}:
                findings = tuple(MediaTicketType(str(value)) for value in arguments["findings"])
                row = self.service.diagnose(str(arguments["ticket_id"]), findings, _source(str(arguments["ticket_id"]), "deterministic_diagnosis", now), now=now)
                return _result({"ticket": row.model_dump(mode="json"), "writes": 0})
            if name == "media_ticket_prepare_fix" and set(arguments) == {"ticket_id", "action", "reason", "reversible"}:
                proposal = self.service.prepare_fix(str(arguments["ticket_id"]), action=str(arguments["action"]), reason=str(arguments["reason"]), reversible=bool(arguments["reversible"]), source=_source(str(arguments["ticket_id"]), "fix_proposal", now), now=now)
                return _result({"proposal": proposal.model_dump(mode="json"), "writes": 0})
            if name == "media_ticket_verify_fix" and set(arguments) == {"ticket_id", "passed", "summary"}:
                evidence = MediaEvidence(kind="fix_verification", summary=str(arguments["summary"]), observed_at=now, source=_source(str(arguments["ticket_id"]), "fix_verification", now))
                row = self.service.verify_fix(str(arguments["ticket_id"]), passed=bool(arguments["passed"]), evidence=evidence)
                return _result({"ticket": row.model_dump(mode="json"), "writes": 0})
            if name == "media_ticket_close" and set(arguments) == {"ticket_id", "outcome"}:
                row = self.service.close(str(arguments["ticket_id"]), outcome=MediaTicketState(str(arguments["outcome"])), source=_source(str(arguments["ticket_id"]), "manual_close", now), now=now)
                return _result({"ticket": row.model_dump(mode="json"), "writes": 0})
            if name == "media_scan_item" and set(arguments) == {"item_id", "user_id"}:
                scan = self.service.scan_item(str(arguments["item_id"]), user_id=str(arguments["user_id"]), reader=self.reader)
                return _result({"scan": scan.model_dump(mode="json"), "writes": 0})
            if name == "media_get_streams" and set(arguments) == {"item_id", "user_id"}:
                streams = self.reader.get_media_streams(str(arguments["item_id"]), user_id=str(arguments["user_id"]))
                return _result({"streams": [row.model_dump(mode="json") for row in streams]})
            if name == "media_scan_library" and set(arguments) <= {"library_id", "user_id", "limit"} and {"library_id", "user_id"} <= set(arguments):
                if self.catalog is None:
                    return _error("SOURCE_UNAVAILABLE")
                limit = int(arguments.get("limit", 100))
                listing = self.catalog.list_library_item_ids(str(arguments["library_id"]), user_id=str(arguments["user_id"]), limit=limit)
                ids, total = tuple(str(value) for value in listing.get("ids", ())), listing.get("total")
                if not isinstance(total, int) or total < 0 or len(ids) != len(set(ids)) or len(ids) != total:
                    return _error("INCOMPLETE_SOURCE")
                scans = [self.service.scan_item(item, user_id=str(arguments["user_id"]), reader=self.reader).model_dump(mode="json") for item in ids]
                return _result({"scans": scans, "complete": True, "received": len(ids), "expected": total, "writes": 0})
            if name == "media_get_playback_context" and set(arguments) == {"item_id", "user_id"}:
                if self.catalog is None:
                    return _error("SOURCE_UNAVAILABLE")
                return _result({"context": dict(self.catalog.get_playback_context(str(arguments["item_id"]), user_id=str(arguments["user_id"])))})
        except (ValueError, RuntimeError, TypeError):
            return _error("SOURCE_UNAVAILABLE")
        return _error("POLICY_DENIED")


def media_capability_descriptors() -> tuple[CapabilityDescriptor, ...]:
    keywords = {
        "media_ticket_create": ("segnala", "problema", "episodio", "audio", "video", "sottotitoli"),
        "media_scan_item": ("analizza", "qualità", "solo inglese", "audio mancante", "ffprobe"),
        "media_get_streams": ("tracce", "stream", "lingua", "codec", "bitrate"),
        "media_get_playback_context": ("playback", "client", "transcode"),
        "media_ticket_diagnose": ("diagnosi", "ticket", "problema media"),
        "media_ticket_prepare_fix": ("proponi", "correzione", "fix media"),
    }
    rows = []
    for name in TOOLS:
        permission = CapabilityPermission.PROPOSE if name == "media_ticket_prepare_fix" else (CapabilityPermission.DRAFT if name in {"media_ticket_create", "media_ticket_add_evidence", "media_ticket_diagnose", "media_ticket_verify_fix", "media_ticket_close"} else CapabilityPermission.READ)
        rows.append(CapabilityDescriptor(
            capability_id=name, server_id="media_quality.mcp", domain="media_quality", name=name,
            description=f"Semantic media quality capability {name}",
            keywords=keywords.get(name, ("media", "ticket")), permission=permission,
            side_effect=permission is CapabilityPermission.DRAFT, approval_required=permission is CapabilityPermission.PROPOSE,
            source_system="memory", version="1", promotion=PromotionState.SHADOW,
            enabled=True, health="local_shadow",
        ))
    return tuple(rows)


def _source(native_id: str, locator: str, now: datetime) -> SourceRef:
    return SourceRef(system="media_quality", native_id=native_id, locator=locator, observed_at=now.isoformat())


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"name": name, "description": description, "inputSchema": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}}


def _result(payload: Mapping[str, Any]) -> dict[str, Any]:
    body = {"ok": True, **dict(payload)}
    return {"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}], "structuredContent": body, "isError": False}


def _error(status: str) -> dict[str, Any]:
    body = {"ok": False, "status": status, "writes": 0}
    return {"content": [{"type": "text", "text": json.dumps(body)}], "structuredContent": body, "isError": True}


__all__ = ["MediaCatalog", "MediaQualityMCPServer", "TOOLS", "media_capability_descriptors"]
