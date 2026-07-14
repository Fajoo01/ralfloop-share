import pytest

from ralfloop_agent.inference_lab.distributed_dspark import (
    QWEN3_8B_DSPARK_CONTRACT,
    activation_payload_bytes,
    assess_dspark_feasibility,
    logits_payload_bytes,
)


def test_dspark_contract_is_case_c_and_logits():
    contract = QWEN3_8B_DSPARK_CONTRACT
    assert contract.architecture == "case_c_activation_transfer_then_case_b_logits"
    assert "target_hidden_states" in contract.input_contract
    assert "draft_logits" in contract.output_contract
    assert contract.target_layer_ids == (1, 9, 17, 25, 33)


def test_activation_payload_formula():
    assert activation_payload_bytes(batch=1, sequence_length=4096, hidden_size=4096, layer_count=5, bytes_per_element=2) == 167_772_160
    assert logits_payload_bytes(batch=1, proposal_length=7, vocabulary_size=151_936, bytes_per_element=2) == 2_127_104
    with pytest.raises(ValueError):
        activation_payload_bytes(batch=0, sequence_length=1, hidden_size=1)


def test_temistocle_not_feasible():
    result = assess_dspark_feasibility(
        sequence_length=4096,
        available_ram_bytes=int(7.6 * 1024**3),
        throughput_mbit_s=105.0,
        rtt_ms=15.0,
        target_family="Qwen2.5-7B",
        cuda_available=False,
    )
    assert result.feasible is False
    assert result.status == "distributed_dspark_not_feasible_on_temistocle"
    assert result.target_compatible is False
