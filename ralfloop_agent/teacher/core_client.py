from __future__ import annotations

from typing import Any

from src.mcp_transport import MCPClientSession, UnixMCPTransport


TOOLS = {
    "core.text_profile",
    "core.extractive_summary",
    "core.study_plan",
    "core.math_check",
    "core.math_hint",
    "core.fraction_relation",
    "core.classify_turn",
    "core.concept_evidence",
}


class TeacherCoreClient:
    """Client for the internal deterministic C Teacher core."""

    def __init__(self, socket_path: str, *, timeout: float = 3.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def _connection(self) -> MCPClientSession:
        return MCPClientSession(
            UnixMCPTransport(self.socket_path),
            timeout=self.timeout,
            client_name="teacher-deterministic-core",
        )

    @staticmethod
    def _call(
        session: MCPClientSession,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        raw = session.call_tool(name, arguments)
        payload = raw.get("structuredContent")
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise RuntimeError("teacher_core_invalid_result")
        if payload.get("writes") != 0 or payload.get("external_side_effects") != 0:
            raise RuntimeError("teacher_core_side_effect_contract_broken")
        return payload

    @staticmethod
    def _surface(session: MCPClientSession) -> None:
        surface = {tool.name for tool in session.list_tools()}
        if surface != TOOLS:
            raise RuntimeError("teacher_core_surface_mismatch")

    def text_profile(self, text: str) -> dict[str, Any]:
        with self._connection() as session:
            self._surface(session)
            return self._call(session, "core.text_profile", {"text": text[:60000]})

    def extractive_summary(self, text: str, *, max_sentences: int = 4) -> dict[str, Any]:
        with self._connection() as session:
            self._surface(session)
            return self._call(
                session,
                "core.extractive_summary",
                {"text": text[:60000], "max_sentences": max(1, min(max_sentences, 8))},
            )

    def study_plan(self, minutes: int, mode: str) -> dict[str, Any]:
        with self._connection() as session:
            self._surface(session)
            return self._call(
                session,
                "core.study_plan",
                {"minutes": max(10, min(int(minutes), 240)), "mode": mode},
            )

    def math_check(self, text: str, answer: str) -> dict[str, Any]:
        with self._connection() as session:
            self._surface(session)
            return self._call(
                session,
                "core.math_check",
                {"text": text[:16000], "answer": answer[:256]},
            )

    def math_hint(self, text: str, attempt: str = "") -> dict[str, Any]:
        with self._connection() as session:
            self._surface(session)
            return self._call(
                session,
                "core.math_hint",
                {"text": text[:16000], "attempt": attempt[:512]},
            )

    def fraction_relation(self, text: str) -> dict[str, Any]:
        with self._connection() as session:
            self._surface(session)
            return self._call(
                session,
                "core.fraction_relation",
                {"text": text[:2000]},
            )

    def classify_turn(self, text: str) -> dict[str, Any]:
        with self._connection() as session:
            self._surface(session)
            return self._call(
                session,
                "core.classify_turn",
                {"text": text[:6000]},
            )

    def concept_evidence(self, topic: str) -> dict[str, Any]:
        with self._connection() as session:
            self._surface(session)
            return self._call(
                session,
                "core.concept_evidence",
                {"topic": topic[:300]},
            )


__all__ = ["TeacherCoreClient", "TOOLS"]
