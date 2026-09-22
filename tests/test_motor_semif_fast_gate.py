from __future__ import annotations

from ralfloop_agent.integration.bottazzi_motor_judge import (
    JudgeCase,
    JudgeGate,
    JudgeOutcome,
    JudgeVerdict,
    judge_case_digest,
)
from ralfloop_agent.integration.motor_semif_fast_gate import (
    SemIfFastGateConfig,
    SemIfMotorCascade,
    SemIfScore,
    render_semif_prompt,
    semantic_fast_pass_allowed,
)


def _case(**updates):
    data = {
        "case_id": "case-1",
        "goal": "Judge section",
        "facts": ["APS iscritta al RUNTS."],
        "rules": ["APS ammessa."],
        "candidate_actions": ["PASS", "REQUEST_REVIEW", "REJECT"],
        "metadata": {"risk_class": "internal_readonly", "semantic_fastpath_eligible": True},
    }
    data.update(updates)
    return JudgeCase(**data)


def _fallback_outcome(case):
    return JudgeOutcome(
        case_digest=judge_case_digest(case),
        verdict=JudgeVerdict(
            decision="REQUEST_REVIEW",
            confidence=0.8,
            risk="MEDIUM",
            reason="fallback",
            missing_evidence=["review"],
            provider="motor-test",
        ),
        gate=JudgeGate(
            proceed_to_next_stage=False,
            status="review",
            requires_human_review=True,
        ),
        raw_text="{}",
    )


class _FakeScorer:
    def __init__(self, score=None, error=False, threshold=0.97):
        self.value = score
        self.error = error
        self.config = SemIfFastGateConfig(fast_pass_threshold=threshold)

    def score(self, case):
        if self.error:
            raise RuntimeError("offline")
        return self.value


class _FakeMotor:
    def __init__(self):
        self.calls = 0

    def judge(self, case):
        self.calls += 1
        return _fallback_outcome(case)


def _score(decision="PASS", pass_prob=0.99):
    probs = {
        "PASS": pass_prob,
        "REQUEST_REVIEW": (1.0 - pass_prob) / 2,
        "REJECT": (1.0 - pass_prob) / 2,
    }
    if decision != "PASS":
        probs[decision] = 0.98
        probs["PASS"] = 0.01
        other = "REJECT" if decision == "REQUEST_REVIEW" else "REQUEST_REVIEW"
        probs[other] = 0.01
    return SemIfScore(
        decision=decision,
        probabilities=probs,
        latency_ms=250.0,
        prompt_sha256="a" * 64,
    )


def test_render_prompt_matches_validated_raw_shape():
    prompt = render_semif_prompt(_case())
    assert prompt.startswith("<|im_start|>system\n")
    assert '"letter": "A"' in prompt
    assert '"letter": "B"' in prompt
    assert '"letter": "C"' in prompt
    assert prompt.endswith("<|im_start|>assistant\n")


def test_high_confidence_internal_readonly_pass_can_fast_path():
    case = _case()
    assert semantic_fast_pass_allowed(case, _score(), threshold=0.97)


def test_side_effect_never_fast_passes():
    case = _case(side_effect_intent=True)
    assert not semantic_fast_pass_allowed(case, _score(), threshold=0.97)


def test_low_probability_never_fast_passes():
    case = _case()
    assert not semantic_fast_pass_allowed(case, _score(pass_prob=0.96), threshold=0.97)


def test_nonpass_semantic_result_escalates_to_motor():
    motor = _FakeMotor()
    cascade = SemIfMotorCascade(scorer=_FakeScorer(_score("REJECT")), motor=motor)
    outcome = cascade.judge(_case())
    assert motor.calls == 1
    assert outcome.verdict.provider == "motor-test"


def test_semif_unavailable_escalates_to_motor():
    motor = _FakeMotor()
    cascade = SemIfMotorCascade(scorer=_FakeScorer(error=True), motor=motor)
    outcome = cascade.judge(_case())
    assert motor.calls == 1
    assert outcome.verdict.decision == "REQUEST_REVIEW"


def test_high_confidence_pass_skips_motor():
    motor = _FakeMotor()
    cascade = SemIfMotorCascade(scorer=_FakeScorer(_score()), motor=motor)
    outcome = cascade.judge(_case())
    assert motor.calls == 0
    assert outcome.verdict.decision == "PASS"
    assert outcome.verdict.provider == "semif_qwen35_fastgate"
    assert outcome.gate.proceed_to_next_stage
    assert not outcome.gate.execution_authorized
