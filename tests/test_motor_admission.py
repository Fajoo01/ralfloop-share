from __future__ import annotations

from types import SimpleNamespace

from ralfloop_agent.integration.bottazzi_motor_judge import JudgeCase
from ralfloop_agent.integration.motor_admission import (
    make_exact_token_counter,
    plan_motor_admission,
)
from ralfloop_agent.integration.motor_prompt_budget import MotorPromptBudget
from ralfloop_agent.integration.motor_semantic_skeleton import SkeletonPolicy


def _case(*, facts: list[str] | None = None, goal: str = "verify") -> JudgeCase:
    return JudgeCase(
        case_id="admission-1",
        goal=goal,
        facts=facts or ["fact=true"],
        rules=["never invent"],
        candidate_actions=["PASS", "REQUEST_REVIEW"],
        candidate_answer="PASS",
    )


def test_raw_case_inside_budget_stays_raw():
    plan = plan_motor_admission(
        _case(), token_counter=lambda case: 180,
        budget=MotorPromptBudget(observed_safe_tokens=192),
    )
    assert plan.mode == "RAW"
    assert plan.executable_candidate is True
    assert plan.safe_tokens is None


def test_safe_candidate_can_rescue_over_budget_case(monkeypatch):
    import ralfloop_agent.integration.motor_admission as module

    case = _case(facts=["raw fact " * 100] * 4)
    safe = case.model_copy(update={"facts": ["short"] * 4})
    monkeypatch.setattr(
        module,
        "compress_judge_case_facts",
        lambda *args, **kwargs: (safe, SimpleNamespace(guard_fallbacks=1)),
    )
    plan = plan_motor_admission(
        case,
        token_counter=lambda item: 150 if item is safe else 260,
        budget=MotorPromptBudget(observed_safe_tokens=192),
    )
    assert plan.mode == "SAFE_CANDIDATE"
    assert plan.raw_tokens == 260
    assert plan.safe_tokens == 150
    assert plan.guard_fallbacks == 1


def test_over_budget_case_fails_closed_when_not_compression_eligible():
    plan = plan_motor_admission(
        _case(goal="x" * 5000),
        token_counter=lambda case: 260,
        budget=MotorPromptBudget(observed_safe_tokens=192),
        policy=SkeletonPolicy(min_chars=1000, min_segments=4),
    )
    assert plan.mode == "REVIEW"
    assert plan.reason == "over_budget_not_compression_eligible"
    assert plan.executable_candidate is False


def test_safe_candidate_still_over_budget_requires_review(monkeypatch):
    import ralfloop_agent.integration.motor_admission as module

    case = _case(facts=["raw fact " * 100] * 4)
    safe = case.model_copy(update={"facts": ["shorter fact " * 20] * 4})
    monkeypatch.setattr(
        module,
        "compress_judge_case_facts",
        lambda *args, **kwargs: (safe, SimpleNamespace(guard_fallbacks=0)),
    )
    plan = plan_motor_admission(
        case,
        token_counter=lambda item: 220 if item is safe else 280,
        budget=MotorPromptBudget(observed_safe_tokens=192),
    )
    assert plan.mode == "REVIEW"
    assert plan.reason == "safe_candidate_still_over_budget"
    assert plan.safe_tokens == 220


def test_exact_counter_uses_rendered_ds41_prompt(monkeypatch):
    import ralfloop_agent.integration.motor_admission as module

    captured = {}
    monkeypatch.setattr(module, "render_ds41_judge_prompt", lambda case: "RENDERED")

    def fake_count(rendered, *, ds4_binary, model_path):
        captured.update(rendered=rendered, binary=str(ds4_binary), model=str(model_path))
        return 177

    monkeypatch.setattr(module, "count_rendered_tokens", fake_count)
    counter = make_exact_token_counter(ds4_binary="/tmp/ds4", model_path="/tmp/model.gguf")
    assert counter(_case()) == 177
    assert captured == {
        "rendered": "RENDERED",
        "binary": "/tmp/ds4",
        "model": "/tmp/model.gguf",
    }
