from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path
from typing import Any


REPO_ROOT = Path("/home/sibilla-cumana/ralfloop_agent_scaffold")
HF_ROOT = Path("/home/sibilla-cumana/RecursiveMAS/.hf-recursivemas/hub")
DOMAIN_TRAINING_ROOT = REPO_ROOT / ".ralf_run/recursive_domain_reasoning_training"

SUPPORTED_DOMAIN_REASON_CODES = (
    "qualitative_judgment",
    "strategic_assessment",
    "recommendation_required",
    "conflicting_sources",
    "incomplete_rules",
    "evidence_synthesis",
    "domain_validation",
    "domain_creation_review",
)


@dataclass(frozen=True)
class RecursiveMASProfile:
    profile_id: str
    profile_version: str
    planner_checkpoint: str
    critic_checkpoint: str
    solver_checkpoint: str
    inner_adapter_checkpoints: dict[str, str]
    outer_adapter_checkpoints: dict[str, str]
    input_contract: str
    output_contract: str
    supported_reason_codes: tuple[str, ...]
    prompt_family: str
    native_latent: bool = True
    enabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["supported_reason_codes"] = list(self.supported_reason_codes)
        return payload


MATH_SNAPSHOTS = {
    "planner": HF_ROOT / "models--RecursiveMAS--Sequential-Light-Planner-Qwen3-1.7B/snapshots/98ba7ec8230e1318ac4de9bfb5d5e85f341b0307",
    "critic": HF_ROOT / "models--RecursiveMAS--Sequential-Light-Critic-Llama3.2-1B/snapshots/b24d06be9de449803f23f712c86646d27444036c",
    "solver": HF_ROOT / "models--RecursiveMAS--Sequential-Light-Solver-Qwen2.5-Math-1.5B/snapshots/fa89c80170896e5be0da362fb2d87153b7d58cbc",
    "outer": HF_ROOT / "models--RecursiveMAS--Sequential-Light-Outerlinks/snapshots/12420b91249efe1d05cf80b72de7d8007aa85b00",
}
MATH_ADAPTER_HASHES = {
    "planner": "025560b16fa170403e80163499b54481988a7f32590e3eb27e9b98b3e85ba0f9",
    "critic": "4988907ff79262e4843940f358544a6079c54d0016f680dbb9dc9cf15a870f49",
    "solver": "d726670688eb335b6eddc7f1a5b6e80af9095fa6c1765735795ac1c5d3bfbd26",
    "outer_12": "9de78dcf1af909e77d0388e576ba0b8720bdf1c38f7384abbbb705608b9a30be",
    "outer_23": "6f7b975e2deed2000891d709cfb83b0c4946344fcd962fb0f3a10e9b4f878df5",
    "outer_31": "4a16e3e2a6d9a9f8b40d8a4596b8d97530c24db4e47e4ab5eedb9f593a78e19b",
}

MATH_PROFILE = RecursiveMASProfile(
    profile_id="recursive_mas_math",
    profile_version="upstream-38f7da45",
    planner_checkpoint=str(MATH_SNAPSHOTS["planner"] / "model.safetensors"),
    critic_checkpoint=str(MATH_SNAPSHOTS["critic"] / "model.safetensors"),
    solver_checkpoint=str(MATH_SNAPSHOTS["solver"] / "model.safetensors"),
    inner_adapter_checkpoints={
        "planner": str(MATH_SNAPSHOTS["planner"] / "adapter(math).pt"),
        "critic": str(MATH_SNAPSHOTS["critic"] / "adapter(math).pt"),
        "solver": str(MATH_SNAPSHOTS["solver"] / "adapter(math).pt"),
    },
    outer_adapter_checkpoints={
        "outer_12": str(MATH_SNAPSHOTS["outer"] / "Planner-Critic-Outerlink(math).pt"),
        "outer_23": str(MATH_SNAPSHOTS["outer"] / "Critic-Solver-Outerlink(math).pt"),
        "outer_31": str(MATH_SNAPSHOTS["outer"] / "Solver-Planner-Outerlink(math).pt"),
    },
    input_contract="math_question_v1",
    output_contract="boxed_math_answer_v1",
    supported_reason_codes=(),
    prompt_family="upstream_math",
)

DOMAIN_PROFILE = RecursiveMASProfile(
    profile_id="recursive_mas_domain_reasoning",
    profile_version="v1",
    planner_checkpoint=MATH_PROFILE.planner_checkpoint,
    critic_checkpoint=MATH_PROFILE.critic_checkpoint,
    solver_checkpoint=MATH_PROFILE.solver_checkpoint,
    inner_adapter_checkpoints={
        role: str(DOMAIN_TRAINING_ROOT / f"final/{role}/adapter.pt")
        for role in ("planner", "critic", "solver")
    },
    outer_adapter_checkpoints={
        name: str(DOMAIN_TRAINING_ROOT / f"final/outer/{name}.pt")
        for name in ("outer_12", "outer_23", "outer_31")
    },
    input_contract="domain_opinion_v1",
    output_contract="domain_opinion_v1",
    supported_reason_codes=SUPPORTED_DOMAIN_REASON_CODES,
    prompt_family="domain_reasoning_v1",
)

PROFILES = {
    MATH_PROFILE.profile_id: MATH_PROFILE,
    DOMAIN_PROFILE.profile_id: DOMAIN_PROFILE,
}


def get_profile(profile_id: str) -> RecursiveMASProfile:
    try:
        return PROFILES[profile_id]
    except KeyError as exc:
        raise ValueError(f"unknown_recursive_mas_profile:{profile_id}") from exc


def checkpoint_status(profile_id: str) -> dict[str, Any]:
    profile = get_profile(profile_id)
    files = {**profile.inner_adapter_checkpoints, **profile.outer_adapter_checkpoints}
    missing = sorted(name for name, value in files.items() if not Path(value).is_file())
    return {
        "profile_id": profile_id,
        "ready": not missing,
        "missing": missing,
        "enabled": profile.enabled,
    }


def verify_math_checkpoint_hashes() -> dict[str, Any]:
    files = {**MATH_PROFILE.inner_adapter_checkpoints, **MATH_PROFILE.outer_adapter_checkpoints}
    observed: dict[str, str | None] = {}
    for name, value in files.items():
        path = Path(value)
        observed[name] = _sha256(path) if path.is_file() else None
    mismatches = sorted(name for name, expected in MATH_ADAPTER_HASHES.items() if observed.get(name) != expected)
    return {"ok": not mismatches, "observed": observed, "expected": MATH_ADAPTER_HASHES, "mismatches": mismatches}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
