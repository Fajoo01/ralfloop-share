from __future__ import annotations

from types import SimpleNamespace

from ralfloop_agent.integration.bottazzi_motor_judge import JudgeCase, SYSTEM_PROMPT
from ralfloop_agent.integration.motor_prompt_budget import (
    MotorPromptBudget,
    assess_judge_prompt,
    count_rendered_tokens,
    render_ds41_judge_prompt,
)


def _case() -> JudgeCase:
    return JudgeCase(
        case_id="budget-1",
        goal="verify",
        facts=["fact=true"],
        rules=["never invent"],
        candidate_actions=["PASS", "REQUEST_REVIEW"],
        candidate_answer="PASS",
    )


def test_renderer_matches_ds41_system_user_chat_shape():
    rendered = render_ds41_judge_prompt(_case())
    assert rendered.startswith("<｜begin▁of▁sentence｜><｜System｜>" + SYSTEM_PROMPT)
    assert "<｜User｜>{" in rendered
    assert rendered.endswith("<｜Assistant｜></think>")


def test_token_counter_parses_ds4_dump(monkeypatch):
    import ralfloop_agent.integration.motor_prompt_budget as module

    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="[1,2,3,4]\n"))
    assert count_rendered_tokens("rendered", ds4_binary="ds4", model_path="model.gguf") == 4


def test_budget_assessment_marks_observed_band(monkeypatch):
    import ralfloop_agent.integration.motor_prompt_budget as module

    monkeypatch.setattr(module, "count_rendered_tokens", lambda *args, **kwargs: 193)
    result = assess_judge_prompt(
        _case(), ds4_binary="ds4", model_path="model.gguf",
        budget=MotorPromptBudget(observed_safe_tokens=192),
    )
    assert result == {
        "rendered_tokens": 193,
        "observed_safe_tokens": 192,
        "within_observed_safe_band": False,
        "over_by": 1,
    }


def test_protocol_comparison_measures_compact_system(monkeypatch):
    import ralfloop_agent.integration.motor_prompt_budget as module

    seen = []
    monkeypatch.setattr(
        module,
        "count_rendered_tokens",
        lambda rendered, **kwargs: seen.append(rendered) or (226 if len(seen) == 1 else 190),
    )
    result = module.compare_judge_protocols(
        _case(), ds4_binary="ds4", model_path="model.gguf",
        budget=MotorPromptBudget(observed_safe_tokens=192),
    )
    assert result == {
        "current_tokens": 226,
        "compact_system_tokens": 190,
        "saved_tokens": 36,
        "compact_system_within_observed_safe_band": True,
    }
    assert SYSTEM_PROMPT in seen[0]
    assert module.COMPACT_JUDGE_SYSTEM_PROMPT in seen[1]
