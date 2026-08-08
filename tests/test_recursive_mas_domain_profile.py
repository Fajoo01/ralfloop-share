from pathlib import Path

import pytest

from ralfloop_agent.domains.recursive_mas_domain_prompts import (
    FEEDBACK_SLOT,
    PLANNER_SLOT,
    REFINED_SLOT,
    build_domain_critic_prompt_with_slot,
    build_domain_planner_prompt,
    build_domain_planner_prompt_with_feedback_slot,
    build_domain_solver_prompt_with_slots,
)
from ralfloop_agent.domains.recursive_mas_profiles import (
    DOMAIN_PROFILE,
    MATH_ADAPTER_HASHES,
    MATH_PROFILE,
    get_profile,
)


def test_math_profile_keeps_exact_upstream_checkpoints():
    assert MATH_PROFILE.profile_id == "recursive_mas_math"
    assert MATH_PROFILE.prompt_family == "upstream_math"
    assert MATH_PROFILE.input_contract == "math_question_v1"
    assert MATH_PROFILE.outer_adapter_checkpoints["outer_12"].endswith("Planner-Critic-Outerlink(math).pt")
    assert MATH_ADAPTER_HASHES["planner"] == "025560b16fa170403e80163499b54481988a7f32590e3eb27e9b98b3e85ba0f9"


def test_domain_profile_is_separate_and_disabled():
    assert DOMAIN_PROFILE.profile_id == "recursive_mas_domain_reasoning"
    assert DOMAIN_PROFILE.native_latent is True
    assert DOMAIN_PROFILE.enabled is False
    assert DOMAIN_PROFILE.input_contract == DOMAIN_PROFILE.output_contract == "domain_opinion_v1"
    assert set(DOMAIN_PROFILE.inner_adapter_checkpoints).isdisjoint(set())
    assert DOMAIN_PROFILE.inner_adapter_checkpoints != MATH_PROFILE.inner_adapter_checkpoints
    assert all("recursive_domain_reasoning_training" in value for value in DOMAIN_PROFILE.inner_adapter_checkpoints.values())
    assert get_profile(DOMAIN_PROFILE.profile_id) is DOMAIN_PROFILE


def test_unknown_profile_rejected():
    with pytest.raises(ValueError, match="unknown_recursive_mas_profile"):
        get_profile("missing")


def test_domain_prompts_keep_native_slots_and_domain_roles():
    question = '{"domain_id":"synthetic"}'
    planner = build_domain_planner_prompt(question)
    feedback = build_domain_planner_prompt_with_feedback_slot(question)
    critic = build_domain_critic_prompt_with_slot(question)
    solver = build_domain_solver_prompt_with_slots(question)
    assert "at least two interpretations" in planner
    assert FEEDBACK_SLOT in feedback
    assert PLANNER_SLOT in critic
    assert "strongest counterargument" in critic
    assert REFINED_SLOT in solver
    assert "domain_opinion_v1" in solver
    assert "\\boxed" not in "\n".join((planner, feedback, critic, solver))


def test_domain_solver_rejects_non_chain_shape():
    with pytest.raises(ValueError, match="unsupported_mas_shape"):
        build_domain_solver_prompt_with_slots("{}", mas_shape="mesh")
