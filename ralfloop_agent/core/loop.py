from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from ralfloop_agent.core.state import AgentState, MemoryEntry, PlanStep
from ralfloop_agent.core.decisions import ActionDecision
from ralfloop_agent.logging.audit import AuditLogger
from ralfloop_agent.contracts.completion_policy import evaluate_completion_stop
from ralfloop_agent.contracts.final_answer_renderer import render_final_answer
from ralfloop_agent.contracts.tool_dispatch import dispatch_tool
from ralfloop_agent.contracts.read_file_postprocess import handle_read_file_result
from ralfloop_agent.contracts.role_helpers import next_role, profile_for_role, model_name_for_role, rag_for_role, planner_for_role
from ralfloop_agent.contracts.state_transitions import advance_role_and_iteration, finalize_state
from ralfloop_agent.contracts.seeded_context_bootstrap import choose_seeded_context_action




class RalfloopAgent:
    def __init__(self, adapter, planner, coder_planner=None, judge_planner=None, logger: AuditLogger | None = None) -> None:
        self.adapter = adapter
        self.planner = planner
        self.coder_planner = coder_planner or planner
        self.judge_planner = judge_planner or planner
        self.logger = logger or AuditLogger()

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

        adapter_sandbox = {
            "id": state.sandbox.id,
            "root": state.sandbox.workspace_path,
            "status": state.sandbox.status,
        }

        seed_files = {
            "user_goal.txt": user_goal,
            "skill_context.txt": str(ctx.get("skill_context", "") or ""),
            "extra_context.json": json.dumps(ctx.get("extra_context", {}) or {}, ensure_ascii=False, indent=2),
            "workspace_manifest.txt": (
                "Seed files available at task start:\n"
                "- user_goal.txt\n"
                "- skill_context.txt\n"
                "- extra_context.json\n"
                "Read only these files for initial context unless you create new files yourself.\n"
            ),
        }
        for seed_path, seed_content in seed_files.items():
            seed_result = self.adapter.write_file(adapter_sandbox, path=seed_path, content=seed_content)
            self.logger.log(
                task_id=state.task_id,
                iteration=state.iteration,
                tool_name="sandbox_write_file",
                tool_input={"path": seed_path},
                tool_output=seed_result.model_dump(),
                decision="seed",
            )

        try:
            while state.iteration < state.max_iterations:
                state.role_history.append(state.current_role)
                state.memory.append(
                    MemoryEntry(
                        kind="decision",
                        content=f"role::{state.current_role}::profile::{profile_for_role(state)}::model::{model_name_for_role(state)}::rag::{rag_for_role(state)}"
                    )
                )

                decision = choose_seeded_context_action(
                    state,
                    planner_for_role(self, state).choose_next_action,
                )
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
                      "model_profile": profile_for_role(state),
                      "model_name": model_name_for_role(state),
                      "rag_collection": rag_for_role(state),
                  }
                state.action_history.append({
                    "tool_name": decision.tool_name,
                    "tool_input": dict(decision.tool_input or {}),
                    "role": state.current_role,
                })
                self.logger.log(
                    task_id=state.task_id,
                    iteration=state.iteration,
                    tool_name=decision.tool_name,
                    tool_input=decision.tool_input,
                    decision="act",
                )

                result = self._dispatch(adapter_sandbox, decision.tool_name, decision.tool_input)
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
                    read_outcome = handle_read_file_result(state, decision, result)
                    result = read_outcome["result"]
                    state.last_result = result
                    if self._should_stop(state):
                        break
                    if read_outcome["force_role_advance"]:
                        state.current_role = next_role(state.current_role)
                    if read_outcome["force_iteration_advance"]:
                        state.iteration += 1
                    if read_outcome["should_continue"]:
                        continue

                if result.ok:
                    if self._should_stop(state):
                        break
                    state.current_role = next_role(state.current_role)
                    state.iteration += 1
                    continue

                state.consecutive_failures += 1
                state.memory.append(MemoryEntry(kind="warning", content=f"{decision.tool_name}: {result.stderr or 'failed'}"))
                if self._should_stop(state):
                    break
                advance_role_and_iteration(state, next_role)

            finalize_state(state, self._build_final_answer)
        finally:
            self.adapter.destroy_sandbox(state.sandbox.id or "")
            state.sandbox.status = "destroyed"
            state.sandbox.destroyed_at = datetime.now(UTC)
            self.logger.log(task_id=state.task_id, iteration=state.iteration, decision="sandbox_destroyed", sandbox_id=state.sandbox.id)

        return state

    def _dispatch(self, sandbox: dict[str, Any], tool_name: str, tool_input: dict[str, Any]):
        return dispatch_tool(self.adapter, sandbox, tool_name, tool_input)

    def _should_stop(self, state: AgentState) -> bool:
        return evaluate_completion_stop(state, self.planner)

    def _build_final_answer(self, state: AgentState) -> str:
        return render_final_answer(state)
