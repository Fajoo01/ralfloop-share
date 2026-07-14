import json

from ralfloop_agent.inference_lab.recursive_codex_benchmark import (
    AgentBenchmark,
    AgentTask,
    CodexSequentialRunner,
    load_agent_dataset,
)


def test_dataset_has_twelve_isolated_tasks():
    path = "ralfloop_agent/inference_lab/data/recursive_codex_tasks.json"
    tasks = load_agent_dataset(path)
    assert len(tasks) == 12
    assert len({task.id for task in tasks}) == 12


def test_candidate_isolation(tmp_path):
    task = AgentTask("x", "repository", "p", 10, {"input.txt": "fixture"}, {"type": "exact_output", "expected": "ok"})
    bench = AgentBenchmark(tmp_path)
    one = bench.run_candidate("single_qwen_7b", [task], lambda task, root: {"output": "ok"})
    two = bench.run_candidate("recursive_mas_native", [task], lambda task, root: {"output": "ok"})
    assert one["pass_rate"] == 1.0 == two["pass_rate"]
    assert (tmp_path / "single_qwen_7b" / "x" / "input.txt").is_file()
    assert (tmp_path / "recursive_mas_native" / "x" / "input.txt").is_file()


def test_codex_runner_disabled_in_tests(tmp_path):
    runner = CodexSequentialRunner(enabled=False)
    task = AgentTask("x", "repository", "p", 1, {}, {"type": "exact_output", "expected": ""})
    try:
        runner(task, tmp_path)
    except RuntimeError as exc:
        assert str(exc) == "codex_runner_disabled"
    else:
        raise AssertionError("Codex executed in unit test")


def test_codex_empty_output_is_not_scored(monkeypatch, tmp_path):
    class Completed:
        returncode = 0
        stdout = ""

    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: Completed())
    runner = CodexSequentialRunner(enabled=True)
    task = AgentTask("x", "repository", "p", 1, {}, {"type": "exact_output", "expected": ""})
    try:
        runner(task, tmp_path)
    except RuntimeError as exc:
        assert str(exc) == "codex_empty_output"
    else:
        raise AssertionError("empty Codex output accepted")


def test_safety_violation_fails_candidate(tmp_path):
    task = AgentTask("x", "safety", "p", 10, {}, {"type": "contains_all", "expected": ["refuse"], "forbidden": []})
    result = AgentBenchmark(tmp_path).run_candidate("single_qwen_7b", [task], lambda *_: {"output": "refuse auto-approve"})
    assert result["pass_rate"] == 0.0
    assert result["safety_violations"] == 1
