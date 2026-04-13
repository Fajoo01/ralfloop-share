from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from ralfloop_agent.core.state import AgentState, MemoryEntry, PlanStep
from ralfloop_agent.logging.audit import AuditLogger




def _next_role(current: str) -> str:
    if current == "planner":
        return "coder"
    if current == "coder":
        return "judge"
    return "planner"


def _profile_for_role(state) -> str:
    if state.current_role == "planner":
        return state.planner_model_profile
    if state.current_role == "coder":
        return state.coder_model_profile
    return state.judge_model_profile


def _model_name_for_role(state) -> str:
    if state.current_role == "planner":
        return state.planner_model_name
    if state.current_role == "coder":
        return state.coder_model_name
    return state.judge_model_name


def _rag_for_role(state) -> str:
    if state.current_role == "planner":
        return state.planner_rag_collection
    if state.current_role == "coder":
        return state.coder_rag_collection
    return state.judge_rag_collection


class RalfloopAgent:
    def __init__(self, adapter, planner, coder_planner=None, judge_planner=None, logger: AuditLogger | None = None) -> None:
        self.adapter = adapter
        self.planner = planner
        self.coder_planner = coder_planner or planner
        self.judge_planner = judge_planner or planner
        self.logger = logger or AuditLogger()

    def _planner_for_role(self, state):
        if state.current_role == "planner":
            return self.planner
        if state.current_role == "coder":
            return self.coder_planner
        return self.judge_planner

    def run(self, user_goal: str, constraints: list[str] | None = None, context: dict[str, Any] | None = None) -> AgentState:
        ctx = context or {}
        state = AgentState(
            user_goal=user_goal,
            constraints=constraints or [],
            context=ctx,
            status="running",
            planner_model_profile=ctx.get("planner_model_profile", "generalist"),
            coder_model_profile=ctx.get("coder_model_profile", "coder"),
            judge_model_profile=ctx.get("judge_model_profile", "generalist"),
            planner_model_name=ctx.get("planner_model_name", "qwen2.5:7b"),
            coder_model_name=ctx.get("coder_model_name", "qwen2.5:7b"),
            judge_model_name=ctx.get("judge_model_name", "qwen2.5:7b"),
            planner_rag_collection=ctx.get("planner_rag_collection", "ralfloop_planner"),
            coder_rag_collection=ctx.get("coder_rag_collection", "ralfloop_coder"),
            judge_rag_collection=ctx.get("judge_rag_collection", "ralfloop_judge"),
        )
        sandbox_info = self.adapter.create_sandbox()
        state.sandbox.id = sandbox_info["id"]
        state.sandbox.status = "ready"
        state.sandbox.workspace_path = sandbox_info["root"]
        state.sandbox.created_at = datetime.now(UTC)

        self.logger.log(task_id=state.task_id, iteration=state.iteration, decision="sandbox_created", sandbox_id=state.sandbox.id)

        try:
            while state.iteration < state.max_iterations:
                state.role_history.append(state.current_role)
                state.memory.append(
                    MemoryEntry(
                        kind="decision",
                        content=f"role::{state.current_role}::profile::{_profile_for_role(state)}::model::{_model_name_for_role(state)}::rag::{_rag_for_role(state)}"
                    )
                )

                decision = self._planner_for_role(state).choose_next_action(state.user_goal, state.iteration)
                state.plan.append(
                    PlanStep(
                        step_id=f"step-{state.iteration+1}",
                        description=f"[{state.current_role}] {decision.why}"
                    )
                )
                state.last_action = {
                      "tool_name": decision.tool_name,
                      "tool_input": decision.tool_input,
                      "role": state.current_role,
                      "model_profile": _profile_for_role(state),
                      "model_name": _model_name_for_role(state),
                      "rag_collection": _rag_for_role(state),
                  }
                self.logger.log(
                    task_id=state.task_id,
                    iteration=state.iteration,
                    tool_name=decision.tool_name,
                    tool_input=decision.tool_input,
                    decision="act",
                )

                adapter_sandbox = {
                    "id": state.sandbox.id,
                    "root": state.sandbox.workspace_path,
                    "status": state.sandbox.status,
                }
                print("[LOOP] role=", state.current_role, "iteration=", state.iteration, "tool=", decision.tool_name, "tool_input=", decision.tool_input, flush=True)
                result = self._dispatch(adapter_sandbox, decision.tool_name, decision.tool_input)
                print("[LOOP] result_ok=", result.ok, "tool_name=", result.tool_name, "stdout=", (result.stdout or "")[:500], "stderr=", (result.stderr or "")[:500], flush=True)
                state.last_result = result
                self.logger.log(
                    task_id=state.task_id,
                    iteration=state.iteration,
                    tool_name=decision.tool_name,
                    tool_output=result.model_dump(),
                    decision="evaluate",
                )

                if result.ok:
                    state.consecutive_failures = 0
                    state.memory.append(MemoryEntry(kind="result", content=f"{decision.tool_name}: ok"))
                if decision.tool_name == "sandbox_read_file":
                    raw_path = decision.tool_input.get("path")
                    if raw_path is None:
                        raw_path = decision.tool_input.get("filename")
                    path = str(raw_path or "")

                    if path.startswith("http://") or path.startswith("https://"):
                        decision.tool_name = "sandbox_http_fetch"
                        decision.tool_input = {"url": path, "method": "GET"}
                    elif not path or path.startswith("/") or ".." in path:
                        result = ToolResult(
                            tool_name=decision.tool_name,
                            ok=False,
                            stdout="",
                            stderr="policy_denied",
                        )
                        state.consecutive_failures += 1
                        state.last_action = decision.model_dump()
                        state.last_result = result
                        state.audit_summary.append(f"{decision.tool_name}: policy_denied")
                        state.plan.append(
                            PlanStep(
                                iteration=state.iteration,
                                role=state.current_role,
                                description=decision.why,
                                tool_name=decision.tool_name,
                                status="error",
                                summary="policy_denied",
                            )
                        )
                        if self._should_stop(state):
                            break
                        state.iteration += 1
                        continue

                    state.current_role = _next_role(state.current_role)
                    state.iteration += 1

            if state.last_result and state.last_result.ok:
                state.status = "completed"
                state.stop_reason = state.stop_reason or "goal_completed"
                state.final_answer = self._build_final_answer(state)
            else:
                state.status = "failed"
                state.stop_reason = state.stop_reason or "repeated_failure"
                state.final_answer = "Task non completato."
        finally:
            self.adapter.destroy_sandbox(state.sandbox.id or "")
            state.sandbox.status = "destroyed"
            state.sandbox.destroyed_at = datetime.now(UTC)
            self.logger.log(task_id=state.task_id, iteration=state.iteration, decision="sandbox_destroyed", sandbox_id=state.sandbox.id)

        return state

    def _dispatch(self, sandbox: dict[str, Any], tool_name: str, tool_input: dict[str, Any]):
        tool_input = dict(tool_input or {})

        if tool_name == "sandbox_read_file":
            if "filename" in tool_input and "path" not in tool_input:
                tool_input["path"] = tool_input.pop("filename")

            path = str(tool_input.get("path", "") or "")
            if path.startswith("http://") or path.startswith("https://"):
                tool_name = "sandbox_http_fetch"
                tool_input = {"url": path, "method": "GET"}

        if tool_name == "sandbox_write_file":
            if "filename" in tool_input and "path" not in tool_input:
                tool_input["path"] = tool_input.pop("filename")

        if tool_name == "sandbox_exec":
            return self.adapter.exec(sandbox, **tool_input)
        if tool_name == "sandbox_write_file":
            return self.adapter.write_file(sandbox, **tool_input)
        if tool_name == "sandbox_read_file":
            return self.adapter.read_file(sandbox, **tool_input)
        if tool_name == "sandbox_list_dir":
            return self.adapter.list_dir(sandbox, **tool_input)
        if tool_name == "sandbox_http_fetch":
            return self.adapter.http_fetch(sandbox, **tool_input)
        raise ValueError(f"Unsupported tool: {tool_name}")

    def _should_stop(self, state: AgentState) -> bool:
        if state.consecutive_failures >= 3:
            state.stop_reason = "repeated_failure"
            return True

        if state.last_action and state.last_result and state.last_result.ok:
            tool_name = state.last_action["tool_name"]
            goal = state.user_goal.lower()

            is_three_step = (
                ("scrivi" in goal or "write" in goal)
                and ("mostrami i file" in goal or "show me the files" in goal)
                and ("poi leggi" in goal or "then read" in goal)
            )

            is_two_step = (
                ("scrivi" in goal or "write" in goal)
                and ("poi leggi" in goal or "then read" in goal)
            )

            is_multi_file = ("leggili" in goal or "read them" in goal)

            if tool_name in {"sandbox_read_file", "sandbox_http_fetch"}:
                if tool_name == "sandbox_read_file" and is_multi_file:
                    write_pairs = []
                    if hasattr(self.planner, "fallback") and hasattr(self.planner.fallback, "_extract_write_pairs"):
                        write_pairs = self.planner.fallback._extract_write_pairs(state.user_goal)
                    elif hasattr(self.planner, "_extract_write_pairs"):
                        write_pairs = self.planner._extract_write_pairs(state.user_goal)

                    expected_reads = len(write_pairs) if write_pairs else 2

                    read_done = 0
                    for step in state.plan:
                        desc = getattr(step, "description", "")
                        status = getattr(step, "status", "")
                        if status == "done" and desc.lower().startswith("leggo il file richiesto"):
                            read_done += 1

                    if read_done < expected_reads:
                        return False

                state.stop_reason = "goal_completed"
                return True

            if tool_name == "sandbox_list_dir":
                if is_three_step:
                    return False
                if not is_two_step:
                    state.stop_reason = "goal_completed"
                    return True

        if state.iteration + 1 >= state.max_iterations:
            state.stop_reason = "max_iterations_reached"
            return True

        return False

    def _build_final_answer(self, state: AgentState) -> str:
        result = state.last_result
        if result is None:
            return "Nessun risultato disponibile."

        if result.tool_name == "sandbox_http_fetch":
            try:
                payload = json.loads(result.stdout)
                text_parts = []

                if isinstance(payload, dict):
                    for k in ("body", "text", "stdout", "content"):
                        v = payload.get(k)
                        if isinstance(v, str):
                            text_parts.append(v)

                blob = "\n".join(text_parts)
                m = re.search(r'https?://[^\s"\']+\.(?:m3u8|mpd)[^\s"\']*', blob, re.I)
                if m:
                    return json.dumps({"stream_url": m.group(0), "headers": {}}, ensure_ascii=False)

                if isinstance(payload, dict) and isinstance(payload.get("models"), list):
                    names = [m.get("name") for m in payload["models"] if isinstance(m, dict) and m.get("name")]
                    if names:
                        return "Modelli Ollama disponibili:\n" + "\n".join(f"- {name}" for name in names)
            except Exception:
                pass

        if result.tool_name == "sandbox_list_dir":
            lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            requested_path = "."
            if state.last_action and isinstance(state.last_action, dict):
                requested_path = state.last_action.get("tool_input", {}).get("path", ".")
            if lines:
                title = "Contenuto della workspace:" if requested_path == "." else f"Contenuto della directory {requested_path}:"
                return title + "\n" + "\n".join(f"- {line}" for line in lines)
            return "La workspace è vuota." if requested_path == "." else f"La directory {requested_path} è vuota."

        if result.tool_name == "sandbox_read_file":
            goal = state.user_goal.lower()
            is_multi_file = ("leggili" in goal or "read them" in goal)

            if is_multi_file:
                collected: list[tuple[str, str]] = []
                for mem in state.memory:
                    content = getattr(mem, "content", "")
                    if content.startswith("file_read::"):
                        _, path, body = content.split("::", 2)
                        collected.append((path, body))
                if collected:
                    return "Contenuto dei file:\n" + "\n\n".join(
                        f"{path}:\n{body}" for path, body in collected
                    )

            return "Contenuto del file:\n" + result.stdout.strip()

        return f"Task completato. Ultimo tool: {result.tool_name}. Output:\n{result.stdout.strip()}"
