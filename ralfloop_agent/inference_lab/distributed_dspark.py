from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DSparkContract:
    architecture: str
    target_family: str
    draft_family: str
    target_hidden_size: int
    target_layer_ids: tuple[int, ...]
    draft_layers: int
    block_size: int
    vocabulary_size: int
    checkpoint_bytes: int
    input_contract: str
    output_contract: str
    cache_contract: str
    dtype: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


QWEN3_8B_DSPARK_CONTRACT = DSparkContract(
    architecture="case_c_activation_transfer_then_case_b_logits",
    target_family="Qwen3-8B",
    draft_family="Qwen3DSparkModel",
    target_hidden_size=4096,
    target_layer_ids=(1, 9, 17, 25, 33),
    draft_layers=5,
    block_size=7,
    vocabulary_size=151_936,
    checkpoint_bytes=4_742_170_330,
    input_contract="target_hidden_states[B,S,5*4096]+draft_input_ids[B,7]+position_ids+draft_DynamicCache",
    output_contract="draft_logits[B,7,151936],sampled_token_ids,draft_probabilities",
    cache_contract="target_DynamicCache verifies K+1; draft_DynamicCache cropped at accepted position",
    dtype="bfloat16",
)


def activation_payload_bytes(
    *,
    batch: int,
    sequence_length: int,
    hidden_size: int,
    layer_count: int = 1,
    bytes_per_element: int = 2,
) -> int:
    values = (batch, sequence_length, hidden_size, layer_count, bytes_per_element)
    if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in values):
        raise ValueError("invalid_activation_shape")
    return batch * sequence_length * hidden_size * layer_count * bytes_per_element


def logits_payload_bytes(*, batch: int, proposal_length: int, vocabulary_size: int, bytes_per_element: int = 2) -> int:
    return activation_payload_bytes(
        batch=batch,
        sequence_length=proposal_length,
        hidden_size=vocabulary_size,
        bytes_per_element=bytes_per_element,
    )


def transfer_time_ms(payload_bytes: int, throughput_mbit_s: float, *, round_trips: int = 1, rtt_ms: float = 0.0) -> float:
    if payload_bytes < 0 or throughput_mbit_s <= 0 or round_trips < 1 or rtt_ms < 0:
        raise ValueError("invalid_network_parameters")
    return (payload_bytes * 8 / (throughput_mbit_s * 1_000_000) * 1000) + round_trips * rtt_ms


@dataclass(frozen=True)
class DSparkFeasibility:
    feasible: bool
    status: str
    reason: str
    activation_prefill_bytes: int
    activation_per_generated_token_bytes: int
    logits_per_proposal_bytes: int
    theoretical_prefill_transfer_ms: float
    theoretical_round_transfer_ms: float
    checkpoint_bytes: int
    available_ram_bytes: int
    target_compatible: bool
    cuda_available: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def assess_dspark_feasibility(
    *,
    sequence_length: int,
    available_ram_bytes: int,
    throughput_mbit_s: float,
    rtt_ms: float,
    target_family: str,
    cuda_available: bool,
    contract: DSparkContract = QWEN3_8B_DSPARK_CONTRACT,
) -> DSparkFeasibility:
    prefill = activation_payload_bytes(
        batch=1,
        sequence_length=sequence_length,
        hidden_size=contract.target_hidden_size,
        layer_count=len(contract.target_layer_ids),
        bytes_per_element=2,
    )
    per_token = activation_payload_bytes(
        batch=1,
        sequence_length=1,
        hidden_size=contract.target_hidden_size,
        layer_count=len(contract.target_layer_ids),
        bytes_per_element=2,
    )
    logits = logits_payload_bytes(
        batch=1,
        proposal_length=contract.block_size,
        vocabulary_size=contract.vocabulary_size,
        bytes_per_element=2,
    )
    target_compatible = target_family == contract.target_family
    memory_margin = 2 * 1024**3
    memory_fits = contract.checkpoint_bytes + memory_margin <= available_ram_bytes
    feasible = target_compatible and memory_fits and cuda_available
    reasons = []
    if not target_compatible:
        reasons.append("qwen3_dspark_incompatible_with_qwen2_5_target")
    if not memory_fits:
        reasons.append("checkpoint_plus_runtime_exceeds_safe_ram")
    if not cuda_available:
        reasons.append("temistocle_has_no_cuda")
    return DSparkFeasibility(
        feasible=feasible,
        status="feasible" if feasible else "distributed_dspark_not_feasible_on_temistocle",
        reason=",".join(reasons) if reasons else "feasible",
        activation_prefill_bytes=prefill,
        activation_per_generated_token_bytes=per_token,
        logits_per_proposal_bytes=logits,
        theoretical_prefill_transfer_ms=transfer_time_ms(prefill, throughput_mbit_s, rtt_ms=rtt_ms),
        theoretical_round_transfer_ms=transfer_time_ms(per_token + logits, throughput_mbit_s, rtt_ms=rtt_ms),
        checkpoint_bytes=contract.checkpoint_bytes,
        available_ram_bytes=available_ram_bytes,
        target_compatible=target_compatible,
        cuda_available=cuda_available,
    )


def distributed_dspark_enabled() -> bool:
    return os.getenv("RALF_DSPARK_DISTRIBUTED_ENABLED", "0") == "1"


def audit_checkpoint(path: str | Path, expected_bytes: int = QWEN3_8B_DSPARK_CONTRACT.checkpoint_bytes) -> dict[str, Any]:
    selected = Path(path)
    if not selected.is_file():
        return {"ok": False, "reason": "checkpoint_missing", "path": str(selected)}
    size = selected.stat().st_size
    return {"ok": size == expected_bytes, "size": size, "expected_size": expected_bytes, "path": str(selected)}
