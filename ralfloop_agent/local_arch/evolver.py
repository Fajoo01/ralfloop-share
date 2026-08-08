from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path
import random
import re
import sqlite3
import statistics
import time
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence
from urllib.request import Request, urlopen

from .sandbox import EvaluationResult
from .store import ArtifactStore, canonical_json, sha256_bytes


MUTATION_TYPES = (
    "micro_optimization", "data_structure", "algorithmic", "memory_layout", "allocation", "loop",
    "vectorization", "caching", "parallelization", "approximation", "heuristic", "compiler_hint",
    "architecture", "parameterization",
)
EVOLVER_VERSION = "ralfloop-evolver-v1"


@dataclass(frozen=True)
class Budget:
    generations: int
    candidates: int
    llm_calls: int
    wall_seconds: int


@dataclass(frozen=True)
class EvolutionSpec:
    problem_id: str
    task_type: str
    language: str
    objective: str
    hard_constraints: tuple[str, ...]
    soft_objectives: tuple[str, ...]
    seed_artifact: str
    evaluator: str
    metrics: tuple[str, ...]
    acceptance_tests: str
    budget: Budget
    fitness_mode: str = "pareto"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "EvolutionSpec":
        required = {
            "problem_id", "task_type", "language", "objective", "hard_constraints", "soft_objectives",
            "seed_artifact", "evaluator", "metrics", "acceptance_tests", "budget",
        }
        if not required.issubset(value):
            missing = sorted(required - set(value))
            raise ValueError(f"evolution_precondition_missing:{','.join(missing)}")
        if value["language"] not in {"c", "cpp", "python", "sql", "config"}:
            raise ValueError("unsupported_evolution_language")
        if not value["hard_constraints"] or not value["metrics"] or not value["acceptance_tests"]:
            raise ValueError("baseline_evaluator_metrics_tests_required")
        if not str(value["seed_artifact"]).startswith("sha256:"):
            raise ValueError("immutable_seed_artifact_required")
        raw_budget = value["budget"]
        budget = Budget(*(int(raw_budget[key]) for key in ("generations", "candidates", "llm_calls", "wall_seconds")))
        if min(asdict(budget).values()) <= 0:
            raise ValueError("invalid_evolution_budget")
        return cls(
            str(value["problem_id"]), str(value["task_type"]), str(value["language"]), str(value["objective"]),
            tuple(value["hard_constraints"]), tuple(value["soft_objectives"]), str(value["seed_artifact"]),
            str(value["evaluator"]), tuple(value["metrics"]), str(value["acceptance_tests"]), budget,
            str(value.get("fitness_mode", "pareto")),
        )


@dataclass(frozen=True)
class Fitness:
    valid: bool
    raw: Mapping[str, float | bool | int | None]
    normalized: Mapping[str, float]
    score: float | None
    pareto: tuple[float, ...]
    lexicographic: tuple[float, ...]
    version: str = "fitness-v1"


def compute_fitness(
    result: EvaluationResult | Mapping[str, Any],
    *,
    baseline: Mapping[str, float] | None = None,
    weights: Mapping[str, float] | None = None,
) -> Fitness:
    raw = result.as_dict() if isinstance(result, EvaluationResult) else dict(result)
    valid = bool(raw.get("correct")) and int(raw.get("tests_passed", 0)) == int(raw.get("tests_total", 0))
    valid &= not bool(raw.get("timeout")) and not bool(raw.get("crash")) and not bool(raw.get("sanitizer_errors"))
    if not valid:
        return Fitness(False, raw, {}, None, (), (0.0,), "fitness-v1")
    baseline = baseline or {"wall_ms": float(raw.get("wall_ms") or 1), "rss_mb": float(raw.get("rss_mb") or 1), "binary_kb": float(raw.get("binary_kb") or 1)}
    weights = weights or {"wall_ms": 0.60, "rss_mb": 0.25, "binary_kb": 0.15}
    normalized: dict[str, float] = {}
    for metric in ("wall_ms", "rss_mb", "binary_kb"):
        measured = max(float(raw.get(metric) or baseline.get(metric, 1)), 1e-12)
        normalized[metric] = max(0.0, min(2.0, baseline.get(metric, measured) / measured))
    score = sum(normalized[name] * weight for name, weight in weights.items())
    pareto = tuple(-float(raw.get(name) or float("inf")) for name in ("wall_ms", "rss_mb", "binary_kb"))
    lexicographic = (1.0, -float(raw.get("wall_ms") or float("inf")), -float(raw.get("rss_mb") or float("inf")))
    return Fitness(True, raw, normalized, score, pareto, lexicographic, "fitness-v1")


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    parent_id: str | None
    generation: int
    source_hash: str
    mutation_type: str
    fitness: Fitness
    lineage_depth: int = 0
    novelty: float = 0.0


class ParentSelector:
    def __init__(self, seed: int = 1):
        self.random = random.Random(seed)

    def select(self, candidates: Sequence[Candidate], strategy: str = "tournament") -> Candidate:
        valid = [item for item in candidates if item.fitness.valid]
        if not valid:
            raise ValueError("no_valid_parent")
        if strategy == "top_k":
            pool = sorted(valid, key=lambda item: item.fitness.score or float("-inf"), reverse=True)[: max(1, min(5, len(valid)))]
            return self.random.choice(pool)
        if strategy == "novelty":
            return max(valid, key=lambda item: (item.novelty, item.fitness.score or 0))
        if strategy == "pareto":
            frontier = pareto_frontier(valid)
            return self.random.choice(frontier)
        if strategy == "lineage_diversity":
            return min(valid, key=lambda item: (item.lineage_depth, -(item.fitness.score or 0)))
        if strategy == "random_elite":
            elite = sorted(valid, key=lambda item: item.fitness.score or 0, reverse=True)[: max(1, len(valid) // 4)]
            return self.random.choice(elite)
        if strategy != "tournament":
            raise ValueError("unknown_parent_strategy")
        sampled = self.random.sample(valid, min(3, len(valid)))
        return max(sampled, key=lambda item: item.fitness.score or float("-inf"))


def pareto_frontier(candidates: Sequence[Candidate]) -> list[Candidate]:
    result: list[Candidate] = []
    for candidate in candidates:
        dominated = False
        for other in candidates:
            if other is candidate:
                continue
            left, right = other.fitness.pareto, candidate.fitness.pareto
            if left and all(a >= b for a, b in zip(left, right)) and any(a > b for a, b in zip(left, right)):
                dominated = True
                break
        if not dominated:
            result.append(candidate)
    return result


class StagnationTracker:
    def __init__(self, patience: int = 8, minimum_improvement: float = 0.002):
        self.patience = patience
        self.minimum_improvement = minimum_improvement
        self.best = float("-inf")
        self.stale = 0

    def update(self, score: float | None) -> str:
        if score is not None and score > self.best + self.minimum_improvement:
            self.best = score
            self.stale = 0
            return "improved"
        self.stale += 1
        if self.stale >= self.patience:
            return "increase_diversity"
        return "continue"


class StopController:
    def __init__(self, budget: Budget, started: float | None = None):
        self.budget = budget
        self.started = started if started is not None else time.monotonic()

    def reason(self, *, generation: int, candidates: int, llm_calls: int, stagnated: bool = False, evaluator_ok: bool = True, cancelled: bool = False) -> str | None:
        if cancelled:
            return "human_cancel"
        if not evaluator_ok:
            return "evaluator_failure"
        if generation >= self.budget.generations:
            return "generation_budget"
        if candidates >= self.budget.candidates:
            return "candidate_budget"
        if llm_calls >= self.budget.llm_calls:
            return "llm_call_budget"
        if time.monotonic() - self.started >= self.budget.wall_seconds:
            return "wall_time_budget"
        if stagnated:
            return "stagnation"
        return None


class MutationPrompt:
    @staticmethod
    def build(
        parent: Candidate,
        *,
        task: str,
        metrics: Mapping[str, Any],
        bottleneck: str,
        good: Sequence[str],
        bad: Sequence[str],
    ) -> str:
        lines = [
            f"TASK {task}", f"PARENT {parent.candidate_id}", f"FIT {parent.fitness.score or 0:.3f}",
            f"TEST {metrics.get('tests_passed', 0)}/{metrics.get('tests_total', 0)}",
            f"WALL {metrics.get('wall_ms', 'na')}ms", f"RSS {metrics.get('rss_mb', 'na')}MB",
            "", "BOTTLENECK:", bottleneck[:160], "", "GOOD:", *[item[:120] for item in good[:4]],
            "", "BAD:", *[item[:120] for item in bad[:4]], "", "GOAL:",
            "improve measured objectives without changing API, evaluator, tests, or correctness checks",
            "", "RETURN:", "one unified diff only",
        ]
        return "\n".join(lines)


def validate_candidate_patch(patch: str, *, max_bytes: int = 32768) -> str:
    if not patch.startswith("--- a/candidate.c\n+++ b/candidate.c\n"):
        raise ValueError("single_candidate_unified_diff_required")
    if len(patch.encode()) > max_bytes or "GIT binary patch" in patch:
        raise ValueError("invalid_patch_size_or_type")
    forbidden = ("evaluator", "benchmark", "dataset", "expected", "tests/", "../", "/dev/", "/proc/")
    if any(term in patch.casefold() for term in forbidden):
        raise ValueError("protected_content_in_patch")
    return hashlib.sha256(patch.encode()).hexdigest()


class ProgramDatabase:
    SCHEMA = """
    PRAGMA foreign_keys=ON;
    CREATE TABLE IF NOT EXISTS experiments (experiment_id TEXT PRIMARY KEY, problem_id TEXT NOT NULL, spec_hash TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS programs (candidate_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL, parent_id TEXT, generation INTEGER NOT NULL, source_hash TEXT NOT NULL UNIQUE, binary_hash TEXT, patch_hash TEXT, model_id TEXT, prompt_hash TEXT, mutation_type TEXT NOT NULL, evaluation_id TEXT, fitness REAL, correctness INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS evaluations (evaluation_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, evaluator_version TEXT NOT NULL, dataset_hash TEXT NOT NULL, raw_json TEXT NOT NULL, fitness_version TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS parents (candidate_id TEXT NOT NULL, parent_id TEXT NOT NULL, selection_method TEXT NOT NULL, PRIMARY KEY(candidate_id,parent_id));
    CREATE TABLE IF NOT EXISTS mutations (mutation_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, mutation_type TEXT NOT NULL, patch_hash TEXT NOT NULL, prompt_hash TEXT, model_id TEXT);
    CREATE TABLE IF NOT EXISTS generations (experiment_id TEXT NOT NULL, generation INTEGER NOT NULL, best_candidate_id TEXT, median_fitness REAL, diversity REAL, PRIMARY KEY(experiment_id,generation));
    CREATE TABLE IF NOT EXISTS artifacts (ref TEXT PRIMARY KEY, candidate_id TEXT, kind TEXT NOT NULL, bytes INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS metrics (evaluation_id TEXT NOT NULL, name TEXT NOT NULL, raw_value REAL, normalized_value REAL, PRIMARY KEY(evaluation_id,name));
    CREATE TABLE IF NOT EXISTS failures (failure_id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT, category TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS lineages (candidate_id TEXT NOT NULL, ancestor_id TEXT NOT NULL, depth INTEGER NOT NULL, PRIMARY KEY(candidate_id,ancestor_id));
    CREATE TABLE IF NOT EXISTS prompt_samples (prompt_hash TEXT PRIMARY KEY, prompt_ref TEXT NOT NULL, token_estimate INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS model_calls (call_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL, model_id TEXT NOT NULL, prompt_hash TEXT NOT NULL, latency_ms REAL, status TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_program_generation ON programs(generation, fitness DESC);
    CREATE INDEX IF NOT EXISTS idx_program_parent ON programs(parent_id);
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(self.SCHEMA)

    def create_experiment(self, spec: EvolutionSpec) -> str:
        spec_hash = sha256_bytes(canonical_json(asdict(spec)))
        experiment_id = f"exp-{spec_hash[:16]}"
        self.connection.execute(
            "INSERT OR IGNORE INTO experiments VALUES(?,?,?,?,?)",
            (experiment_id, spec.problem_id, spec_hash, "created", _now()),
        )
        self.connection.commit()
        return experiment_id

    def add_candidate(
        self,
        candidate: Candidate,
        result: EvaluationResult,
        *,
        patch_hash: str | None = None,
        model_id: str | None = None,
        prompt_hash: str | None = None,
        experiment_id: str = "unscoped",
    ) -> str:
        evaluation_id = f"eval-{candidate.source_hash[:16]}"
        try:
            self.connection.execute(
                "INSERT INTO programs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (candidate.candidate_id, experiment_id, candidate.parent_id, candidate.generation, candidate.source_hash,
                 result.binary_hash, patch_hash, model_id, prompt_hash, candidate.mutation_type, evaluation_id,
                 candidate.fitness.score, int(candidate.fitness.valid), "valid" if candidate.fitness.valid else "invalid", _now()),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("duplicate_candidate") from exc
        self.connection.execute(
            "INSERT INTO evaluations VALUES(?,?,?,?,?,?,?)",
            (evaluation_id, candidate.candidate_id, result.evaluator_version, result.dataset_hash,
             json.dumps(result.as_dict(), sort_keys=True), candidate.fitness.version, _now()),
        )
        for name, value in candidate.fitness.raw.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self.connection.execute(
                    "INSERT OR REPLACE INTO metrics VALUES(?,?,?,?)",
                    (evaluation_id, name, float(value), candidate.fitness.normalized.get(name)),
                )
        if candidate.parent_id:
            self.connection.execute("INSERT INTO parents VALUES(?,?,?)", (candidate.candidate_id, candidate.parent_id, "recorded"))
            self.connection.execute("INSERT INTO lineages VALUES(?,?,?)", (candidate.candidate_id, candidate.parent_id, 1))
            ancestors = self.connection.execute("SELECT ancestor_id,depth FROM lineages WHERE candidate_id=?", (candidate.parent_id,)).fetchall()
            self.connection.executemany("INSERT INTO lineages VALUES(?,?,?)", ((candidate.candidate_id, row[0], row[1] + 1) for row in ancestors))
        self.connection.commit()
        return evaluation_id

    def lineage(self, candidate_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT ancestor_id,depth FROM lineages WHERE candidate_id=? ORDER BY depth", (candidate_id,))]

    def best(self, experiment_id: str | None = None) -> dict[str, Any] | None:
        if experiment_id:
            row = self.connection.execute("SELECT * FROM programs WHERE correctness=1 AND experiment_id=? ORDER BY fitness DESC LIMIT 1", (experiment_id,)).fetchone()
        else:
            row = self.connection.execute("SELECT * FROM programs WHERE correctness=1 ORDER BY fitness DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        self.connection.close()


class EvolutionArchive:
    def __init__(self, store: ArtifactStore):
        self.store = store

    def save_candidate(
        self,
        *,
        source: str,
        patch: str,
        compile_log: str,
        result: EvaluationResult,
        fitness: Fitness,
        lineage: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> dict[str, str]:
        values = {
            "candidate.c": (source.encode(), "text/x-c"),
            "candidate.patch": (patch.encode(), "text/x-diff"),
            "compile.log": (compile_log.encode(), "text/plain"),
            "tests.json": (canonical_json({"passed": result.tests_passed, "total": result.tests_total}), "application/json"),
            "benchmark.json": (canonical_json({"median_ms": result.median_ms, "p95_ms": result.p95_ms, "noise": result.noise}), "application/json"),
            "fitness.json": (canonical_json(asdict(fitness)), "application/json"),
            "lineage.json": (canonical_json(lineage), "application/json"),
            "metadata.json": (canonical_json(metadata), "application/json"),
        }
        return {name: self.store.put(data, media_type=media, provenance={"kind": name, **metadata}).ref for name, (data, media) in values.items()}


def population_diversity(candidates: Sequence[Candidate]) -> float:
    if len(candidates) < 2:
        return 0.0
    prefixes = {item.source_hash[:8] for item in candidates}
    lineages = {item.parent_id for item in candidates}
    return min(1.0, (len(prefixes) + len(lineages)) / (2 * len(candidates)))


def generation_summary(candidates: Sequence[Candidate]) -> dict[str, Any]:
    scores = [item.fitness.score for item in candidates if item.fitness.score is not None]
    return {
        "candidates": len(candidates),
        "valid": sum(item.fitness.valid for item in candidates),
        "invalid": sum(not item.fitness.valid for item in candidates),
        "best": max(scores) if scores else None,
        "median": statistics.median(scores) if scores else None,
        "diversity": population_diversity(candidates),
    }


class MutationProvider(Protocol):
    model_id: str

    def propose(self, prompt: str) -> str: ...


class QwenMutationProvider:
    """Loopback-only proposal provider. It cannot execute the returned patch."""

    model_id = "qwen2.5-coder:7b"

    def __init__(self, endpoint: str = "http://127.0.0.1:11434/api/generate", timeout: float = 90.0):
        if not endpoint.startswith("http://127.0.0.1:"):
            raise ValueError("qwen_endpoint_must_be_loopback")
        self.endpoint = endpoint
        self.timeout = timeout

    def propose(self, prompt: str) -> str:
        payload = canonical_json({
            "model": self.model_id,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": 768},
        })
        request = Request(self.endpoint, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=self.timeout) as response:
            result = json.loads(response.read(2 * 1024 * 1024))
        patch = result.get("response")
        if not isinstance(patch, str):
            raise ValueError("qwen_missing_patch")
        validate_candidate_patch(patch)
        return patch


@dataclass(frozen=True)
class GenerationResult:
    experiment_id: str
    generation: int
    candidates: tuple[Candidate, ...]
    summary: Mapping[str, Any]
    stop_reason: str | None
    strategy_action: str


class EvolutionEngine:
    """Bounded generation loop: propose -> patch -> sandbox -> evaluate -> persist -> select."""

    def __init__(
        self,
        database: ProgramDatabase,
        archive: EvolutionArchive,
        evaluator: Any,
        mutation_provider: MutationProvider,
        *,
        selector: ParentSelector | None = None,
        strategic_provider: Callable[[str], str] | None = None,
    ):
        self.database = database
        self.archive = archive
        self.evaluator = evaluator
        self.mutation_provider = mutation_provider
        self.selector = selector or ParentSelector()
        self.strategic_provider = strategic_provider
        self._evaluator_hash = sha256_bytes(inspect.getsource(type(evaluator)).encode())

    def generation(
        self,
        *,
        experiment_id: str,
        spec: EvolutionSpec,
        generation: int,
        population: Sequence[Candidate],
        sources: Mapping[str, str],
        tests: Sequence[Any],
        baseline_metrics: Mapping[str, float],
        candidates_per_generation: int,
        bottleneck: str,
        good: Sequence[str] = (),
        bad: Sequence[str] = (),
        selection_strategy: str = "tournament",
    ) -> GenerationResult:
        if not population or not tests:
            raise ValueError("population_and_tests_required")
        if sha256_bytes(inspect.getsource(type(self.evaluator)).encode()) != self._evaluator_hash:
            raise RuntimeError("evaluator_mutated")
        tests_hash = sha256_bytes(canonical_json([asdict(item) if is_dataclass(item) else item for item in tests]))
        controller = StopController(spec.budget)
        generated: list[Candidate] = []
        failures = 0
        llm_calls = 0
        for index in range(candidates_per_generation):
            stop = controller.reason(generation=generation, candidates=index, llm_calls=llm_calls)
            if stop:
                return GenerationResult(experiment_id, generation, tuple(generated), generation_summary(generated), stop, "stop")
            parent = self.selector.select(population, selection_strategy)
            prompt = MutationPrompt.build(parent, task=f"{spec.task_type} {spec.objective}", metrics=parent.fitness.raw, bottleneck=bottleneck, good=good, bad=bad)
            prompt_hash = sha256_bytes(prompt.encode())
            prompt_ref = self.archive.store.put(prompt.encode(), media_type="text/plain", provenance={"kind": "mutation_prompt", "experiment": experiment_id}).ref
            self.database.connection.execute("INSERT OR IGNORE INTO prompt_samples VALUES(?,?,?)", (prompt_hash, prompt_ref, max(1, len(prompt) // 4)))
            llm_calls += 1
            call_started = time.perf_counter_ns()
            call_id = f"call-{experiment_id[-8:]}-{generation}-{index}"
            try:
                patch = self.mutation_provider.propose(prompt)
                self.database.connection.execute(
                    "INSERT INTO model_calls VALUES(?,?,?,?,?,?,?)",
                    (call_id, experiment_id, self.mutation_provider.model_id, prompt_hash, (time.perf_counter_ns() - call_started) / 1_000_000, "ok", _now()),
                )
                patch_hash = validate_candidate_patch(patch)
                source = apply_unified_diff(sources[parent.candidate_id], patch)
                source_hash = sha256_bytes(source.encode())
                if sha256_bytes(canonical_json([asdict(item) if is_dataclass(item) else item for item in tests])) != tests_hash:
                    raise RuntimeError("dataset_mutated")
                if sha256_bytes(inspect.getsource(type(self.evaluator)).encode()) != self._evaluator_hash:
                    raise RuntimeError("evaluator_mutated")
                result = self.evaluator.evaluate(source, tests)
                if result.source_hash != source_hash:
                    raise RuntimeError("evaluator_source_hash_mismatch")
                if result.evaluator_version != spec.evaluator:
                    raise RuntimeError("evaluator_version_mismatch")
                fitness = compute_fitness(result, baseline=baseline_metrics)
                item = Candidate(
                    f"cand-{experiment_id[-8:]}-{generation}-{source_hash[:12]}", parent.candidate_id,
                    generation, source_hash, infer_mutation_type(patch), fitness, parent.lineage_depth + 1,
                    novelty=_source_novelty(source_hash, [candidate.source_hash for candidate in population]),
                )
                self.database.add_candidate(
                    item, result, patch_hash=patch_hash, model_id=self.mutation_provider.model_id,
                    prompt_hash=prompt_hash, experiment_id=experiment_id,
                )
                refs = self.archive.save_candidate(
                    source=source, patch=patch, compile_log=result.failure or "compile_ok", result=result,
                    fitness=fitness, lineage={"parent": parent.candidate_id, "generation": generation},
                    metadata={"experiment": experiment_id, "candidate": item.candidate_id, "model": self.mutation_provider.model_id},
                )
                self.database.connection.execute("INSERT INTO mutations VALUES(?,?,?,?,?,?)", (f"mut-{source_hash[:16]}", item.candidate_id, item.mutation_type, patch_hash, prompt_hash, self.mutation_provider.model_id))
                for kind, ref in refs.items():
                    self.database.connection.execute("INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?)", (ref, item.candidate_id, kind, len(self.archive.store.get(ref))))
                generated.append(item)
            except Exception as exc:
                failures += 1
                self.database.connection.execute(
                    "INSERT OR IGNORE INTO model_calls VALUES(?,?,?,?,?,?,?)",
                    (call_id, experiment_id, self.mutation_provider.model_id, prompt_hash, (time.perf_counter_ns() - call_started) / 1_000_000, "failed", _now()),
                )
                self.database.connection.execute(
                    "INSERT INTO failures(candidate_id,category,detail,created_at) VALUES(?,?,?,?)",
                    (parent.candidate_id, "mutation_or_evaluation", str(exc)[:1000], _now()),
                )
                self.database.connection.commit()
        summary = generation_summary(generated)
        self.database.connection.execute(
            "INSERT OR REPLACE INTO generations VALUES(?,?,?,?,?)",
            (experiment_id, generation, max(generated, key=lambda item: item.fitness.score or float("-inf")).candidate_id if generated else None,
             summary["median"], summary["diversity"]),
        )
        self.database.connection.commit()
        strategy = "continue"
        if not generated or failures == candidates_per_generation:
            strategy = "no_valid_mutations"
            if self.strategic_provider:
                strategy = "director:" + self.strategic_provider(json.dumps({"v": 1, "experiment": experiment_id, "generation": generation, "failures": failures}, separators=(",", ":")))[:160]
        return GenerationResult(experiment_id, generation, tuple(generated), summary, None, strategy)


def apply_unified_diff(original: str, patch: str) -> str:
    validate_candidate_patch(patch)
    original_lines = original.splitlines(keepends=True)
    patch_lines = patch.splitlines(keepends=True)[2:]
    output: list[str] = []
    source_index = 0
    index = 0
    hunk = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    while index < len(patch_lines):
        match = hunk.match(patch_lines[index])
        if not match:
            raise ValueError("invalid_unified_diff_hunk")
        old_start = int(match.group(1)) - 1
        if old_start < source_index:
            raise ValueError("overlapping_diff_hunk")
        output.extend(original_lines[source_index:old_start])
        source_index = old_start
        index += 1
        while index < len(patch_lines) and not patch_lines[index].startswith("@@"):
            line = patch_lines[index]
            if line.startswith(" "):
                if source_index >= len(original_lines) or original_lines[source_index].rstrip("\n") != line[1:].rstrip("\n"):
                    raise ValueError("diff_context_mismatch")
                output.append(original_lines[source_index])
                source_index += 1
            elif line.startswith("-"):
                if source_index >= len(original_lines) or original_lines[source_index].rstrip("\n") != line[1:].rstrip("\n"):
                    raise ValueError("diff_delete_mismatch")
                source_index += 1
            elif line.startswith("+"):
                output.append(line[1:])
            elif line.startswith("\\ No newline"):
                pass
            else:
                raise ValueError("invalid_diff_line")
            index += 1
    output.extend(original_lines[source_index:])
    return "".join(output)


def infer_mutation_type(patch: str) -> str:
    lowered = patch.casefold()
    for terms, mutation in (
        (("malloc", "calloc", "free"), "allocation"), (("struct ", "array", "vector"), "data_structure"),
        (("for (", "while ("), "loop"), (("cache", "memo"), "caching"), (("pragma", "inline"), "compiler_hint"),
    ):
        if any(term in lowered for term in terms):
            return mutation
    return "micro_optimization"


def _source_novelty(source_hash: str, peers: Sequence[str]) -> float:
    if not peers:
        return 1.0
    distances = [sum(left != right for left, right in zip(source_hash, peer)) / len(source_hash) for peer in peers]
    return min(distances)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
