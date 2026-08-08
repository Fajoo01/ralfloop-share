from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


PROFILE_PATH = Path(__file__).with_name("recursive_mas_domain_instruct_v1.json")
CACHE_ROOT = Path("/home/sibilla-cumana/RecursiveMAS/.hf-domain-instruct-v1/hub")
SNAPSHOTS = {
    "planner": CACHE_ROOT / "models--Qwen--Qwen2.5-3B-Instruct/snapshots/aa8e72537993ba99e69dfaafa59ed015b17504d1",
    "critic": CACHE_ROOT / "models--Qwen--Qwen2.5-1.5B-Instruct/snapshots/989aa7980e4cf806f80c7fef2b1adb7bc71aa306",
    "solver": CACHE_ROOT / "models--Qwen--Qwen2.5-3B-Instruct/snapshots/aa8e72537993ba99e69dfaafa59ed015b17504d1",
}
EXPECTED = {
    "planner": {
        "model": "Qwen/Qwen2.5-3B-Instruct",
        "revision": "aa8e72537993ba99e69dfaafa59ed015b17504d1",
        "hidden_size": 2048,
    },
    "critic": {
        "model": "Qwen/Qwen2.5-1.5B-Instruct",
        "revision": "989aa7980e4cf806f80c7fef2b1adb7bc71aa306",
        "hidden_size": 1536,
    },
    "solver": {
        "model": "Qwen/Qwen2.5-3B-Instruct",
        "revision": "aa8e72537993ba99e69dfaafa59ed015b17504d1",
        "hidden_size": 2048,
    },
}
LINK_DIMENSIONS = {
    "outer_12": (2048, 1536),
    "outer_23": (1536, 2048),
    "outer_31": (2048, 2048),
}
TOKENIZER_FILES = ("merges.txt", "tokenizer.json", "tokenizer_config.json", "vocab.json")
REQUIRED_PROFILE_FIELDS = {
    "profile_id",
    "profile_version",
    "planner_model",
    "critic_model",
    "solver_model",
    "planner_revision",
    "critic_revision",
    "solver_revision",
    "inner_adapter_checkpoints",
    "cross_model_adapter_checkpoints",
    "input_contract",
    "output_contract",
    "enabled",
}


def load_instruct_profile(path: Path = PROFILE_PATH) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if set(payload) != REQUIRED_PROFILE_FIELDS:
        raise ValueError("instruct_profile_fields_invalid")
    if payload["profile_id"] != "recursive_mas_domain_instruct_v1" or payload["profile_version"] != "v1":
        raise ValueError("instruct_profile_identity_invalid")
    if payload["enabled"] is not False:
        raise ValueError("instruct_profile_must_remain_disabled")
    if payload["inner_adapter_checkpoints"] or payload["cross_model_adapter_checkpoints"]:
        raise ValueError("unverified_instruct_adapters_must_not_be_registered")
    for role in ("planner", "critic", "solver"):
        if payload[f"{role}_model"] != EXPECTED[role]["model"]:
            raise ValueError(f"instruct_model_mismatch:{role}")
        if payload[f"{role}_revision"] != EXPECTED[role]["revision"]:
            raise ValueError(f"instruct_revision_mismatch:{role}")
    return payload


def tokenizer_sha256(snapshot: Path) -> str:
    digest = hashlib.sha256()
    for name in TOKENIZER_FILES:
        path = snapshot / name
        if not path.is_file():
            raise FileNotFoundError(path)
        digest.update(name.encode("utf-8") + b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def inspect_model_config(role: str, snapshot: Path) -> dict[str, Any]:
    if role not in EXPECTED:
        raise ValueError(f"unknown_instruct_role:{role}")
    config = json.loads((snapshot / "config.json").read_text(encoding="utf-8"))
    tokenizer = json.loads((snapshot / "tokenizer_config.json").read_text(encoding="utf-8"))
    template = str(tokenizer.get("chat_template") or "")
    return {
        "role": role,
        "path": str(snapshot.resolve()),
        "model_id": EXPECTED[role]["model"],
        "revision": EXPECTED[role]["revision"],
        "architecture": (config.get("architectures") or [None])[0],
        "hidden_size": config.get("hidden_size"),
        "num_hidden_layers": config.get("num_hidden_layers"),
        "vocab_size": config.get("vocab_size"),
        "dtype": config.get("torch_dtype"),
        "tokenizer_class": tokenizer.get("tokenizer_class"),
        "tokenizer_sha256": tokenizer_sha256(snapshot),
        "chat_template_sha256": hashlib.sha256(template.encode("utf-8")).hexdigest(),
    }


def audit_model_compatibility(snapshots: Mapping[str, Path] = SNAPSHOTS) -> dict[str, Any]:
    records = {role: inspect_model_config(role, Path(snapshots[role])) for role in EXPECTED}
    errors: list[str] = []
    for role, record in records.items():
        if record["architecture"] != "Qwen2ForCausalLM":
            errors.append(f"architecture:{role}")
        if record["hidden_size"] != EXPECTED[role]["hidden_size"]:
            errors.append(f"hidden_size:{role}")
        if record["vocab_size"] != 151936:
            errors.append(f"vocab_size:{role}")
        if record["tokenizer_class"] != "Qwen2Tokenizer":
            errors.append(f"tokenizer_class:{role}")
    tokenizer_hashes = {record["tokenizer_sha256"] for record in records.values()}
    templates = {record["chat_template_sha256"] for record in records.values()}
    if len(tokenizer_hashes) != 1:
        errors.append("tokenizer_hash_mismatch")
    if len(templates) != 1:
        errors.append("chat_template_mismatch")
    return {
        "ok": not errors,
        "errors": errors,
        "models": records,
        "inner_dimensions": {role: [item["hidden_size"], item["hidden_size"]] for role, item in EXPECTED.items()},
        "cross_model_dimensions": {name: list(value) for name, value in LINK_DIMENSIONS.items()},
    }


def evaluate_direct_text_gate(
    *,
    planner_valid: int,
    critic_valid: int,
    solver_valid: int,
    total: int = 8,
    invented_rule_ids: int = 0,
    invented_source_ids: int = 0,
) -> dict[str, Any]:
    if total != 8:
        raise ValueError("direct_text_gate_requires_eight_cases")
    planner_passed = planner_valid >= 6
    critic_passed = critic_valid >= 6
    solver_passed = solver_valid >= 7
    provenance_passed = invented_rule_ids == 0 and invented_source_ids == 0
    passed = planner_passed and critic_passed and solver_passed and provenance_passed
    return {
        "passed": passed,
        "classification": "direct_text_gate_passed" if passed else "instruct_base_insufficient",
        "planner": {"valid": planner_valid, "required": 6, "passed": planner_passed},
        "critic": {"valid": critic_valid, "required": 6, "passed": critic_passed},
        "solver": {"valid": solver_valid, "required": 7, "passed": solver_passed},
        "provenance": {
            "invented_rule_ids": invented_rule_ids,
            "invented_source_ids": invented_source_ids,
            "passed": provenance_passed,
        },
    }
