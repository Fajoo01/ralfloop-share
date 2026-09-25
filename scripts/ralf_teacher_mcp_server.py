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

from ralfloop_agent.teacher.models import (
    CheckAnswerInput,
    EndSessionInput,
    ExplainDifferentlyInput,
    ExplainInput,
    GenerateExerciseInput,
    HintInput,
    LoginInput,
    PrepareReadingInput,
    MindMapGenerateInput,
    MindMapUpdateInput,
    MindMapExplainInput,
    StudyAudioGenerateInput,
    DocumentaryGenerateInput,
    QuizInput,
    StartSessionInput,
    StudentProgressInput,
    StudyPlanInput,
    SummarizeMaterialInput,
)
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


TOOL_MODELS = {
    LOGIN: LoginInput,
    START_SESSION: StartSessionInput,
    EXPLAIN: ExplainInput,
    EXPLAIN_DIFFERENTLY: ExplainDifferentlyInput,
    HINT: HintInput,
    GENERATE_EXERCISE: GenerateExerciseInput,
    CHECK_ANSWER: CheckAnswerInput,
    QUIZ: QuizInput,
    STUDY_PLAN: StudyPlanInput,
    SUMMARIZE_MATERIAL: SummarizeMaterialInput,
    STUDENT_PROGRESS: StudentProgressInput,
    END_SESSION: EndSessionInput,
    PREPARE_READING: PrepareReadingInput,
    MINDMAP_GENERATE: MindMapGenerateInput,
    MINDMAP_UPDATE: MindMapUpdateInput,
    MINDMAP_EXPLAIN: MindMapExplainInput,
    STUDY_AUDIO_GENERATE: StudyAudioGenerateInput,
    DOCUMENTARY_GENERATE: DocumentaryGenerateInput,
}


DESCRIPTIONS = {
    LOGIN:
        "Resolve a Tiremm Innanz student card into an internal student profile.",
    START_SESSION:
        "Start a teaching session for a student, subject and topic.",
    EXPLAIN:
        "Explain a concept at the student's school level.",
    EXPLAIN_DIFFERENTLY:
        "Explain the same concept again using a different approach.",
    HINT:
        "Give one progressive hint without revealing the final solution.",
    GENERATE_EXERCISE:
        "Generate an exercise adapted to the student.",
    CHECK_ANSWER:
        "Check a student's answer and guide correction.",
    QUIZ:
        "Generate a short quiz or oral-exam style interrogation.",
    STUDY_PLAN:
        "Create a bounded study plan.",
    SUMMARIZE_MATERIAL:
        "Summarize only the supplied study material.",
    STUDENT_PROGRESS:
        "Read the student's persisted learning progress.",
    END_SESSION:
        "End an active teaching session.",
    PREPARE_READING:
        "Prepare supplied material for future TTS/audiobook reading.",
    MINDMAP_GENERATE:
        "Generate a bounded editable mind map only from supplied study material.",
    MINDMAP_UPDATE:
        "Revise an existing mind map according to the student's instruction.",
    MINDMAP_EXPLAIN:
        "Explain one selected node of an existing mind map.",
    STUDY_AUDIO_GENERATE:
        "Create a dyslexia-friendly narrated study script and bounded audio handoff.",
    DOCUMENTARY_GENERATE:
        "Create a short educational documentary script and bounded audio handoff.",
}


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
