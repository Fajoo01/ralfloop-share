from __future__ import annotations

import json
from pathlib import Path

import pytest

from ralfloop_agent.domains.email_reply import (
    build_email_reply_domain,
    validate_draft_against_domain,
)
from ralfloop_agent.semantic_judge import (
    EmailRiskAssessment,
    JudgeAvailabilityError,
    SemanticIssue,
    SemanticReview,
    SemanticReviewResult,
    ShadowModeDisabled,
    assess_email_risk,
    run_shadow_email_case,
)


CORPUS = Path(__file__).parent / "fixtures" / "email_semantic_shadow_corpus_v1.json"


class FakeJudge:
    provider = "deepseek_v4_flash"
    model = "ds4-fake"

    def __init__(self, verdict: str = "pass", *, error: Exception | None = None) -> None:
        self.verdict = verdict
        self.error = error
        self.calls: list[tuple[object, str]] = []

    def review(self, packet, draft):
        self.calls.append((packet, draft))
        if self.error:
            raise self.error
        issues = []
        if self.verdict == "repair":
            issues = [SemanticIssue(
                type="overstated_evidence",
                severity="high",
                draft_text="inizierà a settembre",
                reason="Uncertainty from evidence was removed.",
                domain_refs=["source_email.body"],
            )]
        return SemanticReviewResult(
            SemanticReview(verdict=self.verdict, issues=issues),
            self.provider,
            self.model,
            23,
            input_tokens=111,
            output_tokens=42,
        )


def cases():
    return json.loads(CORPUS.read_text(encoding="utf-8"))["cases"]


def case(case_id: str):
    return next(item for item in cases() if item["case_id"] == case_id)


def test_risk_router_matches_twenty_case_realistic_matrix():
    observed = {"low": 0, "normal": 0, "high": 0}
    for item in cases():
        assessment = assess_email_risk(item["context"], item["draft"])
        assert assessment.level == item["expected_risk"], item["case_id"]
        assert assessment.reasons
        observed[assessment.level] += 1
    assert observed == {"low": 8, "normal": 8, "high": 4}


def test_h_i_j_like_cases_route_high_for_explainable_structured_reasons():
    h = assess_email_risk(case("H_uncertainty_destroyed")["context"], case("H_uncertainty_destroyed")["draft"])
    i = assess_email_risk(case("I_subject_confusion")["context"], case("I_subject_confusion")["draft"])
    j = assess_email_risk(case("J_unsupported_result_notice")["context"], case("J_unsupported_result_notice")["draft"])
    assert h.level == "high" and "temporal_uncertainty" in h.reasons
    assert i.level == "high" and "multiple_attributed_subjects" in i.reasons
    assert j.level == "high" and "expectation_creating_language" in j.reasons


def test_structured_override_is_explicit_and_authoritative():
    item = case("HG_amount_guard_block")
    packet = dict(item["context"])
    packet["reply_constraints"] = {**packet["reply_constraints"], "semantic_risk_override": "normal"}
    assessment = assess_email_risk(packet, item["draft"])
    assert assessment == EmailRiskAssessment(level="normal", reasons=["explicit_override:normal"])
    with pytest.raises(ValueError, match="semantic_risk_override_invalid"):
        assess_email_risk(packet, item["draft"], classification="critical")


def test_shadow_disabled_is_explicit(tmp_path):
    item = case("L01_attachment_received")
    with pytest.raises(ShadowModeDisabled):
        run_shadow_email_case(
            case_id=item["case_id"], context_packet=item["context"], draft=item["draft"],
            artifact_root=tmp_path, shadow_enabled=False,
        )


def test_low_shadow_skips_ds4_and_writes_only_redacted_artifact(tmp_path):
    item = case("L01_attachment_received")
    packet = dict(item["context"])
    packet["source_email"] = {**packet["source_email"], "sender": "Mario <mario@example.org>"}
    judge = FakeJudge()
    result = run_shadow_email_case(
        case_id=item["case_id"], context_packet=packet, draft=item["draft"],
        artifact_root=tmp_path, semantic_judge=judge, shadow_enabled=True,
    )
    assert result["risk"]["level"] == "low"
    assert result["ds4_invoked"] is False and result["ds4_skipped_reason"] == "risk_low"
    assert result["final_validator"] == "passed" and judge.calls == []
    assert result["would_reach_approval"] is False
    artifact = Path(result["artifact_path"]).read_text(encoding="utf-8")
    assert "mario@example.org" not in artifact
    assert json.loads(artifact)["approval_candidate"] == {"created": False, "eligible": True}


def test_high_guard_block_never_calls_ds4_and_records_exact_reason(tmp_path):
    item = case("HG_amount_guard_block")
    judge = FakeJudge()
    result = run_shadow_email_case(
        case_id=item["case_id"], context_packet=item["context"], draft=item["draft"],
        artifact_root=tmp_path, semantic_judge=judge, shadow_enabled=True,
    )
    assert result["risk"]["level"] == "high"
    assert result["hard_guard"] == "draft_domain_unsupported_amount"
    assert result["ds4_invoked"] is False
    assert result["ds4_skipped_reason"] == "hard_guard_block"
    assert judge.calls == [] and result["overall_result"] == "blocked"


def test_high_shadow_calls_ds4_once_repairs_copy_once_and_final_validates(tmp_path):
    item = case("H_uncertainty_destroyed")
    judge = FakeJudge("repair")
    repairs = []

    def repair(packet, draft, issues):
        repairs.append((packet, draft, issues))
        return item["expected_repair"]

    result = run_shadow_email_case(
        case_id=item["case_id"], context_packet=item["context"], draft=item["draft"],
        artifact_root=tmp_path, semantic_judge=judge, repair_callback=repair,
        shadow_enabled=True,
    )
    assert len(judge.calls) == 1 and len(repairs) == 1
    assert result["ds4_verdict"] == "repair"
    assert result["qwen_repair_attempted"] is True and result["qwen_repair_success"] is True
    assert result["draft_original"] == item["draft"]
    assert result["draft_repaired"] == item["expected_repair"]
    assert result["final_validator"] == "passed"
    assert result["would_reach_approval"] is False


@pytest.mark.parametrize(
    "error,verdict",
    [
        (JudgeAvailabilityError("deepseek_v4_flash_gpu_unavailable"), "unavailable"),
        (TimeoutError("semantic_judge_timeout"), "timeout"),
        (ValueError("semantic_review_malformed_json"), "invalid_output"),
        (RuntimeError("unexpected_runtime_failure"), "error"),
    ],
)
def test_shadow_ds4_failure_is_fail_closed_without_repair(tmp_path, error, verdict):
    item = case("H_uncertainty_destroyed")
    judge = FakeJudge(error=error)
    repairs = []
    result = run_shadow_email_case(
        case_id=item["case_id"], context_packet=item["context"], draft=item["draft"],
        artifact_root=tmp_path, semantic_judge=judge,
        repair_callback=lambda *args: repairs.append(args), shadow_enabled=True,
    )
    assert len(judge.calls) == 1 and repairs == []
    assert result["ds4_verdict"] == verdict
    assert result["final_validator"] == "not_run"
    assert result["overall_result"] == "blocked"
    assert result["would_reach_approval"] is False


def test_corpus_hard_guard_blocks_only_explicit_guard_case():
    blocked = []
    for item in cases():
        domain = build_email_reply_domain(item["context"])
        try:
            validate_draft_against_domain(item["draft"], domain)
        except Exception as exc:
            blocked.append((item["case_id"], str(getattr(exc, "reason_code", exc))))
    assert blocked == [("HG_amount_guard_block", "draft_domain_unsupported_amount")]
