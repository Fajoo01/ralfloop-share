from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import os
from pathlib import Path
import random
import sqlite3
import time
from typing import Callable, Mapping, Sequence


@dataclass(frozen=True)
class PheromonePolicy:
    """Tuning for adaptive routing among already-authorized alternatives."""

    alpha: float = 1.0
    beta: float = 1.0
    evaporation_half_life_seconds: float = 21_600.0
    exploration_rate: float = 0.10
    initial_pheromone: float = 1.0
    min_pheromone: float = 0.05
    max_pheromone: float = 50.0
    success_deposit: float = 1.0
    failure_retention: float = 0.50
    timeout_retention: float = 0.25
    latency_reference_ms: float = 5_000.0
    cost_reference: float = 1.0
    ewma_weight: float = 0.20

    def __post_init__(self) -> None:
        if self.alpha < 0 or self.beta < 0:
            raise ValueError("alpha and beta must be non-negative")
        if self.evaporation_half_life_seconds <= 0:
            raise ValueError("evaporation half-life must be positive")
        if not 0 <= self.exploration_rate <= 1:
            raise ValueError("exploration_rate must be in [0, 1]")
        if not 0 < self.min_pheromone <= self.initial_pheromone <= self.max_pheromone:
            raise ValueError("pheromone bounds must satisfy 0 < min <= initial <= max")
        if not 0 <= self.failure_retention <= 1 or not 0 <= self.timeout_retention <= 1:
            raise ValueError("failure retention must be in [0, 1]")
        if self.latency_reference_ms <= 0 or self.cost_reference <= 0:
            raise ValueError("latency and cost references must be positive")
        if not 0 < self.ewma_weight <= 1:
            raise ValueError("ewma_weight must be in (0, 1]")


@dataclass(frozen=True)
class CandidateScore:
    candidate: str
    pheromone: float
    heuristic: float
    raw_score: float
    probability: float
    observations: int = 0
    successes: int = 0
    failures: int = 0
    latency_ewma_ms: float | None = None
    cost_ewma: float | None = None


@dataclass(frozen=True)
class PheromoneDecision:
    context: str
    selected: str
    scores: tuple[CandidateScore, ...]
    exploration_rate: float
    policy_boundary: str = "authorized_candidates_only"

    def to_dict(self) -> dict:
        return {
            "context": self.context,
            "selected": self.selected,
            "scores": [asdict(item) for item in self.scores],
            "exploration_rate": self.exploration_rate,
            "policy_boundary": self.policy_boundary,
        }


class PheromoneRouter:
    """ACO-inspired adaptive scorer.

    This class never discovers or authorizes capabilities. Callers must pass the
    complete set of candidates already allowed by deterministic policy. Historical
    state for any other candidate is ignored by construction.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        policy: PheromonePolicy | None = None,
        clock: Callable[[], float] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.policy = policy or PheromonePolicy()
        self._clock = clock or time.time
        self._rng = rng or random.Random()

    def rank(
        self,
        context: str,
        allowed_candidates: Sequence[str],
        *,
        heuristics: Mapping[str, float] | None = None,
        now: float | None = None,
    ) -> tuple[CandidateScore, ...]:
        context = self._validate_context(context)
        candidates = self._validate_candidates(allowed_candidates)
        heuristics = heuristics or {}
        timestamp = self._clock() if now is None else float(now)
        rows = self._load_rows(context, candidates)

        interim: list[tuple[str, float, float, float, tuple]] = []
        total = 0.0
        for candidate in candidates:
            row = rows.get(candidate)
            if row is None:
                pheromone = self.policy.initial_pheromone
                stats = (0, 0, 0, None, None)
            else:
                pheromone = self._evaporated(float(row[0]), float(row[6]), timestamp)
                stats = (int(row[1]), int(row[2]), int(row[3]), row[4], row[5])
            heuristic = max(float(heuristics.get(candidate, 1.0)), 1e-12)
            raw_score = (pheromone ** self.policy.alpha) * (heuristic ** self.policy.beta)
            interim.append((candidate, pheromone, heuristic, raw_score, stats))
            total += raw_score

        base_probability = 1.0 / len(interim)
        scores: list[CandidateScore] = []
        for candidate, pheromone, heuristic, raw_score, stats in interim:
            normalized = raw_score / total if total > 0 else base_probability
            probability = (
                (1.0 - self.policy.exploration_rate) * normalized
                + self.policy.exploration_rate * base_probability
            )
            observations, successes, failures, latency_ewma_ms, cost_ewma = stats
            scores.append(
                CandidateScore(
                    candidate=candidate,
                    pheromone=pheromone,
                    heuristic=heuristic,
                    raw_score=raw_score,
                    probability=probability,
                    observations=observations,
                    successes=successes,
                    failures=failures,
                    latency_ewma_ms=latency_ewma_ms,
                    cost_ewma=cost_ewma,
                )
            )
        return tuple(scores)

    def choose(
        self,
        context: str,
        allowed_candidates: Sequence[str],
        *,
        heuristics: Mapping[str, float] | None = None,
        now: float | None = None,
    ) -> PheromoneDecision:
        scores = self.rank(context, allowed_candidates, heuristics=heuristics, now=now)
        needle = self._rng.random()
        cumulative = 0.0
        selected = scores[-1].candidate
        for item in scores:
            cumulative += item.probability
            if needle <= cumulative:
                selected = item.candidate
                break
        return PheromoneDecision(
            context=context,
            selected=selected,
            scores=scores,
            exploration_rate=self.policy.exploration_rate,
        )

    def observe(
        self,
        context: str,
        candidate: str,
        *,
        outcome: str,
        quality: float = 1.0,
        latency_ms: float | None = None,
        cost: float | None = None,
        now: float | None = None,
    ) -> CandidateScore:
        """Apply one verified outcome after execution.

        outcome accepts ``success``, ``failure`` or ``timeout``. ``quality`` is a
        bounded verifier signal, not free-form model confidence.
        """

        context = self._validate_context(context)
        candidate = self._validate_candidates([candidate])[0]
        if outcome not in {"success", "failure", "timeout"}:
            raise ValueError("outcome must be success, failure or timeout")
        timestamp = self._clock() if now is None else float(now)
        quality = min(max(float(quality), 0.0), 1.0)
        latency = None if latency_ms is None else max(float(latency_ms), 0.0)
        numeric_cost = None if cost is None else max(float(cost), 0.0)

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT pheromone, observations, successes, failures,
                       latency_ewma_ms, cost_ewma, updated_at
                FROM route_pheromone
                WHERE context = ? AND candidate = ?
                """,
                (context, candidate),
            ).fetchone()
            if row is None:
                pheromone = self.policy.initial_pheromone
                observations = successes = failures = 0
                latency_ewma_ms = cost_ewma = None
            else:
                pheromone = self._evaporated(float(row[0]), float(row[6]), timestamp)
                observations = int(row[1])
                successes = int(row[2])
                failures = int(row[3])
                latency_ewma_ms = row[4]
                cost_ewma = row[5]

            if outcome == "success":
                latency_factor = 1.0 if latency is None else 1.0 / (1.0 + latency / self.policy.latency_reference_ms)
                cost_factor = 1.0 if numeric_cost is None else 1.0 / (1.0 + numeric_cost / self.policy.cost_reference)
                deposit = self.policy.success_deposit * quality * latency_factor * cost_factor
                pheromone = min(self.policy.max_pheromone, pheromone + deposit)
                successes += 1
            else:
                retention = self.policy.timeout_retention if outcome == "timeout" else self.policy.failure_retention
                pheromone = max(self.policy.min_pheromone, pheromone * retention)
                failures += 1
            observations += 1
            latency_ewma_ms = self._ewma(latency_ewma_ms, latency)
            cost_ewma = self._ewma(cost_ewma, numeric_cost)

            conn.execute(
                """
                INSERT INTO route_pheromone(
                    context, candidate, pheromone, observations, successes,
                    failures, latency_ewma_ms, cost_ewma, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(context, candidate) DO UPDATE SET
                    pheromone=excluded.pheromone,
                    observations=excluded.observations,
                    successes=excluded.successes,
                    failures=excluded.failures,
                    latency_ewma_ms=excluded.latency_ewma_ms,
                    cost_ewma=excluded.cost_ewma,
                    updated_at=excluded.updated_at
                """,
                (
                    context,
                    candidate,
                    pheromone,
                    observations,
                    successes,
                    failures,
                    latency_ewma_ms,
                    cost_ewma,
                    timestamp,
                ),
            )
            conn.commit()

        return self.rank(context, [candidate], now=timestamp)[0]

    def _load_rows(self, context: str, candidates: tuple[str, ...]) -> dict[str, tuple]:
        if not self.db_path.exists():
            return {}
        try:
            with sqlite3.connect(self.db_path, timeout=2.0) as conn:
                self._ensure_schema(conn)
                placeholders = ",".join("?" for _ in candidates)
                query = f"""
                    SELECT candidate, pheromone, observations, successes, failures,
                           latency_ewma_ms, cost_ewma, updated_at
                    FROM route_pheromone
                    WHERE context = ? AND candidate IN ({placeholders})
                """
                rows = conn.execute(query, (context, *candidates)).fetchall()
        except sqlite3.Error:
            return {}
        return {
            str(row[0]): (row[1], row[2], row[3], row[4], row[5], row[6], row[7])
            for row in rows
        }

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS route_pheromone (
                context TEXT NOT NULL,
                candidate TEXT NOT NULL,
                pheromone REAL NOT NULL,
                observations INTEGER NOT NULL DEFAULT 0,
                successes INTEGER NOT NULL DEFAULT 0,
                failures INTEGER NOT NULL DEFAULT 0,
                latency_ewma_ms REAL,
                cost_ewma REAL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(context, candidate)
            )
            """
        )

    def _evaporated(self, value: float, updated_at: float, now: float) -> float:
        elapsed = max(0.0, now - updated_at)
        retention = math.exp(-math.log(2.0) * elapsed / self.policy.evaporation_half_life_seconds)
        above_floor = max(value, self.policy.min_pheromone) - self.policy.min_pheromone
        return self.policy.min_pheromone + above_floor * retention

    def _ewma(self, old: float | None, new: float | None) -> float | None:
        if new is None:
            return old
        if old is None:
            return new
        weight = self.policy.ewma_weight
        return (1.0 - weight) * float(old) + weight * new

    @staticmethod
    def _validate_context(context: str) -> str:
        value = str(context).strip()
        if not value:
            raise ValueError("context must not be empty")
        return value

    @staticmethod
    def _validate_candidates(candidates: Sequence[str]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(str(item).strip() for item in candidates if str(item).strip()))
        if not normalized:
            raise ValueError("at least one authorized candidate is required")
        return normalized


def default_pheromone_db() -> Path:
    configured = os.getenv("RALF_PHEROMONE_DB")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state" / "ralfloop" / "pheromone-router.sqlite3"


def pheromone_mode() -> str:
    value = os.getenv("RALF_PHEROMONE_ROUTING", "off").strip().lower()
    return value if value in {"off", "shadow", "active"} else "off"


__all__ = [
    "CandidateScore",
    "PheromoneDecision",
    "PheromonePolicy",
    "PheromoneRouter",
    "default_pheromone_db",
    "pheromone_mode",
]
