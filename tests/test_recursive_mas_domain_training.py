from pathlib import Path

import pytest

torch = pytest.importorskip("torch")

from ralfloop_agent.domains.recursive_mas_domain_training import (
    DomainTrainingConfig,
    _chat_ids,
    adapter_objective,
    load_component,
    save_component,
    train_component,
)


def test_training_config_is_bounded_and_stage_count_under_limit():
    config = DomainTrainingConfig()
    config.validate()
    assert config.pilot_total_steps == 150
    assert config.pilot_total_steps <= config.max_pilot_steps
    assert config.gradient_accumulation == 4


def test_one_step_training_and_checkpoint_roundtrip(tmp_path):
    torch.manual_seed(42)
    module = torch.nn.Linear(4, 4)
    source = torch.randn(1, 3, 4)
    target = source * 0.5
    result = train_component(module, [(source, target)], steps=1, learning_rate=1e-2, gradient_accumulation=1, device="cpu")
    assert result["steps"] == 1
    path = tmp_path / "adapter.pt"
    digest = save_component(module, path, {"step": 1})
    restored = load_component(torch.nn.Linear(4, 4), path)
    assert len(digest) == 64
    for left, right in zip(module.parameters(), restored.parameters()):
        assert torch.equal(left, right)


def test_ten_step_overfit_reduces_loss():
    torch.manual_seed(7)
    module = torch.nn.Linear(3, 3)
    source = torch.randn(1, 5, 3)
    target = torch.zeros_like(source)
    result = train_component(module, [(source, target)], steps=10, learning_rate=5e-2, gradient_accumulation=1, device="cpu")
    assert result["steps"] == 10
    assert result["loss_decreased"] is True
    assert result["loss_final"] < result["loss_initial"]


def test_objective_is_finite_and_zero_for_identical_nonzero_vectors():
    value = torch.ones(1, 2, 3)
    loss = adapter_objective(value, value)
    assert torch.isfinite(loss)
    assert float(loss) == pytest.approx(0.0, abs=1e-6)


def test_invalid_training_budget_rejected():
    config = DomainTrainingConfig(pilot_steps_per_component=40)
    with pytest.raises(ValueError, match="pilot_step_limit_exceeded"):
        config.validate()


def test_chat_ids_accepts_transformers_mapping_result():
    class Tokenizer:
        def apply_chat_template(self, *_args, **_kwargs):
            return {"input_ids": [[1, 2, 3]]}

    assert _chat_ids(Tokenizer(), "prompt", None) == [1, 2, 3]
