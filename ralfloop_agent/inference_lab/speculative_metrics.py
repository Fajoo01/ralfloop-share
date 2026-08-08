from __future__ import annotations

from dataclasses import dataclass


class TokenizerMismatch(ValueError):
    pass


def require_compatible_tokenizer(
    *,
    target_tokenizer_hash: str,
    draft_tokenizer_hash: str,
    target_vocabulary_hash: str,
    draft_vocabulary_hash: str,
) -> None:
    if target_tokenizer_hash != draft_tokenizer_hash:
        raise TokenizerMismatch("tokenizer_mismatch")
    if target_vocabulary_hash != draft_vocabulary_hash:
        raise TokenizerMismatch("vocabulary_mismatch")


def accepted_prefix(draft: list[int], target: list[int]) -> int:
    accepted = 0
    for draft_token, target_token in zip(draft, target):
        if draft_token != target_token:
            break
        accepted += 1
    return accepted


@dataclass
class SpeculativeMetrics:
    rounds: int = 0
    drafted: int = 0
    accepted: int = 0
    network_bytes: int = 0
    network_ms: float = 0.0

    def record(self, draft: list[int], target: list[int], *, network_bytes: int = 0, network_ms: float = 0.0) -> int:
        accepted = accepted_prefix(draft, target)
        self.rounds += 1
        self.drafted += len(draft)
        self.accepted += accepted
        self.network_bytes += max(0, network_bytes)
        self.network_ms += max(0.0, network_ms)
        return accepted

    @property
    def acceptance_rate(self) -> float:
        return self.accepted / self.drafted if self.drafted else 0.0

    def to_dict(self) -> dict[str, float | int]:
        return {
            "rounds": self.rounds,
            "drafted": self.drafted,
            "accepted": self.accepted,
            "acceptance_rate": self.acceptance_rate,
            "network_bytes": self.network_bytes,
            "network_ms": self.network_ms,
        }
