from __future__ import annotations

import random

import pytest

from src.pheromone_router import PheromonePolicy, PheromoneRouter


def test_initial_rank_is_uniform_and_does_not_create_state(tmp_path):
    db = tmp_path / "pheromone.sqlite3"
    router = PheromoneRouter(db)

    scores = router.rank("model:general", ["a", "b"], now=100.0)

    assert [item.probability for item in scores] == pytest.approx([0.5, 0.5])
    assert not db.exists()


def test_success_reward_includes_latency_and_cost(tmp_path):
    policy = PheromonePolicy(exploration_rate=0.0)
    router = PheromoneRouter(tmp_path / "pheromone.sqlite3", policy=policy)

    fast = router.observe(
        "mcp:research",
        "fast",
        outcome="success",
        quality=1.0,
        latency_ms=100,
        cost=0.05,
        now=100.0,
    )
    slow = router.observe(
        "mcp:research",
        "slow",
        outcome="success",
        quality=1.0,
        latency_ms=5_000,
        cost=1.0,
        now=100.0,
    )
    ranked = {item.candidate: item for item in router.rank("mcp:research", ["fast", "slow"], now=100.0)}

    assert fast.pheromone > slow.pheromone
    assert ranked["fast"].probability > ranked["slow"].probability


def test_failure_and_timeout_reduce_pheromone(tmp_path):
    policy = PheromonePolicy(exploration_rate=0.0)
    router = PheromoneRouter(tmp_path / "pheromone.sqlite3", policy=policy)

    failed = router.observe("lane:x", "failure", outcome="failure", now=10.0)
    timed_out = router.observe("lane:x", "timeout", outcome="timeout", now=10.0)

    assert failed.pheromone == pytest.approx(policy.initial_pheromone * policy.failure_retention)
    assert timed_out.pheromone == pytest.approx(policy.initial_pheromone * policy.timeout_retention)
    assert timed_out.pheromone < failed.pheromone


def test_evaporation_moves_old_signal_toward_floor(tmp_path):
    policy = PheromonePolicy(
        exploration_rate=0.0,
        evaporation_half_life_seconds=10.0,
        min_pheromone=0.1,
        initial_pheromone=1.0,
    )
    router = PheromoneRouter(tmp_path / "pheromone.sqlite3", policy=policy)
    reinforced = router.observe("lane:x", "a", outcome="success", latency_ms=0, cost=0, now=0.0)

    after_one_half_life = router.rank("lane:x", ["a"], now=10.0)[0]
    expected = policy.min_pheromone + (reinforced.pheromone - policy.min_pheromone) * 0.5

    assert after_one_half_life.pheromone == pytest.approx(expected)


def test_exploration_keeps_every_authorized_candidate_alive(tmp_path):
    policy = PheromonePolicy(exploration_rate=0.20, max_pheromone=100.0)
    router = PheromoneRouter(tmp_path / "pheromone.sqlite3", policy=policy)
    for index in range(30):
        router.observe("lane:x", "winner", outcome="success", latency_ms=0, cost=0, now=float(index))

    scores = {item.candidate: item for item in router.rank("lane:x", ["winner", "new"], now=30.0)}

    assert scores["new"].probability >= 0.10
    assert scores["winner"].probability > scores["new"].probability


def test_historical_forbidden_candidate_cannot_reenter_allowed_set(tmp_path):
    policy = PheromonePolicy(exploration_rate=0.05, max_pheromone=100.0)
    router = PheromoneRouter(tmp_path / "pheromone.sqlite3", policy=policy, rng=random.Random(7))
    for index in range(50):
        router.observe("model:general", "forbidden", outcome="success", latency_ms=0, cost=0, now=float(index))

    ranked_names = {item.candidate for item in router.rank("model:general", ["safe_a", "safe_b"], now=60.0)}
    chosen = {
        router.choose("model:general", ["safe_a", "safe_b"], now=60.0).selected
        for _ in range(100)
    }

    assert ranked_names == {"safe_a", "safe_b"}
    assert chosen <= {"safe_a", "safe_b"}
    assert "forbidden" not in chosen


def test_state_persists_across_router_instances(tmp_path):
    db = tmp_path / "pheromone.sqlite3"
    first = PheromoneRouter(db)
    first.observe("lane:x", "a", outcome="success", latency_ms=250, cost=0.2, now=100.0)

    second = PheromoneRouter(db)
    restored = second.rank("lane:x", ["a"], now=100.0)[0]

    assert restored.observations == 1
    assert restored.successes == 1
    assert restored.failures == 0
    assert restored.latency_ewma_ms == pytest.approx(250.0)
    assert restored.cost_ewma == pytest.approx(0.2)
