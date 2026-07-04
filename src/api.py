from __future__ import annotations

import logging

from fastapi import FastAPI

from src.confirmation import confirm_action, reject_action
from src.executor import ShellExecutor
from src.mcp_client import MCPClient, NeedsConfirmationError
from src.models import Evidence, PatchEvidence, TaskRequest, TaskResponse
from src.router import CapabilityRouter
from src.skills import SkillsRegistry

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Ralf Capability Router")
skills_registry = SkillsRegistry()
router = CapabilityRouter(skills_registry)
executor = ShellExecutor()
mcp = MCPClient()


@app.post("/tasks/run", response_model=TaskResponse)
def run_task(request: TaskRequest) -> TaskResponse:
    route = router.route(request.user_goal)
    logger.info("task_route mode=%s goal=%r", route.mode, request.user_goal)

    if request.mode == "route_only":
        return TaskResponse(route=route, evidence=None, message="route_only")

    skill_messages = [skills_registry.run(skill, request.user_goal) for skill in route.skills_used]

    if route.mode == "check_only":
        evidence = executor.run("ls -la")
        return TaskResponse(
            route=route,
            evidence=evidence,
            message=_join_messages("check_only evidence collected", skill_messages),
        )

    if route.mode == "patch_allowed":
        base_evidence = executor.run("pwd")
        patch_evidence = PatchEvidence(
            command=base_evidence.command,
            path=base_evidence.path,
            exit_code=base_evidence.exit_code,
            stdout=base_evidence.stdout,
            stderr=base_evidence.stderr,
            diff="mock diff: no repository files changed",
            tests=["mock test: py_compile", "mock test: pytest targeted"],
        )
        return TaskResponse(
            route=route,
            evidence=patch_evidence,
            message=_join_messages("patch plan requires repro, minimal diff, targeted tests", skill_messages),
        )

    evidence = Evidence(command="mcp:external_action", path="external", exit_code=0)
    try:
        message = _execute_external_action(request.user_goal)
        return TaskResponse(
            route=route,
            evidence=evidence,
            message=_join_messages(message, skill_messages),
        )
    except NeedsConfirmationError as exc:
        return TaskResponse(
            route=route,
            evidence=evidence,
            message=_join_messages(f"pending confirmation for {exc.action_type}", skill_messages),
            pending_confirmation_id=exc.confirmation_id,
        )


@app.post("/confirmations/{confirmation_id}/approve")
def approve_confirmation(confirmation_id: str) -> dict:
    ok = confirm_action(confirmation_id)
    if not ok:
        return {"ok": False, "confirmation_id": confirmation_id, "executed": False}
    return {
        "ok": True,
        "confirmation_id": confirmation_id,
        "executed": True,
        "message": mcp.execute_confirmed(confirmation_id),
    }


@app.post("/confirmations/{confirmation_id}/reject")
def reject_confirmation(confirmation_id: str) -> dict:
    return {"ok": reject_action(confirmation_id), "confirmation_id": confirmation_id}


def _execute_external_action(user_goal: str) -> str:
    goal = user_goal.lower()
    if "browser" in goal:
        return mcp.browser_inspect("about:blank")
    if "telegram" in goal:
        return mcp.send_telegram(user_goal)
    if "drive" in goal or "docs" in goal:
        return mcp.drive_upload(user_goal)
    return mcp.send_email("pending@example.invalid", "Ralf draft", user_goal)


def _join_messages(primary: str, skill_messages: list[str]) -> str:
    if not skill_messages:
        return primary
    return primary + " | " + " | ".join(skill_messages)
