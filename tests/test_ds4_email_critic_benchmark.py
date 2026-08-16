from __future__ import annotations

import json
from pathlib import Path
import sys

from tools.benchmark_ds4_email_critic import (
    DEFAULT_CORPUS,
    load_corpus,
    metrics,
    oracle_passes,
    prepare_case,
    run_fake_reviews,
    run_repairs,
)
from ralfloop_agent.semantic_judge.core import semantic_prompt
from ralfloop_agent.semantic_judge.compact import compact_critic_context


def prepared_cases():
    return [prepare_case(item) for item in load_corpus(DEFAULT_CORPUS)]


def test_corpus_has_full_semantic_matrix_and_evidence_backed_domains():
    cases = prepared_cases()
    assert [item["case"][0] for item in cases] == list("ABCDEFGHIJ")
    assert all(item["domain"].evidence for item in cases)
    assert all(item["domain"].schema_version == "email_reply_domain_v1" for item in cases)
    assert all(item["domain"].supported_facts for item in cases)


def test_hard_guard_blocks_deterministic_cases_but_leaves_semantic_cases_for_critic():
    values = {item["case"]: item for item in prepared_cases()}
    assert values["A_safe_paraphrase"]["hard_guard_result"] == "passed"
    for prefix in "BCDEFG":
        item = next(value for key, value in values.items() if key.startswith(prefix + "_"))
        assert item["hard_guard_result"].startswith("blocked:")
    for prefix in "HIJ":
        item = next(value for key, value in values.items() if key.startswith(prefix + "_"))
        assert item["hard_guard_result"] == "passed"


def test_critic_packets_stay_near_500_tokens_and_fit_validated_context():
    lengths = {
        item["case"]: (len(semantic_prompt(item["context"], item["draft"])) + 3) // 4
        for item in prepared_cases()
    }
    assert max(value for case, value in lengths.items() if not case.startswith("I_")) <= 500
    assert lengths["I_subject_confusion"] <= 510


def test_compact_critic_packets_remove_verbose_provenance_and_preserve_semantics():
    packets = {
        item["case"]: compact_critic_context(item["context"], item["draft"])
        for item in prepared_cases()
    }
    assert max((len(value.prompt) + 3) // 4 for value in packets.values()) <= 350
    assert all("sha256" not in value.prompt and "evidence_refs" not in value.prompt for value in packets.values())
    assert any(entry.certainty == "?" for entry in packets["H_uncertainty_destroyed"].entries)
    subject_packet = packets["I_subject_confusion"]
    assert "Associazione Aurora" in subject_packet.prompt and "Associazione Boreale" in subject_packet.prompt


def test_fake_critic_and_repair_evaluate_all_cases_without_second_critic_call(tmp_path):
    cases = prepared_cases()
    rows, sweep, selected, _ = run_fake_reviews(cases, 64)
    for row, case in zip(rows, cases, strict=True):
        row["expected_verdict"] = case["expected_verdict"]
        row["acceptable_issue_types"] = case["acceptable_issue_types"]
    transition = run_repairs(cases, rows, mode="fixture", fast_chat_env=tmp_path / "unused")
    result = metrics(rows, sweep)
    assert selected == 64 and transition["engine"] == "none"
    assert result["critic_case_recall"] == 1.0
    assert result["safe_false_positive_rate"] == 0.0
    assert result["repair_success_rate"] == 1.0
    assert all(row["overall_result"] == "passed" for row in rows)


def test_oracle_rejects_original_problem_and_accepts_expected_repair():
    for case in prepared_cases():
        if case["expected_verdict"] == "pass":
            assert oracle_passes(case, case["draft"])
        else:
            assert not oracle_passes(case, case["draft"])
            assert oracle_passes(case, case["expected_repair"])


def test_fake_cli_creates_no_approval_or_outbox(monkeypatch, tmp_path, capsys):
    from tools import benchmark_ds4_email_critic as benchmark

    output = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", [
        "benchmark_ds4_email_critic.py", "--runtime", "fake", "--repair-mode", "fixture",
        "--output", str(output),
    ])
    assert benchmark.main() == 0
    capsys.readouterr()
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "completed"
    assert report["side_effect_check"]["paths_unchanged"] is True
    assert report["side_effect_check"]["isolated_approval_created"] is False
    assert report["ds4_calls_after_repair"] == 0
    assert not (tmp_path / "forbidden-side-effects").exists()
