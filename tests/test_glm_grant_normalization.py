from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import threading

from ralfloop_agent.glm_review import cli as glm_cli
from ralfloop_agent.glm_review.cli import add_glm_parser
from ralfloop_agent.glm_review.director import PreflightResult, preflight_grant_review, utility_gate
from ralfloop_agent.glm_review.models import AdapterEnvelope, ContextPacket
from ralfloop_agent.glm_review.normalizer import (
    HISTORICAL_GRANT_FIELDS,
    normalize_grant_review,
)
from ralfloop_agent.glm_review.queue import GlmQueue
from ralfloop_agent.glm_review.service import GlmReviewService


FIXTURE_DIR = Path(__file__).parent / "fixtures/glm-grant-review-cf9cb145c35bd78304b5"
HISTORICAL_INPUT = FIXTURE_DIR / "input.json"
HISTORICAL_PACKET = FIXTURE_DIR / "context-packet.json"
FAKE_LAUNCHER = Path(__file__).parent / "fixtures/glm-director-fake-launcher"
TASK_ID = "meet-code-normalization-test"


def historical_input() -> dict:
    return json.loads(HISTORICAL_INPUT.read_text(encoding="utf-8"))


def valid_review(task_id: str) -> dict:
    return {
        "schema_version": 1,
        "task_id": task_id,
        "verdict": "accept",
        "summary": "Evidence-grounded preflight complete.",
        "critical_issues": [],
        "recommended_changes": [],
        "missing_evidence": [],
        "risk_flags": [],
        "confidence": 0.8,
        "requires_human_approval": False,
    }


class RecordingAdapter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def review(self, packet: ContextPacket, artifact_dir: str | Path) -> AdapterEnvelope:
        self.calls.append(packet.task_id)
        directory = Path(artifact_dir)
        directory.mkdir(parents=True, exist_ok=True)
        prompt = directory / "prompt.txt"
        result = directory / "result.json"
        prompt.write_text("fake launcher", encoding="utf-8")
        result.write_text(json.dumps(valid_review(packet.task_id)), encoding="utf-8")
        return AdapterEnvelope(
            ok=True,
            status="completed",
            exit_code=0,
            duration_ms=1,
            result=valid_review(packet.task_id),
            prompt_artifact=str(prompt),
            result_artifact=str(result),
            model_started=True,
        )


class BombAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def review(self, packet: ContextPacket, artifact_dir: str | Path) -> AdapterEnvelope:
        self.calls += 1
        raise AssertionError("utility_gate_or_launcher_must_not_run")


def test_historical_grant_review_format_is_selected_and_preserved():
    raw = historical_input()
    normalized, report = normalize_grant_review(raw, source_file=str(HISTORICAL_INPUT))
    selected = raw["payload"]
    assert report["selected_source_path"] == "$.payload"
    assert report["fields_preserved"] == list(HISTORICAL_GRANT_FIELDS)
    for field in HISTORICAL_GRANT_FIELDS:
        assert normalized[field] == selected[field]
    assert report["mapping_error"] is False
    assert all(len(value) == 64 for value in report["source_hashes"].values())


def test_historical_draft_evidence_and_structured_values_are_recovered():
    raw = historical_input()
    normalized, report = normalize_grant_review(raw)
    assert normalized["draft"] == raw["payload"]["draft"]
    assert len(normalized["evidence"]) == len(raw["payload"]["evidence"]) == 5
    assert normalized["age_range"] == "11-16"
    assert normalized["participant_count"] == 20
    assert normalized["duration_minutes"] == 240
    assert normalized["same_group"] is True
    assert normalized["free_event"] is True
    assert normalized["budget_total"] == 500
    assert sum(row["amount"] for row in normalized["budget_items"]) == 500
    assert normalized["deadline"] == "2026-07-24"
    assert normalized["event_window"] == {"start": "2026-08-16", "end": "2026-10-31"}
    for name in ("age_range", "duration_minutes", "budget_total", "deadline", "event_window"):
        assert report["fields_derived"][name]["source_path"].startswith("$.payload")
        assert normalized["field_provenance"][name] == report["fields_derived"][name]


def test_context_packet_keeps_draft_evidence_source_ids_and_history(tmp_path):
    service = GlmReviewService(state_dir=tmp_path / "state", adapter=RecordingAdapter())
    queued = service.enqueue_file(HISTORICAL_INPUT, task_type="grant_review", task_id=TASK_ID, immediate=True)
    packet = ContextPacket.model_validate_json(Path(queued["packet_artifact"]).read_text(encoding="utf-8"))
    selected = historical_input()["payload"]
    assert packet.draft == selected["draft"]
    assert len(packet.evidence) == len(selected["evidence"])
    assert [row.source_id for row in packet.evidence] == [row["source_id"] for row in selected["evidence"]]
    assert packet.requirements == selected["requirements"]
    assert packet.budget_summary == selected["budget_summary"]
    assert packet.known_gaps == selected["known_gaps"]
    assert packet.requested_review == selected["requested_review"]
    report = json.loads((Path(queued["input_artifact"]).parent / "normalization-report.json").read_text(encoding="utf-8"))
    assert set(("input_keys", "selected_source_path", "normalized_keys", "fields_preserved", "fields_derived", "fields_missing", "warnings", "source_hashes")) <= set(report)
    assert report["source_hashes"]["context_packet_sha256"] == queued["packet_hash"]


def test_historical_packet_fixture_keeps_source_id():
    packet = ContextPacket.model_validate_json(HISTORICAL_PACKET.read_text(encoding="utf-8"))
    source_ids = [item.source_id for item in packet.evidence]
    assert source_ids == [
        "meet-code-conditions-eligibility",
        "meet-code-conditions-window",
        "meet-code-conditions-deadline",
        "meet-code-conditions-grant",
        "tiremm-profile",
    ]


def test_known_gaps_are_preserved_not_rediscovered_as_missing():
    raw = historical_input()
    normalized, _ = normalize_grant_review(raw)
    preflight = preflight_grant_review(raw)
    assert normalized["known_gaps"] == raw["payload"]["known_gaps"]
    assert preflight.known_gaps == raw["payload"]["known_gaps"]
    assert "event_window" not in preflight.missing_fields
    assert "deadline" not in preflight.missing_fields
    assert not preflight.blockers
    assert preflight.schema_valid is True
    assert preflight.proposal_complete is False
    assert preflight.eligibility_valid is True


def test_candidate_contains_preserved_and_derived_grant_data_without_false_missing(tmp_path):
    adapter = RecordingAdapter()
    service = GlmReviewService(state_dir=tmp_path / "state", adapter=adapter)
    service.enqueue_file(HISTORICAL_INPUT, task_type="grant_review", task_id=TASK_ID, immediate=True)
    completed = service.process_task(TASK_ID, force=True)
    assert completed["state"] == "completed"
    candidate = service.result(TASK_ID)
    assert candidate["draft"]
    assert candidate["evidence_count"] == 5
    assert candidate["known_gaps"] == historical_input()["payload"]["known_gaps"]
    assert candidate["structured_grant"]["age_range"] == "11-16"
    assert candidate["structured_grant"]["duration_minutes"] == 240
    assert candidate["structured_grant"]["budget_total"] == 500
    assert candidate["structured_grant"]["deadline"] == "2026-07-24"
    assert candidate["schema_valid"] is True
    assert candidate["proposal_complete"] is False
    assert candidate["eligibility_valid"] is True
    codes = {row["code"] for row in candidate["deterministic_validation"]["findings"]}
    assert not ({"objectives_missing", "activities_missing", "kpis_missing", "budget_not_checkable", "deadline_missing"} & codes)
    assert candidate["glm_review"]["missing_evidence"] == []


def test_deterministic_fake_launcher_canary_uses_normalized_history(monkeypatch, tmp_path):
    monkeypatch.setenv("RALFLOOP_COLIBRI_LAUNCHER", str(FAKE_LAUNCHER))
    monkeypatch.setenv("RALFLOOP_GLM_CONTEXT_MAX_CHARS", "6000")
    service = GlmReviewService(state_dir=tmp_path / "state")
    service.enqueue_file(HISTORICAL_INPUT, task_type="grant_review", task_id=TASK_ID, immediate=True)
    result = service.process_task(TASK_ID, force=True)
    assert result["state"] == "completed"
    assert result["attempts"] == 2
    candidate = service.result(TASK_ID)
    assert candidate["draft"]
    assert candidate["evidence_count"] == 5
    assert candidate["structured_grant"]["age_range"] == "11-16"
    assert candidate["structured_grant"]["duration_minutes"] == 240
    assert candidate["structured_grant"]["budget_total"] == 500
    assert candidate["structured_grant"]["deadline"] == "2026-07-24"


def test_source_draft_mapping_error_is_terminal_and_skips_gate_cache_launcher(tmp_path):
    adapter = BombAdapter()
    service = GlmReviewService(state_dir=tmp_path / "state", adapter=adapter)
    queued = service.enqueue_file(HISTORICAL_INPUT, task_type="grant_review", task_id=TASK_ID, immediate=True)
    packet_path = Path(queued["packet_artifact"])
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    packet["draft"] = ""
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    result = service.process_task(TASK_ID, force=True)
    assert result["state"] == "invalid_input"
    assert result["attempts"] == 0
    assert result["last_error"] == "input_mapping_error"
    assert result["completed_at"] is None
    assert result["result_artifact"] is None
    assert adapter.calls == 0
    with sqlite3.connect(service.queue.db_path) as conn:
        cache_table = conn.execute("select count(*) from sqlite_master where type='table' and name='glm_director_cache'").fetchone()[0]
        cache_rows = conn.execute("select count(*) from glm_director_cache").fetchone()[0] if cache_table else 0
    assert cache_rows == 0
    report = json.loads((Path(queued["input_artifact"]).parent / "normalization-report.json").read_text(encoding="utf-8"))
    assert report["mapping_error"] is True
    assert "input_mapping_error:source_draft_became_empty" in report["warnings"]


def test_utility_gate_rejects_mapping_error_before_cache_or_insufficient_evidence():
    preflight = PreflightResult(
        ok=False,
        schema_valid=False,
        proposal_complete=False,
        eligibility_valid=False,
        mapping_errors=["input_mapping_error"],
        missing_fields=["project_description", "deadline", "budget_sum"],
        blockers=["missing_required:project_description"],
    )
    decision = utility_gate({}, preflight, cached=True)
    assert decision.call_glm is False
    assert decision.reason == "input_mapping_error"


def test_new_structured_format_remains_compatible():
    payload = {
        "goal": "Review structured proposal",
        "proposal": {
            "draft": "Objectives, activities and KPI for a free event.",
            "objectives": ["Teach"],
            "activities": ["Workshop"],
            "kpis": ["20 participants"],
        },
        "requirements": ["Free event"],
        "evidence": [{"source_id": "NEW-1", "location": "doc:new", "claim": "Valid", "excerpt": "Structured source."}],
        "budget_summary": {"lines": [{"label": "Trainer", "amount": 500}], "total": 500},
        "age_range": "12-17",
        "participant_count": 20,
        "duration_minutes": 240,
        "same_group": True,
        "free_event": True,
        "deadline": "2026-07-24",
        "event_window": {"start": "2026-08-16", "end": "2026-10-31"},
        "organization_status": "APS",
        "known_gaps": [],
        "requested_review": ["strategy"],
    }
    normalized, report = normalize_grant_review(payload)
    assert normalized["proposal"] == payload["proposal"]
    assert normalized["draft"] == payload["proposal"]["draft"]
    assert normalized["evidence"][0]["source_id"] == "NEW-1"
    assert normalized["budget_summary"] == payload["budget_summary"]
    assert normalized["age_range"] == "12-17"
    assert report["selected_source_path"] == "$"
    assert report["mapping_error"] is False


def _other_payload(label: str) -> dict:
    return {"goal": label, "draft": f"Draft {label}", "requirements": [], "evidence": []}


def test_worker_task_id_claims_only_requested_task(tmp_path):
    adapter = RecordingAdapter()
    service = GlmReviewService(state_dir=tmp_path / "state", adapter=adapter)
    service.enqueue_payload(_other_payload("old"), task_type="other", task_id="old-canary-task", immediate=True)
    service.enqueue_payload(_other_payload("target"), task_type="other", task_id="meet-code-target", immediate=True)
    result = service.process_task("meet-code-target", force=True)
    assert result["state"] == "completed"
    assert adapter.calls == ["meet-code-target"]
    assert service.status("old-canary-task")["state"] == "queued"


def test_worker_task_id_rejects_missing_and_running_tasks(tmp_path):
    service = GlmReviewService(state_dir=tmp_path / "state", adapter=RecordingAdapter())
    assert service.process_task("missing-task", force=True)["status"] == "not_found"
    service.enqueue_payload(_other_payload("running"), task_type="other", task_id="running-task", immediate=True)
    assert service.queue.claim_task("running-task", force=True)
    result = service.process_task("running-task", force=True)
    assert result["status"] == "task_already_running"
    assert result["state"] == "running"


def test_exact_task_claim_keeps_cas_under_concurrency(tmp_path):
    queue = GlmQueue(tmp_path / "state")
    task = tmp_path / "task"
    task.mkdir()
    queue.enqueue({
        "task_id": "exact-claim-task",
        "task_type": "other",
        "state": "queued",
        "input_artifact": str(task / "input.json"),
        "packet_artifact": str(task / "packet.json"),
        "packet_hash": "a" * 64,
    })
    barrier = threading.Barrier(3)
    results: list[dict | None] = []

    def claim(name: str) -> None:
        barrier.wait()
        results.append(GlmQueue(tmp_path / "state").claim_task("exact-claim-task", force=True, worker_id=name))

    threads = [threading.Thread(target=claim, args=(f"worker-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert len([row for row in results if row]) == 1


def test_cli_exposes_worker_task_id():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    add_glm_parser(sub)
    args = parser.parse_args(["glm", "worker", "--task-id", "meet-code-target", "--force"])
    assert args.command == "glm"
    assert args.glm_action == "worker"
    assert args.task_id == "meet-code-target"
    assert args.force is True


def test_cli_dispatches_exact_task_and_rejects_missing_or_running(monkeypatch):
    class FakeService:
        response: dict = {"state": "completed"}
        seen: list[str] = []

        def process_task(self, task_id: str, *, force: bool) -> dict:
            self.seen.append(task_id)
            return dict(self.response)

    monkeypatch.setattr(glm_cli, "GlmReviewService", FakeService)
    args = argparse.Namespace(command="glm", glm_action="worker", task_id="meet-code-target", force=True, limit=1)
    assert glm_cli.run(args) == 0
    assert FakeService.seen == ["meet-code-target"]
    FakeService.response = {"status": "not_found", "task_id": "missing-task"}
    args.task_id = "missing-task"
    assert glm_cli.run(args) == 2
    FakeService.response = {"status": "task_already_running", "task_id": "running-task", "state": "running"}
    args.task_id = "running-task"
    assert glm_cli.run(args) == 2
