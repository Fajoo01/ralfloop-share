from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import time
from typing import Any, Callable, Literal

from pydantic import Field, model_validator

from .context import canonical_json
from .models import StrictModel
from .normalizer import normalize_grant_review
from .queue import GlmQueue, now_rome


PROMPT_VERSION = "director-v1"
SCHEMA_VERSION = "director-plan-v1"
WRITE_TOOL_PREFIXES = ("write", "send", "submit", "publish", "delete", "upload", "modify")


class VerifiedFact(StrictModel):
    fact: str = Field(min_length=1, max_length=500)
    evidence_refs: list[str] = Field(min_length=1, max_length=20)


class Assignment(StrictModel):
    task_id: str = Field(min_length=1, max_length=120)
    worker: str = Field(min_length=1, max_length=80)
    objective: str = Field(min_length=1, max_length=500)
    input_refs: list[str] = Field(default_factory=list, max_length=30)
    allowed_tools: list[str] = Field(default_factory=list, max_length=20)
    deliverable: str = Field(min_length=1, max_length=300)
    acceptance_tests: list[str] = Field(default_factory=list, max_length=20)
    depends_on: list[str] = Field(default_factory=list, max_length=20)
    priority: int = Field(default=0, ge=-100, le=100)


class DirectorDecision(StrictModel):
    action: Literal["assign", "revise_plan", "request_human_input", "conclude", "stop_budget_exhausted"]
    round: int = Field(ge=1, le=100)
    strategy: str = Field(min_length=1, max_length=700)
    assignments: list[Assignment] = Field(default_factory=list, max_length=20)
    review_trigger: str = Field(default="round_complete", max_length=300)
    stop_conditions: list[str] = Field(default_factory=list, max_length=20)
    strategic_summary: str = Field(default="", max_length=700)
    human_question: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def action_contract(self) -> "DirectorDecision":
        if self.action in {"assign", "revise_plan"} and not self.assignments:
            raise ValueError("director_assignments_required")
        if self.action not in {"assign", "revise_plan"} and self.assignments:
            raise ValueError("director_assignments_forbidden")
        if self.action == "request_human_input" and not self.human_question:
            raise ValueError("director_human_question_required")
        return self


class Blackboard(StrictModel):
    goal: str
    constraints: list[str] = Field(default_factory=list)
    verified_facts: list[VerifiedFact] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    completed_assignments: list[dict[str, Any]] = Field(default_factory=list)
    failed_assignments: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    resource_usage: dict[str, Any] = Field(default_factory=dict)
    round_number: int = 0


class Capability(StrictModel):
    worker: str
    purpose: str
    tools: list[str] = Field(default_factory=list)
    implementation: str
    write_capable: bool = False


class CapabilityCatalog(StrictModel):
    version: str
    workers: list[Capability]

    def by_worker(self) -> dict[str, Capability]:
        return {item.worker: item for item in self.workers}


class PreflightResult(StrictModel):
    ok: bool
    schema_valid: bool = True
    proposal_complete: bool = False
    eligibility_valid: bool = False
    verified_facts: list[VerifiedFact] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    known_gaps: list[str] = Field(default_factory=list)
    known_gap_fields: list[str] = Field(default_factory=list)
    mapping_errors: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    duration_ms: int = 0


class UtilityDecision(StrictModel):
    call_glm: bool
    reason: str


def capability_catalog(project_root: str | Path | None = None) -> CapabilityCatalog:
    root = Path(project_root or Path(__file__).resolve().parents[2])
    declared = [
        ("rules_engine", "date, budget, requirement and schema checks", ["grant_preflight", "schema_validator"], "ralfloop_agent.glm_review.director"),
        ("retrieval_worker", "local document and knowledge retrieval", ["bandi_semantic_retrieval"], "ralfloop_agent.domains.bandi_semantic_retrieval"),
        ("web_tool_agent", "verifiable read-only web research", ["deep_web_research"], "ralfloop_agent.model_tools.web_research"),
        ("document_worker", "document extraction to structured text", ["document_to_markdown"], "ralfloop_agent.model_tools.worker"),
        ("reviewer", "lightweight coherence review", [], "ralfloop_agent.domains.reasoning_router"),
        ("writer", "bounded rewrite from verified inputs", [], "ralfloop_agent.core.loop"),
        ("coder", "code task in sandbox; write needs approval", ["remote_code"], "ralfloop_agent.model_tools.remote_code"),
        ("judge", "non-deterministic acceptance review", ["schema_validator"], "ralfloop_agent.glm_review.validators"),
    ]
    workers = []
    for worker, purpose, tools, module in declared:
        module_path = root / (module.replace(".", "/") + ".py")
        if module_path.is_file() or importlib.util.find_spec(module) is not None:
            workers.append(Capability(worker=worker, purpose=purpose, tools=tools, implementation=module, write_capable=worker == "coder"))
    payload = [{"worker": item.worker, "tools": item.tools, "implementation": item.implementation} for item in workers]
    version = hashlib.sha256(canonical_json(payload).encode()).hexdigest()[:16]
    return CapabilityCatalog(version=version, workers=workers)


def preflight_grant_review(payload: dict[str, Any]) -> PreflightResult:
    started = time.monotonic()
    payload, normalization = normalize_grant_review(payload)
    proposal = payload.get("proposal") if isinstance(payload.get("proposal"), dict) else {}
    facts: list[VerifiedFact] = []
    missing_all: list[str] = []
    blockers: list[str] = []
    contradictions: list[str] = []
    mapping_errors = ["input_mapping_error"] if normalization.get("mapping_error") else []
    provenance = payload.get("field_provenance") if isinstance(payload.get("field_provenance"), dict) else {}

    def value(*names: str) -> Any:
        for name in names:
            if proposal.get(name) not in (None, "", []):
                return proposal[name]
            if payload.get(name) not in (None, "", []):
                return payload[name]
        return None

    def refs(name: str, *aliases: str) -> list[str]:
        for key in (name, *aliases):
            origin = provenance.get(key)
            if not isinstance(origin, dict):
                continue
            evidence_ref = str(origin.get("evidence_ref") or "")
            if evidence_ref:
                return [evidence_ref]
            source_path = str(origin.get("source_path") or "")
            if source_path:
                return [source_path]
        return [f"input:{name}"]

    checks = {
        "project_description": value("project_description", "description", "draft"),
        "duration_minutes": value("duration_minutes", "duration", "duration_days", "duration_months"),
        "participants": value("participants", "participant_count", "beneficiaries_count"),
        "age_range": value("age_range", "ages", "target_age"),
        "same_group": value("same_group", "same_participant_group"),
        "free_event": value("free_event", "free_participation", "free", "gratuita"),
        "deadline": value("deadline"),
        "event_window": value("event_window", "event_date", "start_date"),
        "organization_status": value("organization_status", "applicant_status", "legal_status"),
    }
    check_aliases = {
        "project_description": ("draft", "description"),
        "duration_minutes": ("duration",),
        "participants": ("participant_count", "beneficiaries_count"),
        "same_group": ("same_participant_group",),
        "free_event": ("free_participation", "free"),
        "event_window": ("event_date", "start_date"),
        "organization_status": ("applicant_status", "legal_status"),
    }
    for name, raw in checks.items():
        if raw is None:
            missing_all.append(name)
        else:
            facts.append(VerifiedFact(fact=f"{name}={str(raw)[:300]}", evidence_refs=refs(name, *check_aliases.get(name, ()))))
    budget = value("budget_items", "budget")
    declared_total = value("budget_total", "total_budget")
    if isinstance(payload.get("budget_summary"), dict):
        budget = budget or payload["budget_summary"].get("items") or payload["budget_summary"].get("lines")
        declared_total = declared_total or payload["budget_summary"].get("total")
    if not isinstance(budget, list) or declared_total is None:
        missing_all.append("budget_sum")
    else:
        amounts = [item.get("amount") for item in budget if isinstance(item, dict) and isinstance(item.get("amount"), (int, float))]
        total = sum(amounts)
        facts.append(VerifiedFact(fact=f"budget_sum={total:g}", evidence_refs=refs("budget_items", "budget_total")))
        if amounts and abs(total - float(declared_total)) > 0.01:
            contradictions.append("budget_sum_mismatch")
    known_gaps = payload.get("known_gaps") if isinstance(payload.get("known_gaps"), list) else []
    for gap in known_gaps:
        facts.append(VerifiedFact(fact=f"declared_gap={str(gap)[:300]}", evidence_refs=["input:known_gaps"]))
    known_gap_fields = sorted(_known_gap_fields(known_gaps))
    missing = sorted(set(missing_all) - set(known_gap_fields))
    for required in ("deadline", "organization_status", "project_description"):
        if required in missing:
            blockers.append(f"missing_required:{required}")
    deadline = checks["deadline"]
    event_window = checks["event_window"]
    event_date = event_window.get("start") if isinstance(event_window, dict) else event_window
    try:
        if deadline and event_date and datetime.fromisoformat(str(event_date)[:10]) <= datetime.fromisoformat(str(deadline)[:10]):
            contradictions.append("event_not_after_deadline")
    except ValueError:
        contradictions.append("invalid_date")
    eligibility_fields = {"duration_minutes", "participants", "age_range", "same_group", "free_event", "event_window", "organization_status"}
    schema_valid = not mapping_errors
    proposal_complete = not missing_all and not known_gaps
    eligibility_valid = not (set(missing_all) & eligibility_fields) and not contradictions
    return PreflightResult(
        ok=schema_valid and not blockers and not contradictions,
        schema_valid=schema_valid,
        proposal_complete=proposal_complete,
        eligibility_valid=eligibility_valid,
        verified_facts=facts,
        missing_fields=missing,
        known_gaps=[str(gap)[:700] for gap in known_gaps],
        known_gap_fields=known_gap_fields,
        mapping_errors=mapping_errors,
        blockers=blockers,
        contradictions=contradictions,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def utility_gate(
    payload: dict[str, Any],
    preflight: PreflightResult,
    *,
    retrospective: bool = False,
    cached: bool = False,
) -> UtilityDecision:
    if preflight.mapping_errors or not preflight.schema_valid:
        return UtilityDecision(call_glm=False, reason="input_mapping_error")
    if cached:
        return UtilityDecision(call_glm=False, reason="valid_cache_hit")
    if payload.get("goal_satisfied") or payload.get("deterministic_result_available"):
        return UtilityDecision(call_glm=False, reason="deterministic_result_available")
    if payload.get("task_mode") in {"extract", "extractive"}:
        return UtilityDecision(call_glm=False, reason="purely_extractive")
    if payload.get("call_status") == "closed" and not retrospective:
        return UtilityDecision(call_glm=False, reason="closed_call_requires_retrospective")
    if preflight.blockers and not retrospective:
        return UtilityDecision(call_glm=False, reason="unresolved_blocking_requirements")
    if len(preflight.missing_fields) >= 6 and not retrospective:
        return UtilityDecision(call_glm=False, reason="documentation_too_incomplete")
    return UtilityDecision(call_glm=True, reason="strategic_review_useful")


def strategic_context_packet(
    blackboard: Blackboard,
    catalog: CapabilityCatalog,
    *,
    artifact_refs: list[str] | None = None,
    delta: dict[str, Any] | None = None,
    reason: str,
    max_chars: int = 2_000,
) -> dict[str, Any]:
    packet = {
        "goal": blackboard.goal[:400],
        "constraints": blackboard.constraints[:10],
        "verified_facts": [item.model_dump(mode="json") for item in blackboard.verified_facts[-15:]],
        "known_gaps": [item.fact.removeprefix("declared_gap=") for item in blackboard.verified_facts if item.fact.startswith("declared_gap=")][-10:],
        "open_strategic_questions": blackboard.open_questions[-10:],
        "artifact_refs": (artifact_refs or blackboard.artifacts)[-15:],
        "prior_round_delta": delta or {},
        "capabilities": [{"worker": item.worker, "tools": item.tools, "purpose": item.purpose} for item in catalog.workers],
        "budgets": blackboard.resource_usage,
        "round": blackboard.round_number + 1,
        "call_reason": reason,
        "instruction": "Direct strategy only. Assign bounded work; never execute tools. Do not rediscover known_gaps.",
    }
    while len(canonical_json(packet)) > max_chars and packet["verified_facts"]:
        packet["verified_facts"].pop(0)
    while len(canonical_json(packet)) > max_chars and packet["capabilities"]:
        packet["capabilities"].pop()
    while len(canonical_json(packet)) > max_chars and packet["artifact_refs"]:
        packet["artifact_refs"].pop(0)
    if len(canonical_json(packet)) > max_chars:
        packet["prior_round_delta"] = {"summary": "delta_truncated", "ref": "blackboard"}
    if len(canonical_json(packet)) > max_chars:
        raise ValueError("strategic_context_cannot_fit")
    return packet


def director_prompt(packet: dict[str, Any], *, max_assignments: int = 5) -> str:
    schema = {
        "action": "assign|revise_plan|request_human_input|conclude|stop_budget_exhausted",
        "round": 1,
        "strategy": "string",
        "assignments": [{
            "task_id": "string", "worker": "string", "objective": "string",
            "input_refs": [], "allowed_tools": [], "deliverable": "string",
            "acceptance_tests": [], "depends_on": [], "priority": 0,
        }],
        "review_trigger": "string", "stop_conditions": [],
        "strategic_summary": "string", "human_question": "string",
    }
    return "\n".join((
        "ROLE: GLM Director. Plan only; never call tools or perform external actions.",
        f"Return one closed JSON object matching schema. assignments<={max_assignments}. No Markdown.",
        "Each assignment: bounded objective, explicit available worker, referenced inputs, allowlisted tools, deliverable, acceptance tests, dependencies, priority.",
        "Use known_gaps as facts; do not assign rediscovery. Conclude with short strategic_summary; Ralf composes final output.",
        "SCHEMA=" + canonical_json(schema),
        "STATE=" + canonical_json(packet),
    ))


def parse_director_decision(raw: bytes | str) -> DirectorDecision:
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    except UnicodeDecodeError as exc:
        raise ValueError("director_output_not_utf8") from exc
    start = text.find("{")
    if start < 0:
        raise ValueError("director_json_missing")
    decoder = json.JSONDecoder()
    try:
        value, _ = decoder.raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise ValueError("director_json_truncated" if exc.pos >= len(text[start:]) - 2 else "director_json_invalid") from exc
    return DirectorDecision.model_validate(value)


def validate_plan(
    decision: DirectorDecision,
    catalog: CapabilityCatalog,
    *,
    max_assignments: int = 5,
    approval: bool = False,
) -> list[Assignment]:
    if len(decision.assignments) > max_assignments:
        raise ValueError("director_assignment_budget_exceeded")
    ids = [item.task_id for item in decision.assignments]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate_assignment_id")
    workers = catalog.by_worker()
    for item in decision.assignments:
        capability = workers.get(item.worker)
        if not capability:
            raise ValueError(f"worker_not_available:{item.worker}")
        denied = sorted(set(item.allowed_tools) - set(capability.tools))
        if denied:
            raise ValueError(f"tool_not_allowlisted:{','.join(denied)}")
        if not approval and (capability.write_capable or any(tool.startswith(WRITE_TOOL_PREFIXES) for tool in item.allowed_tools)):
            raise ValueError("write_tool_requires_approval")
        unknown_dependencies = sorted(set(item.depends_on) - set(ids))
        if unknown_dependencies:
            raise ValueError(f"unknown_dependency:{','.join(unknown_dependencies)}")
    return _topological(decision.assignments)


def assignment_hash(task_type: str, item: Assignment) -> str:
    value = {"task_type": task_type, "worker": item.worker, "input_refs": item.input_refs, "objective": item.objective}
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class DirectorStore:
    def __init__(self, queue: GlmQueue) -> None:
        self.queue = queue
        with queue.connect() as conn:
            conn.executescript(
                """
                create table if not exists glm_blackboards (
                    task_id text primary key,
                    state_json text not null,
                    updated_at text not null
                );
                create table if not exists glm_director_cache (
                    cache_key text primary key,
                    result_json text not null,
                    valid integer not null default 1,
                    created_at text not null
                );
                create table if not exists glm_assignments (
                    task_id text not null,
                    assignment_hash text not null,
                    assignment_json text not null,
                    status text not null,
                    result_json text,
                    updated_at text not null,
                    primary key(task_id, assignment_hash)
                );
                create table if not exists glm_round_metrics (
                    metric_id integer primary key autoincrement,
                    task_id text not null,
                    round_number integer not null,
                    metrics_json text not null,
                    created_at text not null
                );
                """
            )
            conn.execute("insert or ignore into schema_migrations(version,applied_at) values(4,?)", (now_rome(),))

    def load_blackboard(self, task_id: str, *, goal: str, constraints: list[str]) -> Blackboard:
        with self.queue.connect() as conn:
            row = conn.execute("select state_json from glm_blackboards where task_id=?", (task_id,)).fetchone()
        return Blackboard.model_validate_json(row["state_json"]) if row else Blackboard(goal=goal, constraints=constraints)

    def save_blackboard(self, task_id: str, value: Blackboard) -> None:
        with self.queue.connect() as conn:
            conn.execute(
                """insert into glm_blackboards(task_id,state_json,updated_at) values(?,?,?)
                   on conflict(task_id) do update set state_json=excluded.state_json,updated_at=excluded.updated_at""",
                (task_id, value.model_dump_json(), now_rome()),
            )

    def cache_get(self, key: str) -> DirectorDecision | None:
        with self.queue.connect() as conn:
            row = conn.execute("select result_json from glm_director_cache where cache_key=? and valid=1", (key,)).fetchone()
        return DirectorDecision.model_validate_json(row["result_json"]) if row else None

    def cache_put(self, key: str, decision: DirectorDecision) -> None:
        with self.queue.connect() as conn:
            conn.execute(
                "insert or replace into glm_director_cache(cache_key,result_json,valid,created_at) values(?,?,1,?)",
                (key, decision.model_dump_json(), now_rome()),
            )

    def assignment_get(self, task_id: str, digest: str) -> dict[str, Any] | None:
        with self.queue.connect() as conn:
            row = conn.execute(
                "select status,result_json from glm_assignments where task_id=? and assignment_hash=?",
                (task_id, digest),
            ).fetchone()
        if not row:
            return None
        return {"status": row["status"], "result": json.loads(row["result_json"]) if row["result_json"] else None}

    def assignment_save(self, task_id: str, digest: str, item: Assignment, status: str, result: dict[str, Any] | None) -> None:
        with self.queue.connect() as conn:
            conn.execute(
                """insert into glm_assignments values(?,?,?,?,?,?)
                   on conflict(task_id,assignment_hash) do update set
                   status=excluded.status,result_json=excluded.result_json,updated_at=excluded.updated_at""",
                (task_id, digest, item.model_dump_json(), status, canonical_json(result) if result is not None else None, now_rome()),
            )

    def metrics(self, task_id: str, round_number: int, value: dict[str, Any]) -> None:
        with self.queue.connect() as conn:
            conn.execute(
                "insert into glm_round_metrics(task_id,round_number,metrics_json,created_at) values(?,?,?,?)",
                (task_id, round_number, canonical_json(value), now_rome()),
            )


class DirectorOrchestrator:
    def __init__(
        self,
        queue: GlmQueue,
        director: Callable[[dict[str, Any]], DirectorDecision | dict[str, Any]],
        workers: dict[str, Callable[[Assignment, dict[str, Any]], dict[str, Any]]],
        *,
        catalog: CapabilityCatalog | None = None,
        model_configuration: str = "glm-5.2-colibri",
    ) -> None:
        self.queue = queue
        self.store = DirectorStore(queue)
        self.director = director
        self.workers = workers
        self.catalog = catalog or capability_catalog()
        self.model_configuration = model_configuration

    def run(
        self,
        *,
        task_id: str,
        task_type: str,
        payload: dict[str, Any],
        max_rounds: int = 3,
        max_assignments: int = 5,
        time_budget_seconds: int = 7_200,
        worker_call_budget: int = 15,
        retrospective: bool = False,
        approval: bool = False,
    ) -> dict[str, Any]:
        started = time.monotonic()
        max_rounds = max(1, min(int(max_rounds), 10))
        max_assignments = max(1, min(int(max_assignments), 20))
        preflight = preflight_grant_review(payload) if task_type == "grant_review" else PreflightResult(ok=True)
        blackboard = self.store.load_blackboard(task_id, goal=str(payload.get("goal") or task_type), constraints=list(payload.get("constraints") or []))
        known = {(item.fact, tuple(item.evidence_refs)) for item in blackboard.verified_facts}
        blackboard.verified_facts.extend(item for item in preflight.verified_facts if (item.fact, tuple(item.evidence_refs)) not in known)
        blackboard.contradictions = sorted(set([*blackboard.contradictions, *preflight.contradictions]))
        blackboard.open_questions = sorted(set([*blackboard.open_questions, *preflight.missing_fields]))
        blackboard.resource_usage.update({"time_budget_seconds": time_budget_seconds, "worker_call_budget": worker_call_budget})
        self.store.save_blackboard(task_id, blackboard)
        gate = utility_gate(payload, preflight, retrospective=retrospective)
        if not gate.call_glm:
            status = "invalid_input" if gate.reason == "input_mapping_error" else "skipped"
            return {"status": status, "reason": gate.reason, "preflight": preflight.model_dump(mode="json"), "blackboard": blackboard.model_dump(mode="json"), "metrics": {"preflight_ms": preflight.duration_ms, "director_calls": 0}}
        delta: dict[str, Any] = {}
        director_calls = 0
        worker_calls = 0
        cache_hits = 0
        input_tokens = 0
        output_tokens = 0
        prefill_ms = 0
        decode_ms = 0
        call_reason = "initial_strategy"
        for round_number in range(blackboard.round_number + 1, max_rounds + 1):
            if time.monotonic() - started >= time_budget_seconds:
                return self._finish("stop_budget_exhausted", blackboard, preflight, director_calls, worker_calls, cache_hits, started)
            packet = strategic_context_packet(blackboard, self.catalog, delta=delta, reason=call_reason)
            cache_key = self._cache_key(packet)
            decision = self.store.cache_get(cache_key)
            if decision:
                cache_hits += 1
            else:
                call_started = time.monotonic()
                input_tokens += max(1, len(canonical_json(packet)) // 4)
                raw = self.director(packet)
                decision = raw if isinstance(raw, DirectorDecision) else DirectorDecision.model_validate(raw)
                output_tokens += max(1, len(decision.model_dump_json()) // 4)
                director_calls += 1
                self.store.cache_put(cache_key, decision)
                call_ms = int((time.monotonic() - call_started) * 1000)
                decode_ms += call_ms
                blackboard.resource_usage["director_ms"] = int(blackboard.resource_usage.get("director_ms", 0)) + call_ms
            blackboard.resource_usage.update({
                "director_calls": director_calls,
                "cache_hits": cache_hits,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "prefill_glm_ms": prefill_ms,
                "decode_glm_ms": decode_ms,
                "last_glm_call_reason": call_reason,
            })
            if decision.round != round_number:
                raise ValueError("director_round_mismatch")
            blackboard.round_number = round_number
            blackboard.decisions.append(f"{decision.action}:{decision.strategy}")
            if decision.action in {"conclude", "request_human_input", "stop_budget_exhausted"}:
                if decision.strategic_summary:
                    blackboard.decisions.append("summary:" + decision.strategic_summary)
                if decision.action == "request_human_input":
                    blackboard.open_questions.append(decision.human_question)
                self.store.save_blackboard(task_id, blackboard)
                return self._finish(decision.action, blackboard, preflight, director_calls, worker_calls, cache_hits, started, decision=decision)
            ordered = validate_plan(decision, self.catalog, max_assignments=max_assignments, approval=approval)
            round_completed: list[dict[str, Any]] = []
            round_failed: list[dict[str, Any]] = []
            by_id: dict[str, dict[str, Any]] = {}
            worker_ms = 0
            tool_ms = 0
            retrieval_ms = 0
            minimodel_ms = 0
            for item in ordered:
                if worker_calls >= worker_call_budget:
                    return self._finish("stop_budget_exhausted", blackboard, preflight, director_calls, worker_calls, cache_hits, started)
                if any(not by_id.get(dep, {}).get("ok") for dep in item.depends_on):
                    result = {"ok": False, "status": "dependency_failed", "task_id": item.task_id}
                else:
                    digest = assignment_hash(task_type, item)
                    previous = self.store.assignment_get(task_id, digest)
                    if previous and previous["status"] == "completed":
                        result = dict(previous["result"] or {}) | {"deduplicated": True}
                    else:
                        worker = self.workers.get(item.worker)
                        if not worker:
                            result = {"ok": False, "status": "worker_unavailable", "worker": item.worker}
                        else:
                            work_started = time.monotonic()
                            self.store.assignment_save(task_id, digest, item, "running", None)
                            result = worker(item, {"blackboard": blackboard.model_dump(mode="json"), "inputs": item.input_refs})
                            worker_calls += 1
                            elapsed = int((time.monotonic() - work_started) * 1000)
                            worker_ms += elapsed
                            tool_ms += int((result.get("metrics") or {}).get("tool_ms", 0))
                            retrieval_ms += int((result.get("metrics") or {}).get("retrieval_ms", 0))
                            minimodel_ms += int((result.get("metrics") or {}).get("minimodel_ms", 0))
                            if result.get("external_action") and not approval:
                                result = {"ok": False, "status": "external_action_requires_approval"}
                            accepted = _accept(item.acceptance_tests, result)
                            result["acceptance_passed"] = accepted
                            result["ok"] = bool(result.get("ok")) and accepted
                            self.store.assignment_save(task_id, digest, item, "completed" if result["ok"] else "failed", result)
                by_id[item.task_id] = result
                record = {"task_id": item.task_id, "worker": item.worker, "result": result}
                (round_completed if result.get("ok") else round_failed).append(record)
                blackboard.artifacts.extend(str(path) for path in result.get("artifacts") or [])
                for fact in result.get("verified_facts") or []:
                    try:
                        verified = VerifiedFact.model_validate(fact)
                        identity = (verified.fact, tuple(verified.evidence_refs))
                        if identity not in {(row.fact, tuple(row.evidence_refs)) for row in blackboard.verified_facts}:
                            blackboard.verified_facts.append(verified)
                    except ValueError:
                        round_failed.append({"task_id": item.task_id, "error": "fact_missing_evidence_refs"})
                blackboard.contradictions.extend(str(value) for value in result.get("contradictions") or [])
            blackboard.completed_assignments.extend(round_completed)
            blackboard.failed_assignments.extend(round_failed)
            blackboard.next_actions = [item.task_id for item in ordered if item.task_id not in {row["task_id"] for row in round_completed}]
            blackboard.resource_usage.update({
                "director_calls": director_calls,
                "worker_calls": worker_calls,
                "tool_ms": int(blackboard.resource_usage.get("tool_ms", 0)) + tool_ms,
                "worker_ms": int(blackboard.resource_usage.get("worker_ms", 0)) + worker_ms,
                "cache_hits": cache_hits,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "prefill_glm_ms": prefill_ms,
                "decode_glm_ms": decode_ms,
                "retrieval_ms": int(blackboard.resource_usage.get("retrieval_ms", 0)) + retrieval_ms,
                "minimodel_ms": int(blackboard.resource_usage.get("minimodel_ms", 0)) + minimodel_ms,
                "last_glm_call_reason": call_reason,
            })
            delta = {"completed": round_completed, "failed": round_failed, "contradictions": blackboard.contradictions[-10:]}
            self.store.save_blackboard(task_id, blackboard)
            self.store.metrics(task_id, round_number, {"preflight_ms": preflight.duration_ms, "retrieval_ms": retrieval_ms, "minimodel_ms": minimodel_ms, "tool_ms": tool_ms, "prefill_glm_ms": prefill_ms, "decode_glm_ms": decode_ms, "director_calls": director_calls, "worker_calls": worker_calls, "input_tokens": input_tokens, "output_tokens": output_tokens, "assignments_completed": len(round_completed), "assignments_failed": len(round_failed), "cache_hit": bool(cache_hits), "call_reason": call_reason})
            if any((row["result"] or {}).get("goal_satisfied") for row in round_completed):
                return self._finish("conclude", blackboard, preflight, director_calls, worker_calls, cache_hits, started)
            call_reason = "critical_assignment_failed" if round_failed else "round_complete"
        return self._finish("stop_budget_exhausted", blackboard, preflight, director_calls, worker_calls, cache_hits, started)

    def _cache_key(self, packet: dict[str, Any]) -> str:
        value = {
            "packet_hash": hashlib.sha256(canonical_json(packet).encode()).hexdigest(),
            "director_goal": packet["goal"],
            "capability_catalog_version": self.catalog.version,
            "prompt_version": PROMPT_VERSION,
            "model_configuration": self.model_configuration,
            "schema_version": SCHEMA_VERSION,
        }
        return hashlib.sha256(canonical_json(value).encode()).hexdigest()

    @staticmethod
    def _finish(status: str, blackboard: Blackboard, preflight: PreflightResult, director_calls: int, worker_calls: int, cache_hits: int, started: float, *, decision: DirectorDecision | None = None) -> dict[str, Any]:
        return {
            "status": status,
            "decision": decision.model_dump(mode="json") if decision else None,
            "preflight": preflight.model_dump(mode="json"),
            "blackboard": blackboard.model_dump(mode="json"),
            "metrics": {
                "preflight_ms": preflight.duration_ms,
                "director_calls": director_calls,
                "worker_calls": worker_calls,
                "tool_calls": blackboard.resource_usage.get("tool_calls", 0),
                "retrieval_ms": blackboard.resource_usage.get("retrieval_ms", 0),
                "minimodel_ms": blackboard.resource_usage.get("minimodel_ms", 0),
                "tool_ms": blackboard.resource_usage.get("tool_ms", 0),
                "prefill_glm_ms": blackboard.resource_usage.get("prefill_glm_ms", 0),
                "decode_glm_ms": blackboard.resource_usage.get("decode_glm_ms", 0),
                "input_tokens": blackboard.resource_usage.get("input_tokens", 0),
                "output_tokens": blackboard.resource_usage.get("output_tokens", 0),
                "assignments_completed": len(blackboard.completed_assignments),
                "assignments_failed": len(blackboard.failed_assignments),
                "last_glm_call_reason": blackboard.resource_usage.get("last_glm_call_reason"),
                "cache_hits": cache_hits,
                "rounds": blackboard.round_number,
                "total_ms": int((time.monotonic() - started) * 1000),
            },
        }


def _topological(items: list[Assignment]) -> list[Assignment]:
    by_id = {item.task_id: item for item in items}
    indegree = {item.task_id: len(item.depends_on) for item in items}
    edges: dict[str, list[str]] = defaultdict(list)
    for item in items:
        for dependency in item.depends_on:
            edges[dependency].append(item.task_id)
    ready = deque(sorted((task_id for task_id, count in indegree.items() if count == 0), key=lambda key: (-by_id[key].priority, key)))
    output = []
    while ready:
        current = ready.popleft()
        output.append(by_id[current])
        for child in edges[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    if len(output) != len(items):
        raise ValueError("assignment_dependency_cycle")
    return output


def _accept(tests: list[str], result: dict[str, Any]) -> bool:
    for test in tests:
        if test == "ok" and not result.get("ok"):
            return False
        if test == "nonempty" and not result.get("deliverable"):
            return False
        if test.startswith("field:") and not result.get(test.split(":", 1)[1]):
            return False
    return True


def _known_gap_fields(values: list[Any]) -> set[str]:
    aliases = {
        "event_window": ("periodo", "finestra evento", "event window"),
        "event_date": ("data evento", "date evento", "date precise", "date e orari", "event date"),
        "deadline": ("scadenza", "deadline"),
        "organization_status": ("status", "stato organizz", "non profit", "aps", "ets"),
        "participants": ("partecipanti", "participants"),
        "age_range": ("età", "eta", "fascia", "age"),
        "same_group": ("stesso gruppo", "same group"),
        "free_event": ("gratuit", "free"),
        "duration_minutes": ("durata", "minuti", "duration"),
        "budget_sum": ("budget", "costi", "cost"),
        "project_description": ("bozza", "descrizione", "draft", "description"),
    }
    output: set[str] = set()
    for raw in values:
        text = str(raw).casefold()
        for field, markers in aliases.items():
            if any(marker in text for marker in markers):
                output.add("event_window" if field == "event_date" else field)
    return output


__all__ = [
    "Assignment", "Blackboard", "CapabilityCatalog", "DirectorDecision", "DirectorOrchestrator",
    "DirectorStore", "PreflightResult", "VerifiedFact", "assignment_hash", "capability_catalog",
    "director_prompt", "parse_director_decision", "preflight_grant_review", "strategic_context_packet",
    "utility_gate", "validate_plan",
]
