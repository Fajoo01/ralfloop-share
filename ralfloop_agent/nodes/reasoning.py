from __future__ import annotations

from datetime import datetime

from ralfloop_agent.integration.capability_adapter import route_task
from ralfloop_agent.models.result_envelope import ResultEnvelope
from src import audit
from src.confirmation import get_confirmation
from src.executor import ShellExecutor
from src.mcp_client import MCPClient, NeedsConfirmationError
from src.models import PatchEvidence


def run_capability_reasoning_cycle(user_goal: str, context: dict | None = None) -> ResultEnvelope:
    context = dict(context or {})
    route = route_task(user_goal, context.get("mode"))
    executor = ShellExecutor()
    mcp = MCPClient()

    if route.mode == "check_only":
        evidence = executor.run_in_sandbox(["ls", "-la"], cwd=".")
        envelope = ResultEnvelope(
            route=route,
            evidence=evidence,
            answer="check_only evidence collected",
            meta={"timestamp": datetime.now().isoformat(), "source": "capability_reasoning_cycle"},
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
            answer="patch_allowed requires repro, diff and tests",
            meta={"timestamp": datetime.now().isoformat(), "source": "capability_reasoning_cycle"},
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
            answer="human_confirmation_required",
            meta={
                "timestamp": datetime.now().isoformat(),
                "source": "capability_reasoning_cycle",
                "confirmation_id": exc.confirmation_id,
            },
        )
        audit.log_operation("reasoning_cycle_confirmation_required", envelope.model_dump(mode="json"))
        return envelope

    envelope = ResultEnvelope(
        route=route,
        answer="external action completed",
        meta={"timestamp": datetime.now().isoformat(), "source": "capability_reasoning_cycle"},
    )
    audit.log_operation("reasoning_cycle_external_completed", envelope.model_dump(mode="json"))
    return envelope


def reasoning_cycle_node(user_goal: str, context: dict | None = None) -> ResultEnvelope:
    return run_capability_reasoning_cycle(user_goal, context)
