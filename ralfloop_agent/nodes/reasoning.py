from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from ralfloop_agent.integration.capability_adapter import route_task
from ralfloop_agent.models.result_envelope import ResultEnvelope
from src import audit
from src.confirmation import get_confirmation
from src.executor import ShellExecutor
from src.mcp_client import MCPClient, NeedsConfirmationError
from src.models import PatchEvidence
from src.text_mas_proxy import build_text_mas_trace


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
        evidence = executor.run_in_sandbox(["ls", "-la"], cwd=".")
        envelope = ResultEnvelope(
            route=route,
            evidence=evidence,
            jury_policy=route.jury_policy,
            collaboration_backend=route.collaboration_backend,
            verification_policy=route.verification_policy,
            collaboration_trace=collaboration_trace,
            jury_trace=collaboration_trace,
            answer="check_only evidence collected",
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
        envelope = ResultEnvelope(
            route=route,
            evidence=patch,
            jury_policy=route.jury_policy,
            collaboration_backend=route.collaboration_backend,
            verification_policy=route.verification_policy,
            collaboration_trace=collaboration_trace,
            jury_trace=collaboration_trace,
            answer="patch_allowed requires repro, diff and tests",
            meta=meta,
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
