import pytest

from ralfloop_agent.providers.speculative_memory import (
    BoundedNgramPoolCatalog,
    BoundedPromptStateCache,
    CheckpointKey,
    NgramPoolQuota,
    NgramPoolSelector,
    PoolRequest,
    SpeculativeMemoryError,
    token_prefix_hash,
)


MODEL_HASH = "3b46d1066bc91cc2d613e3bc22ce691dd77e6f0d33c9060690d24ce6de494375"


def key(tokens=(1, 2, 3), **changes):
    values = {
        "model_sha256": MODEL_HASH,
        "runtime_commit": "6b80c74f2",
        "tokenizer_identity": "qwen35:248320",
        "chat_template_identity": "sha256:template",
        "inference_parameters": {"ctx": 16384, "kv": "q8_0"},
        "tokens": tokens,
    }
    values.update(changes)
    return CheckpointKey.from_tokens(**values)


def test_token_hash_uses_token_identity_and_count():
    assert token_prefix_hash([1, 23]) != token_prefix_hash([12, 3])
    assert token_prefix_hash([1, 2]) != token_prefix_hash([1, 2, 0])


@pytest.mark.parametrize("field,value", [
    ("model_sha256", "0" * 64),
    ("runtime_commit", "other"),
    ("tokenizer_identity", "other"),
    ("chat_template_identity", "other"),
    ("inference_parameters", {"ctx": 8192, "kv": "q8_0"}),
    ("tokens", (1, 2, 4)),
])
def test_checkpoint_identity_fails_closed_on_any_relevant_change(field, value):
    cache = BoundedPromptStateCache(max_count=2, max_total_bytes=32)
    cache.put(key(), b"state")
    assert cache.restore(key(**{field: value})) is None
    assert cache.metrics.misses == 1


def test_prompt_state_hard_count_and_byte_quotas_are_lru():
    cache = BoundedPromptStateCache(max_count=2, max_total_bytes=8)
    first = key((1,))
    second = key((2,))
    third = key((3,))
    cache.put(first, b"aaaa")
    cache.put(second, b"bbbb")
    assert cache.restore(first) == b"aaaa"
    cache.put(third, b"cccc")
    assert cache.restore(second) is None
    assert cache.count == 2
    assert cache.total_bytes == 8
    assert cache.metrics.evictions == 1
    assert cache.metrics_snapshot()["prompt_state_total_bytes"] == 8
    with pytest.raises(SpeculativeMemoryError, match="hard_quota"):
        cache.put(key((4,)), b"012345678")


def test_pool_selector_prefers_session_then_project_then_domain_then_off():
    selector = NgramPoolSelector()
    assert selector.select(PoolRequest(session_id="s1", exact_session_match=True)).scope == "session"
    assert selector.select(PoolRequest(repository="ralfloop", tool="shell")).scope == "project"
    assert selector.select(PoolRequest(tool="systemd")).scope == "domain"
    assert selector.select(PoolRequest(task_type="novel_prose")).scope == "off"


def test_ngram_catalog_bounds_project_session_and_total_memory():
    catalog = BoundedNgramPoolCatalog(NgramPoolQuota(
        max_global_bytes=16, max_project_count=1, max_session_count=1,
        max_total_bytes=24,
    ))
    catalog.register("global", "global", 8)
    catalog.register("project:a", "project", 8)
    catalog.register("project:b", "project", 8)
    assert not catalog.contains("project:a")
    catalog.register("session:a", "session", 8)
    catalog.register("session:b", "session", 8)
    assert not catalog.contains("session:a")
    assert catalog.total_bytes <= 24
    assert catalog.contains("session:b")
    assert not catalog.contains("missing")
    metrics = catalog.metrics_snapshot("session:b")
    assert metrics["ngram_pool_hits"] == 1
    assert metrics["ngram_pool_misses"] == 3
