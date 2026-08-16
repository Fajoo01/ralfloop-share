from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
from pathlib import Path

import tools.run_email_semantic_shadow as shadow_tool


ROOT = Path(__file__).parents[1]


def test_fake_shadow_canary_routes_twenty_cases_without_side_effects(tmp_path, monkeypatch):
    output = tmp_path / "shadow"
    monkeypatch.setenv("RALFLOOP_SEMANTIC_JUDGE_SHADOW", "1")
    report = shadow_tool.run(argparse.Namespace(
        corpus=ROOT / "tests/fixtures/email_semantic_shadow_corpus_v1.json",
        output=output,
        runtime="fake",
        repair="fixture",
        fast_chat_env=Path("/nonexistent"),
        replay_report=None,
    ))
    assert report["metrics"] == {
        "total_cases": 20,
        "low": 8,
        "normal": 8,
        "high": 4,
        "hard_guard_blocked": 1,
        "ds4_invoked": 3,
        "ds4_skipped": 17,
        "ds4_pass": 0,
        "ds4_repair": 3,
        "final_validator_pass": 19,
        "final_validator_block": 1,
        "qwen_repair_attempted": 3,
        "qwen_repair_success": 3,
        "ds4_p50_ms": None,
        "ds4_p95_ms": None,
        "workload_total_ms": report["metrics"]["workload_total_ms"],
        "workload_average_ms": report["metrics"]["workload_average_ms"],
        "ds4_invocation_rate_percent": 15.0,
    }
    by_id = {item["case_id"]: item for item in report["cases"]}
    assert all(by_id[case_id]["ds4_invoked"] for case_id in (
        "H_uncertainty_destroyed", "I_subject_confusion", "J_unsupported_result_notice",
    ))
    assert by_id["HG_amount_guard_block"]["ds4_skipped_reason"] == "hard_guard_block"
    assert report["runtime_metadata"]["external_restored"] is True
    assert report["side_effect_verification"] == {
        "approval_records_created": 0,
        "outbox_records_created": 0,
        "telegram_send_calls": 0,
        "gmail_send_calls": 0,
        "conversation_state_mutations": 0,
    }
    assert len(list((output / "cases").glob("*.json"))) == 20
    assert not (tmp_path / "approval.sqlite3").exists()
    assert not (tmp_path / "outbox.jsonl").exists()
    assert json.loads((output / "report.json").read_text(encoding="utf-8"))["mode"] == "shadow"


def test_replay_uses_saved_ds4_once_and_qwen_under_scheduler(tmp_path, monkeypatch):
    monkeypatch.setenv("RALFLOOP_SEMANTIC_JUDGE_SHADOW", "1")
    source = shadow_tool.run(argparse.Namespace(
        corpus=ROOT / "tests/fixtures/email_semantic_shadow_corpus_v1.json",
        output=tmp_path / "source",
        runtime="fake",
        repair="fixture",
        fast_chat_env=Path("/nonexistent"),
        replay_report=None,
    ))
    corpus = json.loads((ROOT / "tests/fixtures/email_semantic_shadow_corpus_v1.json").read_text())
    repairs = {item["draft"]: item.get("expected_repair", item["draft"]) for item in corpus["cases"]}
    transitions = []

    class Scheduler:
        @contextmanager
        def engine_session(self, engine, *, task_id=""):
            transition = {"engine": engine, "external_stopped": True, "external_restored": False}
            transitions.append(transition)
            try:
                yield transition
            finally:
                transition["external_restored"] = True

    class Generator:
        def generate_repair(self, packet, rejected, reason, *, semantic_issues=None):
            return type("Draft", (), {
                "text": repairs[rejected],
                "fallback_used": False,
            })()

    monkeypatch.setattr(shadow_tool, "TransactionalGpuScheduler", Scheduler)
    monkeypatch.setattr(shadow_tool, "RalfReplyGenerator", Generator)
    monkeypatch.setattr(shadow_tool, "load_env", lambda path: None)
    replay = shadow_tool.run(argparse.Namespace(
        corpus=ROOT / "tests/fixtures/email_semantic_shadow_corpus_v1.json",
        output=tmp_path / "replay",
        runtime="replay",
        repair="qwen",
        fast_chat_env=Path("/nonexistent"),
        replay_report=tmp_path / "source/report.json",
    ))
    assert source["metrics"]["ds4_invoked"] == replay["metrics"]["ds4_invoked"] == 3
    assert replay["runtime_metadata"]["critic_replayed"] is True
    assert replay["metrics"]["qwen_repair_success"] == 3
    assert transitions == [{
        "engine": "qwen_chat", "external_stopped": True, "external_restored": True,
    }]
