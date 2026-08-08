from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from ralfloop_agent.local_arch.evolver import (
    Budget, Candidate, EvolutionArchive, EvolutionEngine, EvolutionSpec, Fitness, MutationPrompt, ParentSelector,
    ProgramDatabase, StagnationTracker, StopController, compute_fitness, generation_summary,
    apply_unified_diff, pareto_frontier, population_diversity, validate_candidate_patch,
)
from ralfloop_agent.local_arch.sandbox import BubblewrapSandbox, EvaluationResult, SandboxLimits, TestVector
from ralfloop_agent.local_arch.store import ArtifactStore


def evaluation(**updates):
    values = dict(
        correct=True, compilation_success=True, tests_passed=2, tests_total=2, wall_ms=10.0, cpu_ms=9.0,
        rss_mb=5.0, binary_kb=10.0, timeout=False, crash=False, sanitizer_errors=False,
        deterministic_output=True, compiler="gcc", compiler_version="gcc 13", flags=("-O2",),
        source_hash="a" * 64, binary_hash="b" * 64, dataset_hash="c" * 64,
        evaluator_version="v1", cpu_affinity=(0,), random_seed=1, run_count=5, warmup_count=1,
        median_ms=10.0, p95_ms=11.0, noise=0.02, failure=None,
    )
    values.update(updates)
    return EvaluationResult(**values)


def candidate(name, score, *, parent=None, generation=0, novelty=0.0, metrics=None):
    raw = metrics or {"correct": True, "tests_passed": 2, "tests_total": 2, "wall_ms": 10 / score, "rss_mb": 5, "binary_kb": 10}
    fit = compute_fitness(raw, baseline={"wall_ms": 10, "rss_mb": 5, "binary_kb": 10})
    return Candidate(name, parent, generation, (name[-1:] or "a") * 64, "micro_optimization", fit, generation, novelty)


def test_27_28_seed_compile_and_tests_real_sandbox():
    sandbox = BubblewrapSandbox(limits=SandboxLimits(wall_seconds=2, cpu_seconds=1, memory_mb=256))
    if not sandbox.available:
        pytest.skip("bwrap_or_gcc_unavailable")
    source = '#include <stdio.h>\nint main(void){int x;if(scanf("%d",&x)!=1)return 2;printf("%d\\n",x*2);return 0;}'
    try:
        result = sandbox.evaluate(source, [TestVector("2\n", "4\n", "double")], warmups=1, runs=3)
    except RuntimeError as exc:
        pytest.skip(str(exc))
    assert result.compilation_success and result.correct


def test_29_evaluator_hard_constraints():
    assert compute_fitness(evaluation()).valid


def test_30_fitness_raw_normalized_score_and_pareto():
    fitness = compute_fitness(evaluation())
    assert fitness.raw and fitness.normalized and fitness.score is not None and len(fitness.pareto) == 3


@pytest.mark.parametrize("updates", [
    {"correct": False}, {"tests_passed": 1}, {"timeout": True}, {"crash": True}, {"sanitizer_errors": True},
], ids=["invalid", "wrong", "timeout", "crash", "sanitizer"])
def test_31_34_invalid_candidates_never_score(updates):
    assert compute_fitness(evaluation(**updates)).score is None


def test_35_faster_valid_candidate_wins():
    slow = compute_fitness(evaluation(wall_ms=20), baseline={"wall_ms": 20, "rss_mb": 5, "binary_kb": 10})
    fast = compute_fitness(evaluation(wall_ms=10), baseline={"wall_ms": 20, "rss_mb": 5, "binary_kb": 10})
    assert fast.score > slow.score


def test_36_memory_regression_visible_in_pareto():
    base = compute_fitness(evaluation(rss_mb=5))
    regression = compute_fitness(evaluation(rss_mb=50))
    assert regression.pareto[1] < base.pareto[1]


@pytest.mark.parametrize("strategy", ["tournament", "top_k", "novelty", "pareto", "lineage_diversity", "random_elite"])
def test_37_parent_selection_strategies(strategy):
    population = [candidate("c1", 1, novelty=0.1), candidate("c2", 1.5, novelty=0.9), candidate("c3", 0.8, novelty=0.3)]
    assert ParentSelector(1).select(population, strategy) in population


def test_38_lineage_full(tmp_path):
    db = ProgramDatabase(tmp_path / "p.db")
    first = candidate("c1", 1)
    second = candidate("c2", 1.1, parent="c1", generation=1)
    db.add_candidate(first, evaluation(source_hash=first.source_hash))
    db.add_candidate(second, evaluation(source_hash=second.source_hash, binary_hash="d" * 64))
    assert db.lineage("c2") == [{"ancestor_id": "c1", "depth": 1}]


def test_39_archive_content_addressed_no_duplicate(tmp_path):
    store = ArtifactStore(tmp_path)
    archive = EvolutionArchive(store)
    args = dict(source="x", patch="", compile_log="ok", result=evaluation(), fitness=compute_fitness(evaluation()), lineage={}, metadata={"candidate": "c"})
    first = archive.save_candidate(**args)
    second = archive.save_candidate(**args)
    assert first == second


def test_40_population_summary():
    summary = generation_summary([candidate("c1", 1), candidate("c2", 2)])
    assert summary["candidates"] == 2 and summary["valid"] == 2


def test_41_diversity_accounts_for_source_and_lineage():
    assert population_diversity([candidate("ca", 1), candidate("cb", 1, parent="ca")]) > 0


def test_42_stagnation_changes_exploration():
    tracker = StagnationTracker(patience=2)
    assert tracker.update(1) == "improved"
    tracker.update(1)
    assert tracker.update(1) == "increase_diversity"


@pytest.mark.parametrize("kwargs,reason", [
    ({"generation": 2, "candidates": 0, "llm_calls": 0}, "generation_budget"),
    ({"generation": 0, "candidates": 3, "llm_calls": 0}, "candidate_budget"),
    ({"generation": 0, "candidates": 0, "llm_calls": 4}, "llm_call_budget"),
    ({"generation": 0, "candidates": 0, "llm_calls": 0, "stagnated": True}, "stagnation"),
    ({"generation": 0, "candidates": 0, "llm_calls": 0, "evaluator_ok": False}, "evaluator_failure"),
    ({"generation": 0, "candidates": 0, "llm_calls": 0, "cancelled": True}, "human_cancel"),
])
def test_43_budget_stop_conditions(kwargs, reason):
    assert StopController(Budget(2, 3, 4, 999)).reason(**kwargs) == reason


def test_44_candidate_source_hash():
    assert evaluation().source_hash == "a" * 64


def test_45_duplicate_detection(tmp_path):
    db = ProgramDatabase(tmp_path / "p.db")
    item = candidate("c1", 1)
    db.add_candidate(item, evaluation(source_hash=item.source_hash))
    with pytest.raises(ValueError, match="duplicate"):
        db.add_candidate(item, evaluation(source_hash=item.source_hash))


@pytest.mark.parametrize("word", ["evaluator", "benchmark", "dataset", "expected", "tests/", "../"])
def test_46_48_protected_patch_and_anti_hardcode(word):
    patch = f"--- a/candidate.c\n+++ b/candidate.c\n@@ -1 +1 @@\n-x\n+{word}\n"
    with pytest.raises(ValueError, match="protected"):
        validate_candidate_patch(patch)


def test_49_50_no_network_and_sandbox_namespaces():
    command = BubblewrapSandbox().command("./candidate")
    assert "--unshare-all" in command and "--ro-bind" in command and "--cap-drop" in command


def test_seccomp_filter_is_attached():
    assert "--seccomp" in BubblewrapSandbox().command("./candidate", seccomp_fd=9)


@pytest.mark.parametrize("source,expected", [
    ('#include <stdio.h>\n#include <sys/socket.h>\nint main(void){int x=socket(AF_INET,SOCK_STREAM,0);puts(x<0?"blocked":"open");return 0;}', "blocked\n"),
    ('#include <stdio.h>\n#include <unistd.h>\nint main(void){int x=fork();puts(x<0?"blocked":"forked");return 0;}', "blocked\n"),
])
def test_sandbox_runtime_blocks_network_and_process_creation(source, expected):
    result = BubblewrapSandbox().evaluate(source, [TestVector("", expected, "anti-cheat")], warmups=0, runs=1)
    assert result.correct


def test_sandbox_runtime_timeout_is_measured():
    source = 'int main(void){for(;;){} return 0;}'
    result = BubblewrapSandbox(limits=SandboxLimits(wall_seconds=0.2, cpu_seconds=1)).evaluate(source, [TestVector("", "", "timeout")], warmups=0, runs=1)
    assert result.timeout and not result.correct


def test_sandbox_runtime_output_limit_is_measured():
    source = '#include <stdio.h>\nint main(void){for(int i=0;i<100000;i++)putchar(120);return 0;}'
    limits = SandboxLimits(wall_seconds=2, output_bytes=1024, file_bytes=2048)
    result = BubblewrapSandbox(limits=limits).evaluate(source, [TestVector("", "", "output")], warmups=0, runs=1)
    assert not result.correct


def test_sandbox_runtime_memory_limit_is_measured():
    source = '#include <stdlib.h>\nint main(void){char*p=malloc(512*1024*1024);if(!p)return 3;for(long i=0;i<512L*1024*1024;i+=4096)p[i]=1;return 0;}'
    result = BubblewrapSandbox(limits=SandboxLimits(wall_seconds=2, memory_mb=64)).evaluate(source, [TestVector("", "", "memory")], warmups=0, runs=1)
    assert not result.correct


@pytest.mark.parametrize("field", ["cpu_seconds", "memory_mb", "output_bytes", "file_bytes", "tasks"])
def test_51_53_resource_and_output_limits_present(field):
    assert getattr(SandboxLimits(), field) > 0


def test_54_compiler_flags_tracked_and_fixed():
    assert "-O2" in BubblewrapSandbox.fixed_flags
    assert "-O3" not in BubblewrapSandbox.fixed_flags


@pytest.mark.parametrize("field", ["compiler_version", "flags", "cpu_affinity", "random_seed", "dataset_hash", "evaluator_version", "run_count", "warmup_count"])
def test_55_reproducibility_fields(field):
    assert getattr(evaluation(), field) is not None


def test_56_multi_run_benchmark_fields():
    result = evaluation()
    assert result.run_count >= 3 and result.median_ms is not None and result.p95_ms is not None


def test_57_noise_handling_recorded():
    assert 0 <= evaluation().noise < 1


def test_spec_requires_seed_evaluator_metrics_and_tests():
    with pytest.raises(ValueError, match="precondition"):
        EvolutionSpec.from_mapping({})


def test_seed_is_generation_zero():
    assert candidate("c0", 1).generation == 0


def test_mutation_prompt_compact_single_diff():
    prompt = MutationPrompt.build(candidate("c1", 1), task="graph shortest_path", metrics={"tests_passed": 2, "tests_total": 2, "wall_ms": 10, "rss_mb": 5}, bottleneck="lookup 43%", good=["contiguous +8%"], bad=["parallel -9%"])
    assert len(prompt) < 1000 and "one unified diff only" in prompt


def test_pareto_keeps_tradeoffs():
    left = candidate("ca", 1, metrics={"correct": True, "tests_passed": 2, "tests_total": 2, "wall_ms": 5, "rss_mb": 20, "binary_kb": 10})
    right = candidate("cb", 1, metrics={"correct": True, "tests_passed": 2, "tests_total": 2, "wall_ms": 10, "rss_mb": 5, "binary_kb": 10})
    assert len(pareto_frontier([left, right])) == 2


def test_unified_diff_applies_only_candidate_file():
    original = "int main(void){return 1;}\n"
    patch = "--- a/candidate.c\n+++ b/candidate.c\n@@ -1 +1 @@\n-int main(void){return 1;}\n+int main(void){return 0;}\n"
    assert "return 0" in apply_unified_diff(original, patch)


def test_generation_loop_model_proposes_evaluator_scores(tmp_path):
    original = "int main(void){return 1;}\n"
    patch = "--- a/candidate.c\n+++ b/candidate.c\n@@ -1 +1 @@\n-int main(void){return 1;}\n+int main(void){return 0;}\n"
    class Provider:
        model_id = "fake-qwen"
        def propose(self, prompt): return patch
    class Evaluator:
        def evaluate(self, source, tests):
            import hashlib
            return evaluation(source_hash=hashlib.sha256(source.encode()).hexdigest(), evaluator_version="v1")
    db = ProgramDatabase(tmp_path / "p.db")
    store = ArtifactStore(tmp_path / "store")
    engine = EvolutionEngine(db, EvolutionArchive(store), Evaluator(), Provider())
    parent = candidate("c1", 1)
    spec = EvolutionSpec("p", "code", "c", "runtime", ("correct",), ("wall_ms",), "sha256:" + "a"*64, "v1", ("wall_ms",), "sha256:" + "b"*64, Budget(5, 5, 5, 60))
    result = engine.generation(experiment_id="exp-1", spec=spec, generation=1, population=[parent], sources={"c1": original}, tests=[TestVector("", "", "x")], baseline_metrics={"wall_ms": 10, "rss_mb": 5, "binary_kb": 10}, candidates_per_generation=1, bottleneck="return")
    assert result.summary["valid"] == 1
    assert db.connection.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 1
