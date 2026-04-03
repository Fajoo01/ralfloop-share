from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ralfloop_agent.core.state import AgentState, MemoryEntry, PlanStep
from ralfloop_agent.logging.audit import AuditLogger


class RalfloopAgent:
    def __init__(self, adapter, planner, logger: AuditLogger | None = None) -> None:
        self.adapter = adapter
        self.planner = planner
        self.logger = logger or AuditLogger()

    def run(self, user_goal: str, constraints: list[str] | None = None, context: dict[str, Any] | None = None) -> AgentState:
        state = AgentState(user_goal=user_goal, constraints=constraints or [], context=context or {}, status="running")
        sandbox_info = self.adapter.create_sandbox()
        state.sandbox.id = sandbox_info["id"]
        state.sandbox.status = "ready"
        state.sandbox.workspace_path = sandbox_info["root"]
        state.sandbox.created_at = datetime.now(UTC)

        self.logger.log(task_id=state.task_id, iteration=state.iteration, decision="sandbox_created", sandbox_id=state.sandbox.id)

        try:
            while state.iteration < state.max_iterations:
                decision = self.planner.choose_next_action(state.user_goal, state.iteration)
                state.plan.append(PlanStep(step_id=f"step-{state.iteration+1}", description=decision.why))
                state.last_action = {"tool_name": decision.tool_name, "tool_input": decision.tool_input}
                self.logger.log(task_id=state.task_id, iteration=state.iteration, tool_name=decision.tool_name, tool_input=decision.tool_input, decision="act")

                adapter_sandbox = {
                    "id": state.sandbox.id,
                    "root": state.sandbox.workspace_path,
                    "status": state.sandbox.status,
                }
                result = self._dispatch(adapter_sandbox, decision.tool_name, decision.tool_input)
                state.last_result = result
                self.logger.log(task_id=state.task_id, iteration=state.iteration, tool_name=decision.tool_name, tool_output=result.model_dump(), decision="evaluate")

                if result.ok:
                    state.consecutive_failures = 0
                    state.memory.append(MemoryEntry(kind="result", content=f"{decision.tool_name}: ok"))
                    state.plan[-1].status = "done"
                else:
                    state.consecutive_failures += 1
                    state.memory.append(MemoryEntry(kind="warning", content=f"{decision.tool_name}: {result.error_type or 'failed'}"))
                    state.plan[-1].status = "failed"

                if self._should_stop(state):
                    break

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
            if state.last_action["tool_name"] in {"sandbox_read_file", "sandbox_http_fetch", "sandbox_list_dir"}:
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
            return "Contenuto del file:\n" + result.stdout.strip()

        return f"Task completato. Ultimo tool: {result.tool_name}. Output:\n{result.stdout.strip()}"
