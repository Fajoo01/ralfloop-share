from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from typing import Callable

from .speculative_metrics import SpeculativeMetrics, accepted_prefix


SUPPORTED_K = (1, 2, 4, 8)


@dataclass(frozen=True)
class RemoteSpeculationConfig:
    enabled: bool = False
    maximum: int = 4

    @classmethod
    def from_env(cls) -> "RemoteSpeculationConfig":
        enabled = os.getenv("RALF_REMOTE_SPECULATION_ENABLED", "0") == "1"
        maximum = int(os.getenv("RALF_REMOTE_SPECULATION_K", "4"))
        if maximum not in SUPPORTED_K:
            raise ValueError("unsupported_remote_speculation_k")
        return cls(enabled=enabled, maximum=maximum)


@dataclass(frozen=True)
class CompatibilityReport:
    compatible: bool
    reason: str
    target_tokenizer_hash: str
    draft_tokenizer_hash: str
    target_vocabulary_hash: str
    draft_vocabulary_hash: str
    target_vocabulary_size: int
    draft_vocabulary_size: int
    canary_count: int
    canary_mismatches: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def compare_tokenizers(target, draft, canaries: list[str]) -> CompatibilityReport:
    mismatches = sum(target.encode(text) != draft.encode(text) for text in canaries)
    target_id = target.identity
    draft_id = draft.identity
    compatible = (
        target_id.tokenizer_hash == draft_id.tokenizer_hash
        and target_id.vocabulary_hash == draft_id.vocabulary_hash
        and target_id.vocabulary_size == draft_id.vocabulary_size
        and mismatches == 0
    )
    reason = "compatible" if compatible else "tokenizer_incompatible"
    return CompatibilityReport(
        compatible=compatible,
        reason=reason,
        target_tokenizer_hash=target_id.tokenizer_hash,
        draft_tokenizer_hash=draft_id.tokenizer_hash,
        target_vocabulary_hash=target_id.vocabulary_hash,
        draft_vocabulary_hash=draft_id.vocabulary_hash,
        target_vocabulary_size=target_id.vocabulary_size,
        draft_vocabulary_size=draft_id.vocabulary_size,
        canary_count=len(canaries),
        canary_mismatches=mismatches,
    )


def inspect_llama_external_draft_support(help_text: str) -> dict[str, object]:
    lowered = help_text.lower()
    local = "--spec-draft-model" in lowered or "--model-draft" in lowered
    external = any(marker in lowered for marker in ("external draft token", "remote draft endpoint", "draft-token-api"))
    return {
        "local_draft_supported": local,
        "external_draft_supported": external,
        "reason": "supported" if external else "installed_llama_cpp_has_no_external_draft_api",
    }


def commit_verified_tokens(draft_tokens: list[int], target_tokens: list[int]) -> tuple[list[int], int]:
    """Commit accepted draft prefix plus exactly one target correction token."""
    accepted = accepted_prefix(draft_tokens, target_tokens)
    committed = list(draft_tokens[:accepted])
    if accepted < len(target_tokens):
        committed.append(target_tokens[accepted])
    return committed, accepted


class RemoteTokenSpeculationLab:
    """Engine-independent greedy verifier used only by the isolated laboratory."""

    def __init__(self, config: RemoteSpeculationConfig | None = None) -> None:
        self.config = config or RemoteSpeculationConfig.from_env()

    def run_round(
        self,
        *,
        context: list[int],
        draft: Callable[[list[int], int], tuple[list[int], float, int]],
        verify: Callable[[list[int], list[int]], tuple[list[int], float]],
        compatibility: CompatibilityReport,
        metrics: SpeculativeMetrics,
    ) -> dict[str, object]:
        if not self.config.enabled:
            return {"active": False, "fallback": "target_autoregressive", "reason": "disabled"}
        if not compatibility.compatible:
            return {"active": False, "fallback": "target_autoregressive", "reason": compatibility.reason}
        try:
            proposed, network_ms, network_bytes = draft(context, self.config.maximum)
        except Exception as exc:
            return {"active": False, "fallback": "target_autoregressive", "reason": f"remote_unavailable:{type(exc).__name__}"}
        try:
            target_tokens, verification_ms = verify(context, proposed)
        except Exception as exc:
            return {"active": False, "fallback": "target_autoregressive", "reason": f"target_verification_unavailable:{type(exc).__name__}"}
        committed, accepted = commit_verified_tokens(proposed, target_tokens)
        metrics.record(proposed, target_tokens, network_ms=network_ms, network_bytes=network_bytes)
        return {
            "active": True,
            "proposed": proposed,
            "committed": committed,
            "accepted": accepted,
            "verification_ms": verification_ms,
        }
