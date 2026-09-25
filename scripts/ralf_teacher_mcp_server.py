#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
from typing import Any, Mapping

from pydantic import ValidationError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ralfloop_agent.teacher.catalog import DESCRIPTIONS, TOOL_MODELS
from ralfloop_agent.teacher.inference import TeacherInferenceClient
from ralfloop_agent.teacher.grammar_client import GrammarEvidenceClient
from ralfloop_agent.teacher.core_client import TeacherCoreClient
from ralfloop_agent.teacher.service import TeacherService
from ralfloop_agent.teacher.store import TeacherStore
from src.mcp_transport import MCP_PROTOCOL_VERSION
from src.teacher import (
    CHECK_ANSWER,
    END_SESSION,
    EXPLAIN,
    EXPLAIN_DIFFERENTLY,
    GENERATE_EXERCISE,
    HINT,
    LOGIN,
    PREPARE_READING,
    MINDMAP_GENERATE,
    MINDMAP_UPDATE,
    MINDMAP_EXPLAIN,
    STUDY_AUDIO_GENERATE,
    DOCUMENTARY_GENERATE,
    QUIZ,
    START_SESSION,
    STUDENT_PROGRESS,
    STUDY_PLAN,
    SUMMARIZE_MATERIAL,
)



class TeacherMCPServer:
    def __init__(self, service: TeacherService) -> None:
        self.service = service

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "description": DESCRIPTIONS[name],
                "inputSchema": model.model_json_schema(),
            }
            for name, model in TOOL_MODELS.items()
        ]

    def call(
        self,
        name: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        model = TOOL_MODELS.get(name)

        if model is None or not isinstance(arguments, Mapping):
            return _error("POLICY_DENIED")

        try:
            validated = model.model_validate(arguments)
        except ValidationError:
            return _error("INVALID_INPUT")

        values = validated.model_dump()

        try:
            if name == LOGIN:
                payload = self.service.login(**values)
            elif name == START_SESSION:
                payload = self.service.start_session(**values)
            elif name == EXPLAIN:
                payload = self.service.explain(**values)
            elif name == EXPLAIN_DIFFERENTLY:
                payload = self.service.explain_differently(**values)
            elif name == HINT:
                payload = self.service.hint(**values)
            elif name == GENERATE_EXERCISE:
                payload = self.service.generate_exercise(**values)
            elif name == CHECK_ANSWER:
                payload = self.service.check_answer(**values)
            elif name == QUIZ:
                payload = self.service.quiz(**values)
            elif name == STUDY_PLAN:
                payload = self.service.study_plan(**values)
            elif name == SUMMARIZE_MATERIAL:
                payload = self.service.summarize_material(**values)
            elif name == STUDENT_PROGRESS:
                payload = self.service.student_progress(**values)
            elif name == END_SESSION:
                payload = self.service.end_session(**values)
            elif name == PREPARE_READING:
                payload = self.service.prepare_reading(**values)
            elif name == MINDMAP_GENERATE:
                payload = self.service.mindmap_generate(**values)
            elif name == MINDMAP_UPDATE:
                payload = self.service.mindmap_update(**values)
            elif name == MINDMAP_EXPLAIN:
                payload = self.service.mindmap_explain(**values)
            elif name == STUDY_AUDIO_GENERATE:
                payload = self.service.study_audio_generate(**values)
            elif name == DOCUMENTARY_GENERATE:
                payload = self.service.documentary_generate(**values)
            else:
                return _error("POLICY_DENIED")
        except KeyError as exc:
            return _error(str(exc.args[0]).upper())
        except ValueError as exc:
            return _error(str(exc).upper())
        except Exception:
            return _error("SOURCE_UNAVAILABLE")

        payload.setdefault("writes", 0)
        payload.setdefault("sends", 0)
        payload.setdefault("external_side_effects", 0)

        return _tool_result(payload)

    def stream(
        self,
        name: str,
        arguments: Mapping[str, Any],
    ):
        """Stream only pedagogical text operations; never widen tools/capabilities."""
        if name not in {EXPLAIN, EXPLAIN_DIFFERENTLY, HINT}:
            raise ValueError("POLICY_DENIED")
        model = TOOL_MODELS.get(name)
        if model is None or not isinstance(arguments, Mapping):
            raise ValueError("INVALID_INPUT")
        try:
            values = model.model_validate(arguments).model_dump()
        except ValidationError as exc:
            raise ValueError("INVALID_INPUT") from exc

        if name == EXPLAIN:
            events = self.service.stream_explain(**values)
        elif name == EXPLAIN_DIFFERENTLY:
            events = self.service.stream_explain_differently(**values)
        else:
            events = self.service.stream_hint(**values)

        for event in events:
            if not isinstance(event, dict) or event.get("type") not in {"delta", "done"}:
                raise RuntimeError("invalid_teacher_stream_event")
            if event["type"] == "done":
                payload = event.get("result")
                if not isinstance(payload, dict):
                    raise RuntimeError("invalid_teacher_stream_result")
                payload.setdefault("writes", 0)
                payload.setdefault("sends", 0)
                payload.setdefault("external_side_effects", 0)
                yield {"type": "done", "result": _tool_result(payload)}
            else:
                text = event.get("text")
                if isinstance(text, str) and text:
                    yield {"type": "delta", "text": text}


def _tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{
            "type": "text",
            "text": json.dumps(payload, ensure_ascii=False),
        }],
        "structuredContent": payload,
        "isError": not bool(payload.get("ok")),
    }


def _error(code: str) -> dict[str, Any]:
    payload = {
        "ok": False,
        "error": code,
        "writes": 0,
        "sends": 0,
        "external_side_effects": 0,
    }
    return {
        "content": [{
            "type": "text",
            "text": code,
        }],
        "structuredContent": payload,
        "isError": True,
    }


def _response(
    request: Mapping[str, Any],
    server: TeacherMCPServer,
) -> dict[str, Any] | None:
    method = request.get("method")

    if method == "notifications/initialized":
        return None

    request_id = request.get("id")

    if method == "initialize":
        result = {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {
                "name": "ralf-teacher",
                "version": "1",
            },
        }

    elif method == "tools/list":
        result = {"tools": server.list_tools()}

    elif method == "tools/call":
        params = request.get("params")

        if not isinstance(params, Mapping):
            result = _error("INVALID_INPUT")
        else:
            result = server.call(
                str(params.get("name") or ""),
                params.get("arguments", {}),
            )

    else:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32601,
                "message": "method_not_found",
            },
        }

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result,
    }


def _write_message(payload: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _stream_response(request: Mapping[str, Any], server: TeacherMCPServer) -> bool:
    if request.get("method") != "teacher/stream":
        return False
    request_id = request.get("id")
    params = request.get("params")
    if request_id is None or not isinstance(params, Mapping):
        _write_message({"jsonrpc": "2.0", "id": request_id, "result": _error("INVALID_INPUT")})
        return True
    name = str(params.get("name") or "")
    arguments = params.get("arguments", {})
    final = None
    try:
        for event in server.stream(name, arguments):
            if event["type"] == "delta":
                _write_message({
                    "jsonrpc": "2.0",
                    "method": "teacher/stream/event",
                    "params": {"requestId": request_id, "event": event},
                })
            else:
                final = event["result"]
    except (KeyError, ValueError) as exc:
        final = _error(str(exc.args[0] if exc.args else exc).upper())
    except Exception:
        final = _error("SOURCE_UNAVAILABLE")
    if final is None:
        final = _error("SOURCE_UNAVAILABLE")
    _write_message({"jsonrpc": "2.0", "id": request_id, "result": final})
    return True


def main() -> int:
    store = TeacherStore()

    inference_socket = os.getenv(
        "RALF_TEACHER_INFERENCE_SOCKET",
        "",
    ).strip()

    model_call = (
        TeacherInferenceClient(inference_socket)
        if inference_socket
        else None
    )

    grammar_socket = os.getenv(
        "RALF_TEACHER_GRAMMAR_SOCKET",
        "",
    ).strip()
    grammar_client = (
        GrammarEvidenceClient(grammar_socket)
        if grammar_socket
        else None
    )

    core_socket = os.getenv(
        "RALF_TEACHER_CORE_SOCKET",
        "",
    ).strip()
    core_client = (
        TeacherCoreClient(core_socket)
        if core_socket
        else None
    )

    service = TeacherService(
        store,
        model_call=model_call,
        grammar_evidence=(
            grammar_client.evidence_for
            if grammar_client is not None
            else None
        ),
        deterministic_core=core_client,
    )
    server = TeacherMCPServer(service)

    def shutdown_handler(
        _signum: int,
        _frame: Any,
    ) -> None:
        service.close()
        raise SystemExit(0)

    signal.signal(
        signal.SIGTERM,
        shutdown_handler,
    )

    try:
        for line in sys.stdin:
            try:
                request = json.loads(line)

                if not isinstance(request, Mapping):
                    raise ValueError("request_not_object")

                if _stream_response(request, server):
                    continue
                response = _response(request, server)

            except Exception:
                response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32603,
                        "message": "internal_error",
                    },
                }

            if response is not None:
                _write_message(response)
    finally:
        service.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
