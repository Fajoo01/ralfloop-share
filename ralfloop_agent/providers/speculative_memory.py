from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import json
import struct
import time
from typing import Iterable, Mapping, Sequence


class SpeculativeMemoryError(ValueError):
    pass


@dataclass(frozen=True)
class RetrievalDecision:
    documents: tuple[str, ...] = ()
    ngram_pools: tuple[str, ...] = ()


@dataclass(frozen=True)
class PromptStateDecision:
    exact_prefix_hash: str
    checkpoint_hit: bool
    checkpoint_id: str | None


@dataclass(frozen=True)
class PoolRequest:
    repository: str | None = None
    workspace: str | None = None
    tool: str | None = None
    host: str | None = None
    service: str | None = None
    task_type: str | None = None
    session_id: str | None = None
    exact_session_match: bool = False


@dataclass(frozen=True)
class PoolSelection:
    pool_id: str | None
    scope: str
    selection_seconds: float
    reason: str


class NgramPoolSelector:
    def select(self, request: PoolRequest) -> PoolSelection:
        started = time.perf_counter()
        pool_id: str | None = None
        scope = "off"
        reason = "no_relevant_metadata"
        if request.session_id and request.exact_session_match:
            pool_id, scope, reason = (
                f"session:{request.session_id}", "session", "exact_session_match",
            )
        elif request.repository:
            pool_id, scope, reason = (
                f"project:{request.repository}", "project", "same_repository",
            )
        elif request.workspace:
            pool_id, scope, reason = (
                f"project:{request.workspace}", "project", "same_workspace",
            )
        elif request.tool in {"shell", "systemd", "docker"} or request.task_type == "system":
            domain = request.tool or "system"
            pool_id, scope, reason = f"domain:{domain}", "domain", "structured_domain"
        return PoolSelection(pool_id, scope, time.perf_counter() - started, reason)


@dataclass(frozen=True)
class NgramPoolQuota:
    max_global_bytes: int = 16 * 1024 * 1024
    max_project_count: int = 8
    max_session_count: int = 16
    max_total_bytes: int = 256 * 1024 * 1024


class BoundedNgramPoolCatalog:
    def __init__(self, quota: NgramPoolQuota = NgramPoolQuota()) -> None:
        if min(
            quota.max_global_bytes, quota.max_project_count,
            quota.max_session_count, quota.max_total_bytes,
        ) < 1:
            raise SpeculativeMemoryError("invalid_ngram_pool_quota")
        self.quota = quota
        self._pools: OrderedDict[str, tuple[str, int]] = OrderedDict()
        self.evictions = 0
        self.hits = 0
        self.misses = 0

    @property
    def total_bytes(self) -> int:
        return sum(size for _, size in self._pools.values())

    def register(self, pool_id: str, scope: str, size_bytes: int) -> None:
        if scope not in {"global", "domain", "project", "session"}:
            raise SpeculativeMemoryError("invalid_ngram_pool_scope")
        if size_bytes < 1 or size_bytes > self.quota.max_total_bytes:
            raise SpeculativeMemoryError("ngram_pool_exceeds_hard_quota")
        if scope == "global" and size_bytes > self.quota.max_global_bytes:
            raise SpeculativeMemoryError("global_ngram_pool_exceeds_hard_quota")
        self._pools.pop(pool_id, None)
        self._pools[pool_id] = (scope, size_bytes)
        self._enforce_scope("project", self.quota.max_project_count)
        self._enforce_scope("session", self.quota.max_session_count)
        while self.total_bytes > self.quota.max_total_bytes:
            self._pools.popitem(last=False)
            self.evictions += 1

    def _enforce_scope(self, scope: str, limit: int) -> None:
        while sum(item_scope == scope for item_scope, _ in self._pools.values()) > limit:
            victim = next(key for key, (item_scope, _) in self._pools.items() if item_scope == scope)
            del self._pools[victim]
            self.evictions += 1

    def contains(self, pool_id: str) -> bool:
        found = pool_id in self._pools
        if found:
            self.hits += 1
            self._pools.move_to_end(pool_id)
        else:
            self.misses += 1
        return found

    def metrics_snapshot(self, pool_id: str | None = None) -> dict[str, object]:
        scope = self._pools.get(pool_id, ("off", 0))[0] if pool_id else "off"
        size = self._pools.get(pool_id, (scope, 0))[1] if pool_id else 0
        return {
            "ngram_pool_id": pool_id,
            "ngram_pool_scope": scope,
            "ngram_pool_hits": self.hits,
            "ngram_pool_misses": self.misses,
            "ngram_pool_bytes": size,
            "ngram_pool_total_bytes": self.total_bytes,
            "ngram_pool_evictions": self.evictions,
        }


def _canonical_parameters(parameters: Mapping[str, object]) -> bytes:
    return json.dumps(
        parameters, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")


def token_prefix_hash(tokens: Sequence[int]) -> str:
    digest = hashlib.sha256()
    digest.update(struct.pack(">Q", len(tokens)))
    for token in tokens:
        if not -(2**31) <= int(token) < 2**31:
            raise SpeculativeMemoryError("token_id_out_of_int32_range")
        digest.update(struct.pack(">i", int(token)))
    return digest.hexdigest()


@dataclass(frozen=True)
class CheckpointKey:
    model_sha256: str
    runtime_commit: str
    tokenizer_identity: str
    chat_template_identity: str
    inference_parameters: Mapping[str, object]
    prefix_token_hash: str
    prefix_token_count: int

    @classmethod
    def from_tokens(
        cls, *, model_sha256: str, runtime_commit: str, tokenizer_identity: str,
        chat_template_identity: str, inference_parameters: Mapping[str, object],
        tokens: Sequence[int],
    ) -> "CheckpointKey":
        if len(model_sha256) != 64:
            raise SpeculativeMemoryError("invalid_model_sha256")
        return cls(
            model_sha256=model_sha256.lower(), runtime_commit=runtime_commit,
            tokenizer_identity=tokenizer_identity,
            chat_template_identity=chat_template_identity,
            inference_parameters=dict(inference_parameters),
            prefix_token_hash=token_prefix_hash(tokens), prefix_token_count=len(tokens),
        )

    @property
    def checkpoint_id(self) -> str:
        digest = hashlib.sha256()
        fields: Iterable[bytes] = (
            self.model_sha256.encode(), self.runtime_commit.encode(),
            self.tokenizer_identity.encode(), self.chat_template_identity.encode(),
            _canonical_parameters(self.inference_parameters),
            self.prefix_token_hash.encode(), str(self.prefix_token_count).encode(),
        )
        for field_bytes in fields:
            digest.update(struct.pack(">Q", len(field_bytes)))
            digest.update(field_bytes)
        return digest.hexdigest()


@dataclass
class PromptStateMetrics:
    hits: int = 0
    misses: int = 0
    invalidations: int = 0
    evictions: int = 0
    ttft_saved_seconds: float = 0.0


@dataclass
class _Checkpoint:
    key: CheckpointKey
    payload: bytes
    created_at: float = field(default_factory=time.monotonic)


class BoundedPromptStateCache:
    def __init__(self, *, max_count: int, max_total_bytes: int) -> None:
        if max_count < 1 or max_total_bytes < 1:
            raise SpeculativeMemoryError("invalid_prompt_state_quota")
        self.max_count = max_count
        self.max_total_bytes = max_total_bytes
        self._entries: OrderedDict[str, _Checkpoint] = OrderedDict()
        self._total_bytes = 0
        self.metrics = PromptStateMetrics()

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    @property
    def count(self) -> int:
        return len(self._entries)

    def put(self, key: CheckpointKey, payload: bytes) -> str:
        checkpoint_id = key.checkpoint_id
        if len(payload) > self.max_total_bytes:
            raise SpeculativeMemoryError("checkpoint_exceeds_hard_quota")
        old = self._entries.pop(checkpoint_id, None)
        if old is not None:
            self._total_bytes -= len(old.payload)
        self._entries[checkpoint_id] = _Checkpoint(key=key, payload=bytes(payload))
        self._total_bytes += len(payload)
        while self.count > self.max_count or self.total_bytes > self.max_total_bytes:
            _, evicted = self._entries.popitem(last=False)
            self._total_bytes -= len(evicted.payload)
            self.metrics.evictions += 1
        return checkpoint_id

    def restore(self, key: CheckpointKey, *, estimated_ttft_saved: float = 0.0) -> bytes | None:
        checkpoint_id = key.checkpoint_id
        entry = self._entries.get(checkpoint_id)
        if entry is None or entry.key != key:
            self.metrics.misses += 1
            return None
        self._entries.move_to_end(checkpoint_id)
        self.metrics.hits += 1
        self.metrics.ttft_saved_seconds += max(0.0, estimated_ttft_saved)
        return entry.payload

    def invalidate(self, checkpoint_id: str) -> bool:
        entry = self._entries.pop(checkpoint_id, None)
        if entry is None:
            return False
        self._total_bytes -= len(entry.payload)
        self.metrics.invalidations += 1
        return True

    def decision(self, key: CheckpointKey) -> PromptStateDecision:
        hit = key.checkpoint_id in self._entries and self._entries[key.checkpoint_id].key == key
        return PromptStateDecision(
            exact_prefix_hash=key.prefix_token_hash,
            checkpoint_hit=hit,
            checkpoint_id=key.checkpoint_id if hit else None,
        )

    def metrics_snapshot(self) -> dict[str, int | float]:
        return {
            "prompt_state_cache_hits": self.metrics.hits,
            "prompt_state_cache_misses": self.metrics.misses,
            "prompt_state_bytes": max((len(entry.payload) for entry in self._entries.values()), default=0),
            "prompt_state_total_bytes": self.total_bytes,
            "prompt_state_count": self.count,
            "prompt_state_invalidations": self.metrics.invalidations,
            "prompt_state_evictions": self.metrics.evictions,
            "prompt_state_ttft_saved_seconds": self.metrics.ttft_saved_seconds,
        }


__all__ = [
    "BoundedPromptStateCache", "CheckpointKey", "NgramPoolSelector", "PoolRequest",
    "PoolSelection", "PromptStateDecision", "RetrievalDecision", "SpeculativeMemoryError",
    "BoundedNgramPoolCatalog", "NgramPoolQuota",
    "token_prefix_hash",
]
