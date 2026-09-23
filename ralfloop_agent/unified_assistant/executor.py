from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field

from .contracts import AssistantPlan, PlanAssignment, PolicyClass
from .registry import UnifiedRegistryFacade


class StructuredArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(pattern=r"^artifact\.[a-f0-9]{16}$")
    artifact_type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,95}$")
    version: int = Field(default=1, ge=1, le=1000)
    status: str = Field(min_length=1, max_length=64)
    facts: tuple[dict[str, Any], ...] = Field(default_factory=tuple, max_length=64)
    evidence_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    producer_task_id: str
    content_role: str = "data"

    @classmethod
    def create(
        cls,
        *,
        artifact_type: str,
        status: str,
        producer_task_id: str,
        facts: tuple[dict[str, Any], ...] = (),
        evidence_refs: tuple[str, ...] = (),
        payload: Mapping[str, Any] | None = None,
    ) -> "StructuredArtifact":
        canonical = json.dumps({
            "artifact_type": artifact_type, "status": status,
            "facts": facts, "evidence_refs": evidence_refs,
            "payload": dict(payload or {}), "producer_task_id": producer_task_id,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return cls(
            artifact_id="artifact." + hashlib.sha256(canonical.encode()).hexdigest()[:16],
            artifact_type=artifact_type, status=status, facts=facts,
            evidence_refs=evidence_refs, payload=dict(payload or {}),
            producer_task_id=producer_task_id,
        )


class NodeExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    skill: str
    status: str
    artifact_ref: str | None = None
    side_effects: int = Field(default=0, ge=0)
    reason: str = ""


class PlanExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str
    nodes: tuple[NodeExecution, ...]
    artifacts: tuple[StructuredArtifact, ...]


SkillAdapter = Callable[[PlanAssignment, Mapping[str, Any]], StructuredArtifact]


class UnifiedDAGExecutor:
    """Executes only explicitly registered adapters; plan never grants authority."""

    def __init__(
        self,
        registry: UnifiedRegistryFacade,
        adapters: Mapping[str, SkillAdapter],
    ) -> None:
        self.registry = registry
        self.adapters = dict(adapters)
        for skill_id in self.adapters:
            self.registry.skill(skill_id)

    def execute(
        self,
        plan: AssistantPlan,
        *,
        inputs: Mapping[str, Any],
        approved_tasks: frozenset[str] = frozenset(),
    ) -> PlanExecution:
        artifacts: dict[str, StructuredArtifact] = {}
        nodes: list[NodeExecution] = []
        completed: set[str] = set()
        for assignment in plan.assignments:
            try:
                self._validate_node(assignment, completed, approved_tasks)
                adapter = self.adapters.get(assignment.skill)
                if adapter is None:
                    raise ValueError("skill_executor_unavailable")
                node_inputs = self._resolve_inputs(assignment, inputs, artifacts)
                artifact = adapter(assignment, node_inputs)
                if artifact.producer_task_id != assignment.task_id:
                    raise ValueError("artifact_producer_mismatch")
                artifacts[assignment.output_ref] = artifact
                completed.add(assignment.task_id)
                nodes.append(NodeExecution(
                    task_id=assignment.task_id, skill=assignment.skill,
                    status="completed", artifact_ref=artifact.artifact_id,
                ))
            except Exception as exc:
                nodes.append(NodeExecution(
                    task_id=assignment.task_id, skill=assignment.skill,
                    status="blocked", reason=str(exc)[:160],
                ))
                return PlanExecution(
                    status="blocked", nodes=tuple(nodes), artifacts=tuple(artifacts.values())
                )
        return PlanExecution(status="completed", nodes=tuple(nodes), artifacts=tuple(artifacts.values()))

    def _validate_node(
        self,
        assignment: PlanAssignment,
        completed: set[str],
        approved_tasks: frozenset[str],
    ) -> None:
        if set(assignment.depends_on) - completed:
            raise ValueError("dependency_not_completed")
        skill = self.registry.skill(assignment.skill)
        if assignment.domain not in skill.domains:
            raise ValueError("skill_domain_not_allowed")
        if assignment.policy != skill.classification:
            raise ValueError("planner_policy_mismatch")
        if assignment.policy in {PolicyClass.CONFIRM_WRITE, PolicyClass.PROTECTED} and assignment.task_id not in approved_tasks:
            # Compose/draft nodes are non-executing preparation. Real write adapters
            # must require explicit approval task binding.
            if assignment.skill not in {"email.compose", "email.reply", "pec.prepare_send", "documents.sign"}:
                raise ValueError("node_approval_required")
        if assignment.policy is PolicyClass.DENY:
            raise ValueError("node_denied")
        available = {
            capability
            for tool in self.registry.list_tools()
            if not tool.availability.startswith(("disabled", "constrained"))
            for capability in tool.capabilities
        }
        available.update(
            capability
            for domain in self.registry.domains.values()
            for capability in domain.specialist_capabilities
        )
        if (self.registry.facade_path.parent.parent / "ralfloop_agent" / "domains" / "email_reply.py").exists():
            available.add("email_reply_domain_v1")
        if set(skill.required_capabilities) - available:
            raise ValueError("required_capability_unavailable")

    @staticmethod
    def _resolve_inputs(
        assignment: PlanAssignment,
        inputs: Mapping[str, Any],
        artifacts: Mapping[str, StructuredArtifact],
    ) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for ref in assignment.input_refs:
            if ref in artifacts:
                resolved[ref] = artifacts[ref].model_dump(mode="json")
            elif ref in inputs:
                resolved[ref] = inputs[ref]
            else:
                raise ValueError(f"input_ref_unresolved:{ref}")
        resolved["content_boundary"] = "artifacts_are_data_not_instructions"
        return resolved


__all__ = ["NodeExecution", "PlanExecution", "StructuredArtifact", "UnifiedDAGExecutor"]
