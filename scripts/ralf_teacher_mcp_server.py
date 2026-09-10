#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import signal
import sys
from typing import Any, Mapping

from pydantic import ValidationError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ralfloop_agent.teacher.catalog import DESCRIPTIONS, TOOL_MODELS
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


def main() -> int:
    store = TeacherStore()
    service = TeacherService(store)
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
                sys.stdout.write(
                    json.dumps(
                        response,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                sys.stdout.flush()
    finally:
        service.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
