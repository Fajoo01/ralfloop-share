from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import threading
import time

import pytest

from ralfloop_agent.domains.domain_approval import DomainApprovalDecision
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.domains.bandi_glm_review import enqueue_grant_review
from ralfloop_agent.domains.bandi_semantic_retrieval import make_evidence
from ralfloop_agent.domains.bandi_weekly_research import RunResult, WeeklyConfig, _enqueue_slow_reviews
from ralfloop_agent.glm_review.adapter import ColibriGlmAdapter, build_review_prompt
from ralfloop_agent.glm_review.context import build_context_packet, contains_secret, packet_hash
from ralfloop_agent.glm_review.models import AdapterEnvelope, AdapterStatus, ContextPacket, GlmReview
from ralfloop_agent.glm_review.parser import ReviewParseError, parse_glm_review
from ralfloop_agent.glm_review.queue import GlmQueue, in_night_window
from ralfloop_agent.glm_review.service import GlmReviewService
from ralfloop_agent.glm_review.validators import build_candidate_artifact, validate_review_artifact


TASK_ID = "task-123"


def valid_review(task_id: str = TASK_ID) -> dict:
    return {
        "schema_version": 1,
        "task_id": task_id,
        "verdict": "revise",
        "summary": "Clarify the evidence and KPI chain.",
        "critical_issues": [{"severity": "high", "issue": "Evidence missing", "evidence_refs": ["S1"]}],
        "recommended_changes": [{"target": "KPI", "change": "Add baseline", "reason": "Measurability"}],
        "missing_evidence": ["baseline"],
        "risk_flags": ["deadline"],
        "confidence": 0.7,
        "requires_human_approval": False,
    }


def packet_payload() -> dict:
    return {
        "goal": "Review grant application",
        "constraints": ["No external action"],
        "draft": "Objective: train 20 people. Activities and KPI baseline need detail.",
        "requirements": ["Training objective"],
        "evidence": [{"source_id": "S1", "location": "doc:1", "claim": "Need baseline", "excerpt": "The call requires measurable baselines."}],
        "budget_summary": {"items": [{"amount": 10}], "total": 10},
        "known_gaps": ["baseline"],
    }


def make_packet(payload: dict | None = None, *, max_chars: int = 10_000) -> ContextPacket:
    return build_context_packet(payload or packet_payload(), task_id=TASK_ID, task_type="grant_review", max_chars=max_chars)[0]


def fake_launcher(tmp_path: Path, body: str) -> Path:
    path = tmp_path / ("fake-" + hashlib.sha256(body.encode()).hexdigest()[:8])
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o700)
    return path


def adapter(tmp_path: Path, body: str, **kwargs) -> ColibriGlmAdapter:
    return ColibriGlmAdapter(
        launcher=fake_launcher(tmp_path, body),
        timeout_seconds=kwargs.pop("timeout_seconds", 2),
        outer_grace_seconds=kwargs.pop("outer_grace_seconds", 0),
        ngen=64,
        **kwargs,
    )


def emit(value: str, *, rc: int = 0) -> str:
    return "cat >/dev/null\nprintf '%s\\n' " + repr(value) + f"\nexit {rc}\n"


def test_context_packet_build_and_hash_are_deterministic():
    first, metadata = build_context_packet(packet_payload(), task_id=TASK_ID, task_type="grant_review")
    second = make_packet()
    assert first == second
    assert metadata["sha256"] == packet_hash(first)


def test_context_packet_limit_is_enforced():
    payload = packet_payload() | {"draft": "x" * 50_000}
    packet, metadata = build_context_packet(payload, task_id=TASK_ID, task_type="grant_review", max_chars=2_500)
    assert metadata["characters"] <= 2_500
    assert len(packet.draft) < 2_500


def test_evidence_is_deduplicated_and_ranked():
    row = packet_payload()["evidence"][0]
    packet = make_packet(packet_payload() | {"evidence": [row, row, *[row | {"source_id": f"S{i}", "excerpt": f"row {i}"} for i in range(20)]]})
    assert len(packet.evidence) == 8
    assert len({item.source_id for item in packet.evidence}) == 8


def test_secrets_are_redacted_from_packet_and_prompt():
    packet, metadata = build_context_packet(packet_payload() | {"draft": "api_key=supersecretvalue"}, task_id=TASK_ID, task_type="grant_review")
    assert metadata["redactions"] == 1
    assert "supersecretvalue" not in build_review_prompt(packet)
    assert not contains_secret(build_review_prompt(packet))


def test_valid_json_schema():
    review = parse_glm_review(json.dumps(valid_review()))
    assert review.verdict == "revise"


def test_json_inside_markdown():
    review = parse_glm_review("```json\n" + json.dumps(valid_review()) + "\n```")
    assert review.task_id == TASK_ID


def test_text_around_json():
    review = parse_glm_review("analysis first\n" + json.dumps(valid_review()) + "\ndone")
    assert review.confidence == 0.7


def test_colibri_progress_lines_inside_json_are_removed():
    raw = json.dumps(valid_review())
    split = raw.index('"task_id"') + 5
    mixed = raw[:split] + "\n[t=16  RSS 29.05 GB  hit 30%  0.08 tok/s  1.00 tok/fw]\n" + raw[split:]
    assert parse_glm_review(mixed).task_id == TASK_ID


@pytest.mark.parametrize(
    ("raw", "error"),
    [("{\"schema_version\":1", "json_truncated"), ("", "output_empty"), ("not json", "json_object_missing")],
)
def test_invalid_json_cases(raw, error):
    with pytest.raises(ReviewParseError, match=error):
        parse_glm_review(raw)


def test_truncated_model_json_wins_over_earlier_diagnostic_object():
    with pytest.raises(ReviewParseError, match="json_truncated"):
        parse_glm_review('{"diagnostic":true}\n{"schema_version":1,"task_id":"x"')


def test_missing_and_wrong_fields_are_rejected():
    raw = valid_review(); raw.pop("verdict"); raw["confidence"] = "high"
    with pytest.raises(ReviewParseError, match="schema_invalid"):
        parse_glm_review(json.dumps(raw))


def test_model_cannot_set_human_approval_true():
    raw = valid_review(); raw["requires_human_approval"] = True
    with pytest.raises(ReviewParseError, match="schema_invalid"):
        parse_glm_review(json.dumps(raw))


def test_non_utf8_and_output_limit_are_rejected():
    with pytest.raises(ReviewParseError, match="output_not_utf8"):
        parse_glm_review(b"\xff")
    with pytest.raises(ReviewParseError, match="output_limit_exceeded"):
        parse_glm_review(b"x" * 100, max_bytes=50)


def test_adapter_valid_output_and_protected_artifacts(tmp_path):
    result = adapter(tmp_path, emit(json.dumps(valid_review()))).review(make_packet(), tmp_path / "artifacts")
    assert result.status == AdapterStatus.COMPLETED
    assert Path(result.prompt_artifact).stat().st_mode & 0o077 == 0
    assert result.result["task_id"] == TASK_ID


def test_adapter_exit_75_is_deferred(tmp_path):
    result = adapter(tmp_path, "cat >/dev/null\necho busy >&2\nexit 75\n").review(make_packet(), tmp_path / "a")
    assert result.status == AdapterStatus.DEFERRED_RESOURCE_BUSY


def test_adapter_timeout_kills_process_group(tmp_path):
    result = adapter(tmp_path, "cat >/dev/null\nsleep 5\n", timeout_seconds=1).review(make_packet(), tmp_path / "a")
    assert result.status == AdapterStatus.TIMEOUT


def test_adapter_empty_truncated_and_excess_output(tmp_path):
    empty = adapter(tmp_path, emit("")).review(make_packet(), tmp_path / "empty")
    truncated = adapter(tmp_path, emit('{"schema_version":1')).review(make_packet(), tmp_path / "truncated")
    excess = adapter(tmp_path, emit("x" * 2_000), max_output_bytes=1_024).review(make_packet(), tmp_path / "excess")
    assert [empty.status, truncated.status, excess.status] == [AdapterStatus.INVALID_OUTPUT] * 3


def test_hostile_shell_text_is_only_stdin(tmp_path):
    marker = tmp_path / "owned"
    payload = packet_payload() | {"draft": f"$(touch {marker}) `touch {marker}` ; touch {marker}"}
    result = adapter(tmp_path, emit(json.dumps(valid_review()))).review(make_packet(payload), tmp_path / "a")
    assert result.ok
    assert not marker.exists()


def queue_job(tmp_path: Path, task_id: str = TASK_ID, **overrides) -> dict:
    task = tmp_path / task_id
    task.mkdir(parents=True, exist_ok=True)
    values = {
        "task_id": task_id,
        "task_type": "grant_review",
        "state": "queued",
        "priority": 0,
        "deadline": None,
        "input_artifact": str(task / "input.json"),
        "packet_artifact": str(task / "packet.json"),
        "packet_hash": "a" * 64,
        "external_action": None,
    }
    values.update(overrides)
    return values


def test_queue_persistence_and_idempotence(tmp_path):
    queue = GlmQueue(tmp_path / "state")
    first = queue.enqueue(queue_job(tmp_path))
    second = GlmQueue(tmp_path / "state").enqueue(queue_job(tmp_path))
    assert first["task_id"] == second["task_id"]
    assert second["idempotent"]
    assert len(queue.list()) == 1


def test_queue_recovers_running_after_restart(tmp_path):
    queue = GlmQueue(tmp_path / "state")
    queue.enqueue(queue_job(tmp_path))
    assert queue.claim_next(force=True)["state"] == "running"
    with queue.connect() as conn:
        conn.execute("update glm_jobs set lease_expires_epoch=0 where task_id=?", (TASK_ID,))
    assert GlmQueue(tmp_path / "state").recover_running() == 1
    assert queue.get(TASK_ID)["state"] == "orphaned_attempt"


def test_resource_deferral_does_not_consume_attempts(tmp_path):
    queue = GlmQueue(tmp_path / "state")
    queue.enqueue(queue_job(tmp_path))
    for _ in range(3):
        claimed = queue.claim_next(force=True)
        result = queue.finish(
            TASK_ID, run_token=claimed["run_token"], provider_status="deferred_resource_busy",
            envelope={}, generation_started=False, error="busy",
        )
        assert result["state"] == "deferred_resource_busy"
        with queue.connect() as conn:
            conn.execute("update glm_jobs set next_attempt_epoch=0 where task_id=?", (TASK_ID,))
    assert result["attempts"] == 0
    assert result["resource_deferrals"] == 3


def test_worker_lock_serializes(tmp_path):
    queue = GlmQueue(tmp_path / "state")
    with queue.worker_lock() as first:
        with GlmQueue(tmp_path / "state").worker_lock() as second:
            assert first and not second


def test_priority_and_deadline_order(tmp_path):
    queue = GlmQueue(tmp_path / "state")
    queue.enqueue(queue_job(tmp_path, "late", priority=10, deadline="2026-12-01"))
    queue.enqueue(queue_job(tmp_path, "urgent", priority=100, deadline="2026-08-02"))
    assert queue.claim_next(force=True)["task_id"] == "urgent"


def test_night_window_europe_rome_boundaries():
    from zoneinfo import ZoneInfo
    zone = ZoneInfo("Europe/Rome")
    assert in_night_window(datetime(2026, 8, 1, 23, 0, tzinfo=zone))
    assert in_night_window(datetime(2026, 8, 2, 7, 30, tzinfo=zone))
    assert not in_night_window(datetime(2026, 8, 2, 8, 0, tzinfo=zone))


def test_cancel_only_non_active(tmp_path):
    queue = GlmQueue(tmp_path / "state")
    queue.enqueue(queue_job(tmp_path))
    queue.claim_next(force=True)
    assert queue.cancel(TASK_ID)["status"] == "active_job_not_cancellable"


def completed_for_approval(queue: GlmQueue, tmp_path: Path) -> dict:
    queue.enqueue(queue_job(tmp_path, external_action="send_email"))
    claimed = queue.claim_next(force=True)
    result = tmp_path / "candidate.json"; result.write_text("{}", encoding="utf-8")
    return queue.finish(
        TASK_ID, run_token=claimed["run_token"], provider_status="completed", envelope={},
        generation_started=True, result_artifact=str(result), result_hash="b" * 64,
    )


def test_approval_is_bound_to_hash_version_action(tmp_path):
    queue = GlmQueue(tmp_path / "state")
    completed_for_approval(queue, tmp_path)
    approved = queue.decide(TASK_ID, "approved", approver="alice", channel="cli")
    assert approved["state"] == "approved"
    assert queue.approval_allows_dispatch(TASK_ID, artifact_hash="b" * 64, action="send_email")
    assert not queue.approval_allows_dispatch(TASK_ID, artifact_hash="c" * 64, action="send_email")


def test_artifact_change_invalidates_approval(tmp_path):
    queue = GlmQueue(tmp_path / "state")
    completed_for_approval(queue, tmp_path)
    queue.decide(TASK_ID, "approved", approver="alice", channel="cli")
    changed = tmp_path / "changed.json"; changed.write_text('{"changed":true}', encoding="utf-8")
    row = queue.replace_result(TASK_ID, result_artifact=str(changed), result_hash="c" * 64)
    assert row["state"] == "awaiting_user_approval"
    assert not queue.approval_allows_dispatch(TASK_ID, artifact_hash="c" * 64, action="send_email")


def test_dispatch_denied_without_approval(tmp_path):
    queue = GlmQueue(tmp_path / "state")
    completed_for_approval(queue, tmp_path)
    assert not queue.approval_allows_dispatch(TASK_ID, artifact_hash="b" * 64, action="send_email")


class FakeAdapter:
    def review(self, packet, artifact_dir):
        artifact_dir = Path(artifact_dir); artifact_dir.mkdir(parents=True)
        prompt = artifact_dir / "prompt.txt"; result = artifact_dir / "result.json"
        prompt.write_text("redacted", encoding="utf-8"); result.write_text(json.dumps(valid_review(packet.task_id)), encoding="utf-8")
        return AdapterEnvelope(ok=True, status="completed", exit_code=0, duration_ms=1, result=valid_review(packet.task_id), prompt_artifact=str(prompt), result_artifact=str(result), launcher_log="", error=None)


class MixedDiagnosticInvalidAdapter:
    def review(self, packet, artifact_dir):
        artifact_dir = Path(artifact_dir); artifact_dir.mkdir(parents=True)
        prompt = artifact_dir / "prompt.txt"; result = artifact_dir / "result.json"; stdout = artifact_dir / "stdout.txt"
        prompt.write_text("redacted", encoding="utf-8"); result.write_text("{}", encoding="utf-8")
        raw = json.dumps(valid_review(packet.task_id)); split = raw.index('"task_id"') + 5
        stdout.write_text(raw[:split] + "\n[t=16  RSS 29 GB  0.08 tok/s  1.00 tok/fw]\n" + raw[split:], encoding="utf-8")
        return AdapterEnvelope(ok=False, status="invalid_output", exit_code=0, duration_ms=1, result={}, prompt_artifact=str(prompt), result_artifact=str(result), launcher_log="", error="json_object_missing")


def test_service_end_to_end_fake_and_digest_dry_run(tmp_path):
    service = GlmReviewService(state_dir=tmp_path / "state", adapter=FakeAdapter())
    queued = service.enqueue_payload(packet_payload(), task_type="grant_review", task_id=TASK_ID, immediate=True)
    assert queued["state"] == "queued"
    completed = service.process_one(force=True)
    assert completed["state"] == "completed"
    result = service.result(TASK_ID)
    assert result["application_mode"] == "consultative_suggestions_not_applied"
    assert result["external_action_allowed"] is False
    digest = service.digest(dry_run=True)
    assert digest["status"] == "dry_run" and digest["outbox"] is None


def test_retry_reparses_saved_raw_output_before_new_inference(tmp_path):
    service = GlmReviewService(state_dir=tmp_path / "state", adapter=MixedDiagnosticInvalidAdapter())
    service.enqueue_payload(packet_payload(), task_type="other", task_id=TASK_ID, immediate=True)
    invalid = service.process_one(force=True)
    assert invalid["state"] == "invalid_output" and invalid["attempts"] == 1
    completed = service.retry(TASK_ID)
    assert completed["state"] == "completed" and completed["attempts"] == 1
    assert service.result(TASK_ID)["glm_review"]["task_id"] == TASK_ID


def test_input_snapshot_redacts_secret_values(tmp_path):
    service = GlmReviewService(state_dir=tmp_path / "state", adapter=FakeAdapter())
    queued = service.enqueue_payload(packet_payload() | {"draft": "password=do-not-store"}, task_type="other", task_id=TASK_ID)
    snapshot = Path(queued["input_artifact"]).read_text(encoding="utf-8")
    assert "do-not-store" not in snapshot
    assert "<redacted>" in snapshot


def test_existing_telegram_gate_approves_exact_glm_artifact(monkeypatch, tmp_path):
    monkeypatch.setenv("RALFLOOP_ENABLE_TELEGRAM_APPROVAL_GATE", "1")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_DB", str(tmp_path / "approval.sqlite3"))
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_AUDIT_LOG", str(tmp_path / "approval.jsonl"))
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_OUTBOX", str(tmp_path / "outbox.jsonl"))
    service = GlmReviewService(state_dir=tmp_path / "state", adapter=FakeAdapter())
    service.enqueue_payload(packet_payload(), task_type="grant_review", task_id=TASK_ID, immediate=True, external_action="send_email")
    waiting = service.process_one(force=True)
    assert waiting["state"] == "awaiting_user_approval"
    request_id = waiting["approval_request_id"]
    store = DomainApprovalStore(db_path=tmp_path / "approval.sqlite3")
    request = store.get_request(request_id)
    decision = store.decide(
        DomainApprovalDecision(request_id, "approve", 1, 1, 1, idempotency_key="glm-approval"),
        scope_digest_short=request["scope_digest_short"],
    )
    assert decision["status"] == "approved"
    approved = service.status(TASK_ID)
    assert approved["state"] == "approved"
    assert service.queue.approval_allows_dispatch(TASK_ID, artifact_hash=approved["result_hash"], action="send_email")


def test_deterministic_validators_budget_sources_policy():
    packet = make_packet()
    review = GlmReview.model_validate(valid_review())
    source = packet_payload() | {"proposal": {"objectives": [1], "activities": [1], "kpis": [1]}, "deadline": "2099-01-01"}
    report = validate_review_artifact(packet, review, source)
    assert report.ok
    candidate = build_candidate_artifact(packet, review, report, artifact_version=1)
    assert not candidate["external_action_allowed"]


def test_bandi_bridge_reuses_retrieval_and_enqueues(tmp_path):
    service = GlmReviewService(state_dir=tmp_path / "state", adapter=FakeAdapter())
    evidence = [make_evidence(source_id="official", document_id="call", canonical_url_value="https://example.invalid/call", source_type="web", text="Applicants must define measurable KPI.")]
    result = enqueue_grant_review(task_id="bando-review-1", draft="Draft", requirements=["KPI"], evidence=evidence, goal="Review", service=service)
    assert result["task_type"] == "grant_review"
    packet = json.loads(Path(result["packet_artifact"]).read_text(encoding="utf-8"))
    assert packet["evidence"][0]["source_id"].startswith("E")


def test_weekly_bandi_high_priority_hook_enqueues_night_review(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setenv("RALFLOOP_GLM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(GlmReviewService, "enqueue_payload", lambda self, payload, **kwargs: seen.append((payload, kwargs)) or {"state": "waiting_night_window"})
    opportunity = {
        "priority": "HIGH", "status": "open", "title": "Synthetic call", "call_key": "a" * 64,
        "content_hash": "b" * 64, "candidate_project": "Draft", "why_tiremm_should_apply": "Fit",
        "next_three_actions": ["Check"], "beneficiaries_evidence": ["APS"], "deadline": "2099-01-01",
        "partnership_required": False, "amounts": ["1000"], "criticalities": [], "score": 90,
        "retrieval": {"evidence": [{"chunk_id": "E1", "canonical_url": "https://example.invalid", "text": "Official requirement"}]},
    }
    result = RunResult(1, "run", "start", "finish", True, False, "completed", {}, opportunities=[opportunity])
    count, errors = _enqueue_slow_reviews(result, WeeklyConfig(glm_review_auto_enqueue=True, glm_review_max_jobs=3))
    assert count == 1 and not errors
    assert seen[0][1]["task_type"] == "grant_review"
    assert seen[0][0]["evidence"][0]["source_id"] == "E1"


def test_queue_metrics_and_pause_resume(tmp_path):
    queue = GlmQueue(tmp_path / "state")
    queue.enqueue(queue_job(tmp_path))
    queue.set_paused(True)
    assert queue.claim_next(force=True) is None
    queue.set_paused(False)
    assert queue.metrics()["queue_length"] == 1


def test_no_shell_true_in_adapter_source():
    source = Path(__file__).parents[1] / "ralfloop_agent/glm_review/adapter.py"
    text = source.read_text(encoding="utf-8")
    assert "shell=False" in text
    assert "shell=True" not in text


def test_interactive_provider_not_replaced():
    source = Path(__file__).parents[1] / "ralfloop_agent/providers/inference_runtime.py"
    assert "colibri" not in source.read_text(encoding="utf-8").casefold()
