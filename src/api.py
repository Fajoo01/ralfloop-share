from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ralfloop_agent.domains.builder import DomainBuilder
from ralfloop_agent.domains.cli import answer_goal
from ralfloop_agent.domains.promotion import DomainPromotionService
from ralfloop_agent.domains.registry import DomainRegistry
from ralfloop_agent.domains.resolver import DomainResolver
from ralfloop_agent.domains.validator import DomainValidator
from ralfloop_agent.integration.recursive_mas_runtime import RecursiveMASRuntimeController
from src.confirmation import confirm_action, reject_action
from src.executor import ShellExecutor
from src.mcp_client import MCPClient, NeedsConfirmationError
from src.models import Evidence, PatchEvidence, TaskRequest, TaskResponse
from src.router import CapabilityRouter
from src.text_mas_proxy import build_text_mas_trace, summarize_text_mas_trace
from src.skills import SkillsRegistry

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Ralf Capability Router")
skills_registry = SkillsRegistry()
router = CapabilityRouter(skills_registry)
executor = ShellExecutor()
mcp = MCPClient()


class LabRecursiveMASRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str
    rounds: int | None = Field(default=None, ge=1, le=3)
    profile: str = "deterministic_diagnostic"


@app.get("/lab/recursive-mas/status")
def recursive_mas_lab_status() -> dict:
    return RecursiveMASRuntimeController.from_env().status().to_dict()


@app.get("/lab/recursive-mas/health")
def recursive_mas_lab_health() -> dict:
    return RecursiveMASRuntimeController.from_env().health()


@app.post("/lab/recursive-mas/run")
def recursive_mas_lab_run(request: LabRecursiveMASRunRequest) -> dict:
    payload = request.model_dump(exclude_none=True)
    result = RecursiveMASRuntimeController.from_env().execute(payload)
    status = result.get("status")
    if result.get("ok"):
        return result
    if status == "busy":
        raise HTTPException(status_code=409, detail=result)
    if status == "timeout":
        raise HTTPException(status_code=504, detail=result)
    if status in {"disabled", "circuit_open", "backend_unavailable"}:
        raise HTTPException(status_code=503, detail=result)
    raise HTTPException(status_code=500, detail=result)




class DomainGoalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str
    domain: str | None = None


class DomainDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str
    sources: list[str] = Field(default_factory=list)


class DomainPromoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved_by: str
    approval_token: str
    approval_reason: str = ""


@app.get("/lab/domains")
def lab_domains() -> dict:
    return {"domains": DomainRegistry().list_domains()}


@app.get("/lab/domains/{domain_id}")
def lab_domain_status(domain_id: str) -> dict:
    domain = DomainRegistry().get_domain(domain_id)
    if not domain:
        raise HTTPException(status_code=422, detail={"status": "missing"})
    return domain


@app.post("/lab/domains/resolve")
def lab_domain_resolve(request: DomainGoalRequest) -> dict:
    result = DomainResolver().resolve(request.goal, {"domain_id": request.domain} if request.domain else None).to_dict()
    if result["status"] == "ambiguous":
        raise HTTPException(status_code=409, detail=result)
    return result


@app.post("/lab/domains/answer")
def lab_domain_answer(request: DomainGoalRequest) -> dict:
    result = answer_goal(request.goal, request.domain)
    if result.get("status") == "domain_creation_required":
        raise HTTPException(status_code=202, detail=result)
    if result.get("status") == "ambiguous":
        raise HTTPException(status_code=409, detail=result)
    if result.get("status") == "human_confirmation_required":
        raise HTTPException(status_code=423, detail=result)
    if result.get("status") in {"jury_required", "domain_reasoning_required"} and result.get("jury_status") == "disabled":
        raise HTTPException(status_code=503, detail=result)
    return result


@app.post("/lab/domains/drafts")
def lab_domain_draft(request: DomainDraftRequest) -> dict:
    result = DomainBuilder().create_draft(request.goal, request.sources, {}).to_dict()
    if result.get("status") == "invalid":
        raise HTTPException(status_code=422, detail=result)
    return result


@app.post("/lab/domains/{domain_id}/{version}/validate")
def lab_domain_validate(domain_id: str, version: str) -> dict:
    domain = DomainRegistry().get_domain(domain_id, version)
    if not domain:
        raise HTTPException(status_code=422, detail={"valid": False, "blocking_issues": ["domain_missing"]})
    return DomainValidator().validate(domain).to_dict()


@app.post("/lab/domains/{domain_id}/{version}/promote")
def lab_domain_promote(domain_id: str, version: str, request: DomainPromoteRequest) -> dict:
    result = DomainPromotionService().promote(domain_id, version, approved_by=request.approved_by, approval_token=request.approval_token, approval_reason=request.approval_reason).to_dict()
    if result.get("status") == "approval_required":
        raise HTTPException(status_code=423, detail=result)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result)
    return result


@app.post("/tasks/run", response_model=TaskResponse)
def run_task(request: TaskRequest) -> TaskResponse:
    route = router.route(request.user_goal)
    collaboration_trace = build_text_mas_trace(request.user_goal, route)
    logger.info("task_route mode=%s goal=%r", route.mode, request.user_goal)

    if request.mode == "route_only":
        return _response(route=route, evidence=None, message="route_only", collaboration_trace=collaboration_trace)

    skill_messages = [skills_registry.run(skill, request.user_goal) for skill in route.skills_used]
    collaboration_message = summarize_text_mas_trace(collaboration_trace)
    if collaboration_message:
        skill_messages.append(collaboration_message)

    if route.mode == "check_only":
        evidence = executor.run("ls -la")
        return _response(
            route=route,
            evidence=evidence,
            message=_join_messages("check_only evidence collected", skill_messages),
            collaboration_trace=collaboration_trace,
        )

    if route.mode == "patch_allowed":
        base_evidence = executor.run("pwd")
        patch_evidence = PatchEvidence(
            command="ralf repair run",
            path=base_evidence.path,
            exit_code=base_evidence.exit_code,
            stdout=(
                "bounded_repair_required: run in isolated worktree; "
                "then request persistent approval and apply the exact hash-bound patch"
            ),
            stderr=base_evidence.stderr,
            diff="",
            tests=[],
        )
        return _response(
            route=route,
            evidence=patch_evidence,
            message=_join_messages(
                "patch_allowed uses bounded repair: "
                "ralf repair run -> ralf repair request-approval -> "
                "human approval -> ralf repair apply; "
                "no repository change has been fabricated or applied by this route",
                skill_messages,
            ),
            collaboration_trace=collaboration_trace,
        )

    evidence = Evidence(command="mcp:external_action", path="external", exit_code=0)
    try:
        message = _execute_external_action(request.user_goal)
        return _response(
            route=route,
            evidence=evidence,
            message=_join_messages(message, skill_messages),
            collaboration_trace=collaboration_trace,
        )
    except NeedsConfirmationError as exc:
        return _response(
            route=route,
            evidence=evidence,
            message=_join_messages(f"pending confirmation for {exc.action_type}", skill_messages),
            pending_confirmation_id=exc.confirmation_id,
            collaboration_trace=collaboration_trace,
            human_confirmation={"pending_confirmation_id": exc.confirmation_id, "required": True},
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


def _response(
    *,
    route,
    evidence,
    message: str,
    collaboration_trace: dict | None,
    pending_confirmation_id: str | None = None,
    human_confirmation: dict | None = None,
) -> TaskResponse:
    return TaskResponse(
        route=route,
        evidence=evidence,
        message=message,
        pending_confirmation_id=pending_confirmation_id,
        jury_policy=route.jury_policy,
        collaboration_backend=route.collaboration_backend,
        verification_policy=route.verification_policy,
        collaboration_trace=collaboration_trace,
        human_confirmation=human_confirmation,
        jury_trace=collaboration_trace,
    )
