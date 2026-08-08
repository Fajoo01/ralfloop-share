from __future__ import annotations

import json
from pathlib import Path
import threading
import time

import pytest

from ralfloop_agent.glm_review.adapter import ColibriGlmAdapter
from ralfloop_agent.glm_review.context import build_context_packet, canonical_json
from ralfloop_agent.glm_review.director import (
    Assignment,
    Blackboard,
    DirectorDecision,
    DirectorOrchestrator,
    VerifiedFact,
    capability_catalog,
    strategic_context_packet,
    utility_gate,
    validate_plan,
    preflight_grant_review,
)
from ralfloop_agent.glm_review.queue import GlmQueue
from ralfloop_agent.glm_review.service import GlmReviewService


TASK_ID = "race-task-001"


def payload(**overrides):
    value = {
        "goal": "Decide grant strategy",
        "constraints": ["No external writes"],
        "draft": "Project trains 20 participants with measurable outcomes.",
        "project_description": "Training project",
        "duration": "6 months",
        "participants": 20,
        "age_range": "18-30",
        "free_participation": True,
        "budget_items": [{"amount": 600}, {"amount": 400}],
        "budget_total": 1000,
        "deadline": "2099-01-01",
        "event_date": "2099-02-01",
        "organization_status": "APS",
        "requirements": ["measurable outcomes"],
        "evidence": [{"source_id": "E1", "location": "doc:1", "claim": "Eligible", "excerpt": "APS eligible."}],
        "known_gaps": ["partner confirmation"],
    }
    value.update(overrides)
    return value


def queue_job(tmp_path: Path, task_id: str = TASK_ID):
    task = tmp_path / task_id
    task.mkdir(parents=True, exist_ok=True)
    return {
        "task_id": task_id,
        "task_type": "grant_review",
        "state": "queued",
        "input_artifact": str(task / "input.json"),
        "packet_artifact": str(task / "packet.json"),
        "packet_hash": "a" * 64,
    }


def valid_review(task_id: str = TASK_ID):
    return {
        "schema_version": 1,
        "task_id": task_id,
        "verdict": "accept",
        "summary": "Ready",
        "critical_issues": [],
        "recommended_changes": [],
        "missing_evidence": [],
        "risk_flags": [],
        "confidence": 0.8,
        "requires_human_approval": False,
    }


def launcher(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "launcher"
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o700)
    return path


def assignment(task_id="a", *, depends=None, tools=None, worker="rules_engine", priority=0, objective="Check bounded fact"):
    return Assignment(
        task_id=task_id,
        worker=worker,
        objective=objective,
        input_refs=["input:project"],
        allowed_tools=tools or [],
        deliverable="fact",
        acceptance_tests=["ok"],
        depends_on=depends or [],
        priority=priority,
    )


def decision(round_number: int, assignments=None, action="assign"):
    return DirectorDecision(
        action=action,
        round=round_number,
        strategy="Bounded strategy",
        assignments=assignments or [],
        review_trigger="round_complete",
        stop_conditions=["goal_satisfied"],
        strategic_summary="Done" if action == "conclude" else "",
    )


def test_two_workers_get_one_atomic_claim(tmp_path):
    queue = GlmQueue(tmp_path / "state")
    queue.enqueue(queue_job(tmp_path))
    barrier = threading.Barrier(3)
    claimed = []

    def run(name):
        barrier.wait()
        claimed.append(queue.claim_next(force=True, worker_id=name))

    threads = [threading.Thread(target=run, args=(f"w{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    winners = [row for row in claimed if row]
    assert len(winners) == 1
    assert winners[0]["state"] == "running"
    assert winners[0]["attempts"] == 0


def test_claim_sets_token_worker_started_and_lease(tmp_path):
    queue = GlmQueue(tmp_path / "state")
    queue.enqueue(queue_job(tmp_path))
    row = queue.claim_next(force=True, worker_id="worker-A")
    assert row["run_token"] and row["worker_id"] == "worker-A"
    assert row["started_at"] and row["lease_expires_epoch"] > int(time.time())


def test_retry_rejected_during_running_without_mutation(tmp_path):
    queue = GlmQueue(tmp_path / "state")
    queue.enqueue(queue_job(tmp_path))
    before = queue.claim_next(force=True)
    response = queue.retry(TASK_ID)
    after = queue.get(TASK_ID)
    assert response["status"] == "active_run_not_retryable"
    for field in ("state", "attempts", "next_attempt_epoch", "run_token"):
        assert after[field] == before[field]


def test_stale_run_token_cannot_finalize_or_overwrite(tmp_path):
    queue = GlmQueue(tmp_path / "state")
    queue.enqueue(queue_job(tmp_path))
    claimed = queue.claim_next(force=True)
    result = queue.finish(
        TASK_ID, run_token="stale", provider_status="completed", envelope={"bad": True},
        generation_started=True, result_artifact="bad", error="bad",
    )
    current = queue.get(TASK_ID)
    assert result["status"] == "stale_run_token"
    assert current["state"] == "running" and current["run_token"] == claimed["run_token"]
    assert current["result_artifact"] is None and current["attempts"] == 0


def test_active_attempt_blocks_successor_even_force(tmp_path):
    queue = GlmQueue(tmp_path / "state")
    queue.enqueue(queue_job(tmp_path))
    assert queue.claim_next(force=True)
    assert queue.claim_next(force=True) is None


def test_exit_75_defers_without_attempt_and_persists_backoff(tmp_path):
    adapter = ColibriGlmAdapter(launcher=launcher(tmp_path, "cat >/dev/null\necho busy >&2\nexit 75\n"), timeout_seconds=2, ngen=64, outer_grace_seconds=0)
    service = GlmReviewService(state_dir=tmp_path / "state", adapter=adapter)
    service.enqueue_payload(payload(), task_type="grant_review", task_id=TASK_ID, immediate=True)
    result = service.process_one(force=True)
    assert result["state"] == "deferred_resource_busy"
    assert result["attempts"] == 0 and result["resource_deferrals"] == 1
    assert result["next_attempt_epoch"] > int(time.time()) and result["run_token"] is None


def test_only_real_generations_reach_max_attempts(tmp_path):
    queue = GlmQueue(tmp_path / "state")
    queue.enqueue(queue_job(tmp_path))
    for index in range(3):
        claimed = queue.claim_next(force=True)
        result = queue.finish(
            TASK_ID, run_token=claimed["run_token"], provider_status="invalid_output",
            envelope={}, generation_started=True, error="json_truncated",
        )
        if index < 2:
            with queue.connect() as conn:
                conn.execute("update glm_jobs set next_attempt_epoch=0 where task_id=?", (TASK_ID,))
    assert result["attempts"] == 3 and result["state"] == "failed"
    assert queue.retry(TASK_ID)["status"] == "max_attempts_reached"


def test_ngen_timeout_allowlist_reaches_launcher(tmp_path):
    seen = tmp_path / "seen"
    body = f"cat >/dev/null\nprintf '%s %s' \"$GLM_NGEN\" \"$GLM_TIMEOUT_SECONDS\" > {seen}\nprintf '%s\\n' '{json.dumps(valid_review())}'\n"
    packet = build_context_packet(payload(), task_id=TASK_ID, task_type="grant_review", max_chars=2_000)[0]
    result = ColibriGlmAdapter(launcher=launcher(tmp_path, body), timeout_seconds=5_400, ngen=192, outer_grace_seconds=0).review(packet, tmp_path / "artifacts")
    assert result.ok and seen.read_text() == "192 5400"


def test_grant_profile_and_explicit_override(monkeypatch, tmp_path):
    monkeypatch.delenv("GLM_NGEN", raising=False)
    monkeypatch.delenv("GLM_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("RALFLOOP_GLM_NGEN", "128")
    monkeypatch.setenv("RALFLOOP_GLM_TIMEOUT_SECONDS", "3600")
    service = GlmReviewService(state_dir=tmp_path / "one")
    profiled = service._adapter_for_job({"task_type": "grant_review"})
    assert profiled.ngen >= 256 and profiled.timeout_seconds >= 7_200
    monkeypatch.setenv("GLM_NGEN", "192")
    monkeypatch.setenv("GLM_TIMEOUT_SECONDS", "5400")
    explicit = GlmReviewService(state_dir=tmp_path / "two")._adapter_for_job({"task_type": "grant_review"})
    assert (explicit.ngen, explicit.timeout_seconds) == (192, 5_400)


def test_retry_overrides_persist_until_later_worker(monkeypatch, tmp_path):
    monkeypatch.delenv("GLM_NGEN", raising=False)
    monkeypatch.delenv("GLM_TIMEOUT_SECONDS", raising=False)
    state = tmp_path / "state"
    service = GlmReviewService(state_dir=state)
    service.enqueue_payload(payload(), task_type="grant_review", task_id=TASK_ID, immediate=True)
    claimed = service.queue.claim_next(force=True)
    service.queue.finish(
        TASK_ID, run_token=claimed["run_token"], provider_status="invalid_output",
        envelope={}, generation_started=True, error="json_truncated",
    )
    monkeypatch.setenv("GLM_NGEN", "192")
    monkeypatch.setenv("GLM_TIMEOUT_SECONDS", "5400")
    retry_service = GlmReviewService(state_dir=state)
    retried = retry_service.retry(TASK_ID)
    assert (retried["ngen_override"], retried["timeout_override"]) == (192, 5_400)
    monkeypatch.delenv("GLM_NGEN")
    monkeypatch.delenv("GLM_TIMEOUT_SECONDS")
    later = GlmReviewService(state_dir=state)
    configured = later._adapter_for_job(later.status(TASK_ID))
    assert (configured.ngen, configured.timeout_seconds) == (192, 5_400)


def test_envelope_result_and_atomic_files_always_exist(tmp_path):
    packet = build_context_packet(payload(), task_id=TASK_ID, task_type="grant_review", max_chars=2_000)[0]
    adapter = ColibriGlmAdapter(launcher=launcher(tmp_path, "cat >/dev/null\nprintf '%s\\n' '{\"schema_version\":1'\n"), timeout_seconds=2, ngen=64, outer_grace_seconds=0)
    adapter.review(packet, tmp_path / "attempt")
    assert {"prompt.txt", "stdout.txt", "stderr.txt", "result.json", "envelope.json"} <= {path.name for path in (tmp_path / "attempt").iterdir()}
    assert not list((tmp_path / "attempt").glob("*.tmp"))
    assert not list((tmp_path / "attempt").glob("*.running"))


def test_orphan_recovery_rebuilds_diagnostic_without_completing(tmp_path):
    service = GlmReviewService(state_dir=tmp_path / "state", adapter=object())
    service.enqueue_payload(payload(), task_type="grant_review", task_id=TASK_ID, immediate=True)
    attempt = service.artifacts_dir / TASK_ID / "attempt-01-fixture"
    attempt.mkdir()
    (attempt / "prompt.txt").write_text("prompt")
    (attempt / "stdout.txt").write_text('{"schema_version":1')
    (attempt / "stderr.txt").write_text("")
    assert service.recover_orphan_artifacts() == 1
    envelope = json.loads((attempt / "envelope.json").read_text())
    assert envelope["status"] == "invalid_output" and envelope["orphan"]
    assert (attempt / "result.json").exists()
    assert service.status(TASK_ID)["state"] != "completed"


def test_crash_after_subprocess_preserves_envelope_for_recovery(monkeypatch, tmp_path):
    body = "cat >/dev/null\nprintf '%s\\n' '" + json.dumps(valid_review()) + "'\n"
    service = GlmReviewService(
        state_dir=tmp_path / "state",
        adapter=ColibriGlmAdapter(launcher=launcher(tmp_path, body), timeout_seconds=2, ngen=64, outer_grace_seconds=0),
    )
    service.enqueue_payload(payload(), task_type="other", task_id=TASK_ID, immediate=True)
    original = service.queue.finish
    monkeypatch.setattr(service.queue, "finish", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("db_crash")))
    with pytest.raises(RuntimeError, match="db_crash"):
        service.process_one(force=True)
    attempt = next((service.artifacts_dir / TASK_ID).glob("attempt-*"))
    assert (attempt / "envelope.json").exists()
    monkeypatch.setattr(service.queue, "finish", original)
    with service.queue.connect() as conn:
        conn.execute("update glm_jobs set lease_expires_epoch=0,process_pid=null where task_id=?", (TASK_ID,))
    assert service.queue.recover_running() == 1
    assert service.recover_orphan_artifacts() == 1
    assert service.status(TASK_ID)["state"] == "completed"


def test_backend_reinstantiation_does_not_steal_active_claim(tmp_path):
    first = GlmReviewService(state_dir=tmp_path / "state", adapter=object())
    first.enqueue_payload(payload(), task_type="other", task_id=TASK_ID, immediate=True)
    claimed = first.queue.claim_next(force=True)
    second = GlmReviewService(state_dir=tmp_path / "state", adapter=object())
    assert second.status(TASK_ID)["run_token"] == claimed["run_token"]
    assert second.queue.claim_next(force=True) is None


def test_global_worker_lock_and_force_cannot_bypass(tmp_path):
    service = GlmReviewService(state_dir=tmp_path / "state", adapter=object())
    with service.queue.worker_lock() as acquired:
        assert acquired
        assert service.process_one(force=True)["status"] == "deferred_resource_busy"


def test_observed_race_regression_resource_deferral_not_third_attempt(tmp_path):
    queue = GlmQueue(tmp_path / "state")
    queue.enqueue(queue_job(tmp_path))
    first = queue.claim_next(force=True)
    queue.finish(TASK_ID, run_token=first["run_token"], provider_status="invalid_output", envelope={}, generation_started=True, error="json_truncated")
    queue.retry(TASK_ID)
    second = queue.claim_next(force=True)
    assert queue.retry(TASK_ID)["status"] == "active_run_not_retryable"
    assert queue.claim_next(force=True) is None
    queue.finish(TASK_ID, run_token=second["run_token"], provider_status="deferred_resource_busy", envelope={}, generation_started=False, error="RAM")
    row = queue.get(TASK_ID)
    assert row["attempts"] == 1 and row["state"] == "deferred_resource_busy"


def test_director_plan_schema_tool_policy_and_graph():
    catalog = capability_catalog()
    plan = decision(1, [assignment("second", depends=["first"]), assignment("first", priority=10)])
    assert [item.task_id for item in validate_plan(plan, catalog)] == ["first", "second"]
    denied = decision(1, [assignment(tools=["not-real"])])
    with pytest.raises(ValueError, match="tool_not_allowlisted"):
        validate_plan(denied, catalog)


def test_assignment_deduplicated_across_recovery(tmp_path):
    queue = GlmQueue(tmp_path / "state")
    calls = []
    worker = lambda item, context: calls.append(item.task_id) or {"ok": True, "deliverable": "x"}
    first = DirectorOrchestrator(queue, lambda packet: decision(1, [assignment()]), {"rules_engine": worker})
    assert first.run(task_id=TASK_ID, task_type="grant_review", payload=payload(), max_rounds=1)["status"] == "stop_budget_exhausted"
    second = DirectorOrchestrator(queue, lambda packet: decision(2, [assignment()]), {"rules_engine": worker})
    second.run(task_id=TASK_ID, task_type="grant_review", payload=payload(), max_rounds=2)
    assert calls == ["a"]


def test_round_limits_human_escalation_and_goal_stop(tmp_path):
    queue = GlmQueue(tmp_path / "state")
    limited = DirectorOrchestrator(queue, lambda packet: decision(packet["round"], [assignment()]), {"rules_engine": lambda i, c: {"ok": True, "deliverable": "x"}})
    result = limited.run(task_id="limit-task", task_type="grant_review", payload=payload(), max_rounds=1)
    assert result["status"] == "stop_budget_exhausted" and result["metrics"]["rounds"] == 1
    human = DirectorOrchestrator(queue, lambda packet: DirectorDecision(action="request_human_input", round=1, strategy="Need owner", human_question="Who signs?"), {}, model_configuration="human-fake")
    assert human.run(task_id="human-task", task_type="grant_review", payload=payload())["status"] == "request_human_input"
    goal = DirectorOrchestrator(queue, lambda packet: decision(1, [assignment()]), {"rules_engine": lambda i, c: {"ok": True, "deliverable": "x", "goal_satisfied": True}}, model_configuration="goal-fake")
    assert goal.run(task_id="goal-task", task_type="grant_review", payload=payload())["status"] == "conclude"


def test_utility_gate_retrospective_known_gaps_and_compact_delta():
    incomplete = payload(deadline=None, organization_status=None, call_status="closed")
    preflight = preflight_grant_review(incomplete)
    assert not utility_gate(incomplete, preflight).call_glm
    assert utility_gate(incomplete, preflight, retrospective=True).call_glm
    board = Blackboard(
        goal="Review",
        verified_facts=[VerifiedFact(fact="declared_gap=partner", evidence_refs=["input:known_gaps"])],
        open_questions=["strategy"],
    )
    packet = strategic_context_packet(board, capability_catalog(), delta={"completed": ["a"]}, reason="round_complete")
    encoded = canonical_json(packet)
    assert len(encoded) <= 2_000 and "draft" not in packet
    assert packet["known_gaps"] == ["partner"]
    assert "partner" not in packet["open_strategic_questions"]
    assert packet["prior_round_delta"] == {"completed": ["a"]}


def test_cache_invalidates_on_goal_and_metrics_split(tmp_path):
    queue = GlmQueue(tmp_path / "state")
    orchestrator = DirectorOrchestrator(queue, lambda packet: decision(1, [], action="conclude"), {})
    one = strategic_context_packet(Blackboard(goal="one"), orchestrator.catalog, reason="initial")
    two = strategic_context_packet(Blackboard(goal="two"), orchestrator.catalog, reason="initial")
    assert orchestrator._cache_key(one) != orchestrator._cache_key(two)
    result = orchestrator.run(task_id="metrics-task", task_type="grant_review", payload=payload(goal="one"))
    assert {"director_calls", "worker_calls", "tool_calls", "preflight_ms"} <= result["metrics"].keys()


def test_external_action_without_approval_rejected(tmp_path):
    queue = GlmQueue(tmp_path / "state")
    orchestrator = DirectorOrchestrator(
        queue,
        lambda packet: decision(1, [assignment()]),
        {"rules_engine": lambda i, c: {"ok": True, "deliverable": "x", "external_action": "send"}},
    )
    result = orchestrator.run(task_id="approval-task", task_type="grant_review", payload=payload(), max_rounds=1)
    failed = result["blackboard"]["failed_assignments"]
    assert failed[0]["result"]["status"] == "external_action_requires_approval"


def test_recovery_after_crash_mid_round_skips_completed_assignment(tmp_path):
    queue = GlmQueue(tmp_path / "state")
    calls = []

    def crashing(item, context):
        calls.append(item.task_id)
        if item.task_id == "b":
            raise RuntimeError("crash")
        return {"ok": True, "deliverable": item.task_id}

    plan = [assignment("a", objective="Check A"), assignment("b", depends=["a"], objective="Check B")]
    first = DirectorOrchestrator(queue, lambda packet: decision(1, plan), {"rules_engine": crashing}, model_configuration="crash-fake")
    with pytest.raises(RuntimeError, match="crash"):
        first.run(task_id="crash-round", task_type="grant_review", payload=payload(), max_rounds=1)

    second = DirectorOrchestrator(queue, lambda packet: decision(1, plan), {"rules_engine": lambda i, c: calls.append(i.task_id) or {"ok": True, "deliverable": i.task_id}}, model_configuration="crash-fake")
    second.run(task_id="crash-round", task_type="grant_review", payload=payload(), max_rounds=1)
    assert calls.count("a") == 1 and calls.count("b") == 2


def test_service_director_cycle_fake_launcher_updates_blackboard_and_stops(tmp_path):
    assigned = decision(1, [assignment(tools=["grant_preflight"])]).model_dump_json()
    concluded = decision(2, action="conclude").model_dump_json()
    body = """input=$(cat)
case "$input" in
  *'STATE='*'\"round\":1'*) printf '%s\\n' 'ASSIGNED' ;;
  *) printf '%s\\n' 'CONCLUDED' ;;
esac
""".replace("ASSIGNED", assigned).replace("CONCLUDED", concluded)
    service = GlmReviewService(
        state_dir=tmp_path / "state",
        adapter=ColibriGlmAdapter(launcher=launcher(tmp_path, body), timeout_seconds=2, ngen=64, outer_grace_seconds=0),
    )
    service._external_adapter = False
    service.enqueue_payload(payload(), task_type="grant_review", task_id=TASK_ID, immediate=True)
    result = service.process_one(force=True)
    assert result["state"] == "completed" and result["attempts"] == 2
    director_result = json.loads((service.artifacts_dir / TASK_ID / "director-result.json").read_text())
    assert director_result["status"] == "conclude"
    assert director_result["blackboard"]["completed_assignments"]
    facts = director_result["blackboard"]["verified_facts"]
    assert len(facts) == len({(row["fact"], tuple(row["evidence_refs"])) for row in facts})
    assert director_result["metrics"]["rounds"] == 2
