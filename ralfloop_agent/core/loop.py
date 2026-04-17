from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ralfloop_agent.core.state import AgentState, MemoryEntry, PlanStep
from ralfloop_agent.core.decisions import ActionDecision
from ralfloop_agent.logging.audit import AuditLogger
from ralfloop_agent.contracts.completion_policy import evaluate_completion_stop
from ralfloop_agent.contracts.final_answer_renderer import render_final_answer
from ralfloop_agent.contracts.tool_dispatch import dispatch_tool
from ralfloop_agent.contracts.read_file_postprocess import handle_read_file_result
from ralfloop_agent.contracts.role_helpers import next_role, profile_for_role, model_name_for_role, rag_for_role, planner_for_role
from ralfloop_agent.contracts.runtime_multifile_guard import apply_runtime_multifile_guard
from ralfloop_agent.contracts.state_transitions import advance_role_and_iteration, finalize_state
from ralfloop_agent.contracts.seeded_context_bootstrap import choose_seeded_context_action
from ralfloop_agent.contracts.seed_writer import write_seed_files


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
            internal_prompt_style=ctx.get("internal_prompt_style", "standard"),
        )
        sandbox_info = self.adapter.create_sandbox(source_root=ctx.get("workspace_root") or ctx.get("cwd"))
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

        write_seed_files(state, ctx, self.adapter, adapter_sandbox, self.logger)

        try:
            while state.iteration < state.max_iterations:
                state.role_history.append(state.current_role)
                state.memory.append(
                    MemoryEntry(
                        kind="decision",
                        content=f"role::{state.current_role}::profile::{profile_for_role(state)}::model::{model_name_for_role(state)}::rag::{rag_for_role(state)}"
                    )
                )

                active_planner = planner_for_role(self, state)
                decision = choose_seeded_context_action(
                    state,
                    active_planner.choose_next_action,
                )
                decision = apply_runtime_multifile_guard(state, decision, active_planner)
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
                self._record_runtime_evidence(state, decision, result)

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
            self._write_runtime_summaries(state, adapter_sandbox)
        finally:
            keep_sandbox = bool(
                ctx.get("keep_sandbox")
                or (ctx.get("extra_context", {}) or {}).get("keep_sandbox")
            )
            if not keep_sandbox:
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

    def _record_runtime_evidence(self, state: AgentState, decision: ActionDecision, result: Any) -> None:
        if result.ok:
            for artifact in list(getattr(result, "artifacts", []) or []):
                if artifact and artifact not in state.artifacts:
                    state.artifacts.append(artifact)

        path = str((decision.tool_input or {}).get("path", "") or "").strip()
        if not result.ok or not path:
            return
        if decision.tool_name == "sandbox_write_file" and path not in state.written_files:
            state.written_files.append(path)
        if decision.tool_name == "sandbox_read_file" and path not in state.read_files:
            state.read_files.append(path)

    def _write_runtime_summaries(self, state: AgentState, sandbox: dict[str, Any]) -> None:
        planned_paths = [
            "tmp/debug/run_scorecard.json",
            "tmp/debug/session_summary.json",
        ]
        sandbox_root = Path(str(sandbox.get("root", "") or ""))
        for rel_path in planned_paths:
            absolute_path = str(sandbox_root / rel_path)
            if absolute_path not in state.artifacts:
                state.artifacts.append(absolute_path)
            if rel_path not in state.written_files:
                state.written_files.append(rel_path)

        scorecard = self._build_run_scorecard(state)
        session_summary = self._build_session_summary(state)
        generated_paths: list[str] = []

        for rel_path, payload in (
            (planned_paths[0], scorecard),
            (planned_paths[1], session_summary),
        ):
            write_result = self.adapter.write_file(
                sandbox,
                path=rel_path,
                content=json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            )
            if write_result.ok:
                for artifact in list(write_result.artifacts or []):
                    if artifact and artifact not in state.artifacts:
                        state.artifacts.append(artifact)
                    if artifact and artifact not in generated_paths:
                        generated_paths.append(artifact)
            else:
                state.audit_summary.append(f"runtime_summary_write_failed::{rel_path}::{write_result.stderr or write_result.error_type or 'unknown'}")

        if state.last_result is not None:
            merged = list(state.last_result.artifacts or [])
            for artifact in generated_paths:
                if artifact not in merged:
                    merged.append(artifact)
            state.last_result.artifacts = merged

        runtime_debug = list((state.context or {}).get("runtime_debug", []) or [])
        runtime_debug.append(
            {
                "action": "runtime_temp_summaries_written",
                "paths": generated_paths,
                "storage": "temporary_runtime_only",
                "ingest": False,
                "vectorized": False,
            }
        )
        state.context["runtime_debug"] = runtime_debug[-20:]

    def _build_run_scorecard(self, state: AgentState) -> dict[str, Any]:
        evidence_items = []
        for path in state.read_files:
            evidence_items.append({"type": "read_file", "path": path})
        for path in state.written_files:
            evidence_items.append({"type": "written_file", "path": path})

        evidence_backed = state.status == "completed" and bool(state.read_files or state.last_result)
        return {
            "goal": {
                "user_goal": state.user_goal,
                "constraints": list(state.constraints),
            },
            "execution": {
                "task_id": state.task_id,
                "status": state.status,
                "stop_reason": state.stop_reason,
                "iterations_completed": state.iteration,
                "current_role": state.current_role,
                "role_history": list(state.role_history),
                "last_action": dict(state.last_action or {}),
            },
            "evidence": {
                "read_files": list(state.read_files),
                "written_files": list(state.written_files),
                "artifacts": list(state.artifacts),
                "items": evidence_items,
                "runtime_debug_tail": list((state.context or {}).get("runtime_debug", []) or []),
            },
            "result_quality": {
                "final_answer_present": bool((state.final_answer or "").strip()),
                "last_result_ok": bool(getattr(state.last_result, "ok", False)),
                "evidence_backed": evidence_backed,
            },
            "promotion_decision": {
                "decision": "keep_temp" if evidence_backed else "discard",
                "reason": "successful evidence-backed run" if evidence_backed else "failed or weak run",
                "temporary_only": True,
                "rag_ingest": False,
                "vectorize": False,
                "stable_memory_write": False,
                "manuals_write": False,
            },
        }

    def _build_session_summary(self, state: AgentState) -> dict[str, Any]:
        last_result = None
        if state.last_result is not None:
            last_result = {
                "tool_name": state.last_result.tool_name,
                "ok": state.last_result.ok,
                "exit_code": state.last_result.exit_code,
                "error_type": state.last_result.error_type,
                "artifacts": list(state.last_result.artifacts or []),
                "stdout_preview": (state.last_result.stdout or "")[:400],
                "stderr_preview": (state.last_result.stderr or "")[:400],
            }

        summary_text = (state.final_answer or "").strip() or f"Run stopped with reason: {state.stop_reason or 'unknown'}"
        return {
            "user_goal": state.user_goal,
            "stop_reason": state.stop_reason,
            "current_role": state.current_role,
            "role_history": list(state.role_history),
            "last_action": dict(state.last_action or {}),
            "last_result": last_result,
            "read_files": list(state.read_files),
            "written_files": list(state.written_files),
            "summary_text": summary_text,
        }
