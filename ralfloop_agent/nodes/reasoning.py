from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from ralfloop_agent.integration.abc_relation_read import ABCRelationReadAdapter
from ralfloop_agent.integration.capability_adapter import route_task
from ralfloop_agent.models.result_envelope import ResultEnvelope
from ralfloop_agent.integration.verification_judge import run_verification_judge, verification_blocks
from src import audit
from src.confirmation import get_confirmation
from src.executor import ShellExecutor
from src.mcp_client import MCPClient, NeedsConfirmationError
from src.models import Evidence, PatchEvidence
from src.text_mas_proxy import build_text_mas_trace


def _abc_read_evidence(user_goal: str) -> tuple[Evidence, dict]:
    resolution = ABCRelationReadAdapter().resolve(user_goal)
    evidence = Evidence(
        command="mcp:abc_relation:read_only",
        path="abc_relation",
        exit_code=0 if resolution.available else 1,
        stdout=resolution.render(),
        stderr=None if resolution.available else resolution.error,
    )
    meta = {
        "read_mcp": "abc_relation",
        "read_mcp_available": resolution.available,
        "fallback_skills": list(resolution.fallback_skills),
    }
    return evidence, meta


def run_capability_reasoning_cycle(user_goal: str, context: dict | None = None) -> ResultEnvelope:
    context = dict(context or {})
    task_id = str(context.get("task_id") or uuid4())
    route = route_task(user_goal, context.get("mode"))
    collaboration_trace = build_text_mas_trace(user_goal, route)
    executor = ShellExecutor(task_id=task_id)
    mcp = MCPClient()
    meta = {"timestamp": datetime.now().isoformat(), "source": "capability_reasoning_cycle", "task_id": task_id}
    if collaboration_trace:
        meta["collaboration_trace"] = collaboration_trace
    audit.log_operation("sandbox_initialized", {"task_id": task_id, "sandbox_path": str(executor.base_dir)})

    if route.mode == "check_only":
        if "abc_relation" in route.mcp_used:
            evidence, read_meta = _abc_read_evidence(user_goal)
            meta.update(read_meta)
            answer = "read_mcp evidence collected" if evidence.exit_code == 0 else "read_mcp unavailable"
        else:
            evidence = executor.run_in_sandbox(["ls", "-la"], cwd=".")
            answer = "check_only evidence collected"
        envelope = ResultEnvelope(
            route=route,
            evidence=evidence,
            jury_policy=route.jury_policy,
            collaboration_backend=route.collaboration_backend,
            verification_policy=route.verification_policy,
            collaboration_trace=collaboration_trace,
            jury_trace=collaboration_trace,
            answer=answer,
            meta=meta,
        )
        audit.log_operation("reasoning_cycle_check_only", envelope.model_dump(mode="json"))
        return envelope

    if route.mode == "patch_allowed":
        evidence = executor.run_in_sandbox(["git", "diff", "--no-ext-diff"], cwd=".")
        patch = PatchEvidence(
            command=evidence.command,
            path=evidence.path,
            exit_code=evidence.exit_code,
            stdout=evidence.stdout,
            stderr=evidence.stderr,
            diff=evidence.stdout or "",
            tests=["pending: run targeted tests before patch"],
        )
        candidate_answer = "patch_allowed requires repro, diff and tests"
        judge_trace = run_verification_judge(
            user_goal, route, patch, candidate_answer, {**context, "task_id": task_id}
        )
        patch_meta = {**meta}
        if judge_trace is not None:
            patch_meta["verification_judge"] = judge_trace
        answer = "verification_judge_blocked" if verification_blocks(route, judge_trace) else candidate_answer
        envelope = ResultEnvelope(
            route=route,
            evidence=patch,
            jury_policy=route.jury_policy,
            collaboration_backend=route.collaboration_backend,
            verification_policy=route.verification_policy,
            collaboration_trace=collaboration_trace,
            jury_trace=collaboration_trace,
            answer=answer,
            meta=patch_meta,
        )
        audit.log_operation("reasoning_cycle_patch_allowed", envelope.model_dump(mode="json"))
        return envelope

    try:
        if "telegram" in route.mcp_used:
            mcp.send_telegram(user_goal)
        elif "google_workspace.drive" in route.mcp_used:
            mcp.drive_upload(user_goal, filename="ralf_upload.txt")
        else:
            mcp.send_email("pending@example.invalid", "Ralf external action", user_goal)
    except NeedsConfirmationError as exc:
        confirmation = get_confirmation(exc.confirmation_id)
        envelope = ResultEnvelope(
            route=route,
            confirmation=confirmation,
            jury_policy=route.jury_policy,
            collaboration_backend=route.collaboration_backend,
            verification_policy=route.verification_policy,
            collaboration_trace=collaboration_trace,
            human_confirmation={"pending_confirmation_id": exc.confirmation_id, "required": True},
            jury_trace=collaboration_trace,
            answer="human_confirmation_required",
            meta={
                **meta,
                "confirmation_id": exc.confirmation_id,
            },
        )
        audit.log_operation("reasoning_cycle_confirmation_required", envelope.model_dump(mode="json"))
        return envelope
    except Exception as exc:
        envelope = ResultEnvelope(
            route=route,
            jury_policy=route.jury_policy,
            collaboration_backend=route.collaboration_backend,
            verification_policy=route.verification_policy,
            collaboration_trace=collaboration_trace,
            jury_trace=collaboration_trace,
            answer="external_action_failed",
            meta={**meta, "error": str(exc)},
        )
        audit.log_operation("reasoning_cycle_external_failed", envelope.model_dump(mode="json"))
        return envelope

    envelope = ResultEnvelope(
        route=route,
        jury_policy=route.jury_policy,
        collaboration_backend=route.collaboration_backend,
        verification_policy=route.verification_policy,
        collaboration_trace=collaboration_trace,
        jury_trace=collaboration_trace,
        answer="external action completed",
        meta=meta,
    )
    audit.log_operation("reasoning_cycle_external_completed", envelope.model_dump(mode="json"))
    return envelope


def reasoning_cycle_node(user_goal: str, context: dict | None = None) -> ResultEnvelope:
    return run_capability_reasoning_cycle(user_goal, context)
