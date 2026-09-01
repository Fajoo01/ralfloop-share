from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


class LlmProfileError(ValueError):
    pass


@dataclass(frozen=True)
class LlmProfile:
    name: str
    model: str
    model_path: Path | None
    context: int
    gpu_layers: str
    threads: int
    batch_size: int
    ubatch_size: int
    cache_type_k: str
    cache_type_v: str
    cache_ram_mib: int
    cache_reuse: int
    flash_attention: str
    mmap: bool
    cpu_moe: bool
    spec_type: str
    enabled: bool = False

    def server_args(self) -> tuple[str, ...]:
        args = [
            "--ctx-size", str(self.context),
            "--gpu-layers", self.gpu_layers,
            "--threads", str(self.threads),
            "--threads-batch", str(self.threads),
            "--batch-size", str(self.batch_size),
            "--ubatch-size", str(self.ubatch_size),
            "--parallel", "1",
            "--cache-type-k", self.cache_type_k,
            "--cache-type-v", self.cache_type_v,
            "--cache-ram", str(self.cache_ram_mib),
            "--cache-reuse", str(self.cache_reuse),
            "--flash-attn", self.flash_attention,
            "--fit-target", "1024",
            "--cache-prompt", "--metrics", "--slots", "--perf",
        ]
        args.append("--mmap" if self.mmap else "--no-mmap")
        if self.cpu_moe:
            args.append("--cpu-moe")
        if self.spec_type != "none":
            args.extend(("--spec-type", self.spec_type))
        if self.spec_type == "ngram-mod":
            args.extend((
                "--spec-ngram-mod-n-match", "24",
                "--spec-ngram-mod-n-min", "48",
                "--spec-ngram-mod-n-max", "64",
            ))
        return tuple(args)


def load_llm_profile(environ: dict[str, str] | None = None) -> LlmProfile:
    env = os.environ if environ is None else environ
    name = env.get("BOTTAZZI_LLM_PROFILE", "legacy").strip().lower() or "legacy"
    model_path = env.get("BOTTAZZI_QWEN35_MODEL_PATH", "").strip()
    profiles = {
        "legacy": LlmProfile(
            "legacy", "qwen3.5:9b", None, 8192, "24", 6, 512, 512,
            "f16", "f16", 1024, 256, "auto", True, False, "none", True,
        ),
        "qwen35": LlmProfile(
            "qwen35", "qwen3.5-35b-a3b", Path(model_path) if model_path else None,
            16384, "auto", 6, 1024, 256, "q8_0", "q8_0", 4096, 0,
            "auto", True, True, "none", True,
        ),
        "qwen35_ngram": LlmProfile(
            "qwen35_ngram", "qwen3.5-35b-a3b", Path(model_path) if model_path else None,
            16384, "auto", 6, 1024, 256, "q8_0", "q8_0", 4096, 0,
            "auto", True, True, "ngram-mod", True,
        ),
        "qwen35_experimental": LlmProfile(
            "qwen35_experimental", "qwen3.5-35b-a3b", Path(model_path) if model_path else None,
            32768, "auto", 6, 1024, 256, "q8_0", "q8_0", 4096, 0,
            "auto", True, True, env.get("BOTTAZZI_QWEN35_SPEC_TYPE", "none"), True,
        ),
    }
    try:
        profile = profiles[name]
    except KeyError as exc:
        raise LlmProfileError(f"unknown_llm_profile:{name}") from exc
    if name != "legacy" and profile.model_path is None:
        raise LlmProfileError("qwen35_model_path_required")
    if "," in profile.spec_type:
        raise LlmProfileError("combined_speculation_not_validated")
    if profile.spec_type not in {
        "none", "ngram-mod", "ngram-simple", "ngram-map-k", "ngram-map-k4v",
        "ngram-cache", "draft-mtp",
    }:
        raise LlmProfileError(f"unsupported_spec_type:{profile.spec_type}")
    if profile.spec_type == "draft-mtp" and env.get("BOTTAZZI_QWEN35_HAS_MTP", "").lower() != "true":
        raise LlmProfileError("qwen35_mtp_layers_not_verified")
    return profile


__all__ = ["LlmProfile", "LlmProfileError", "load_llm_profile"]
