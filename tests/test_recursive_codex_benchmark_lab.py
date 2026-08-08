import json
from pathlib import Path
import threading

import pytest

from ralfloop_agent.inference_lab.recursive_codex_benchmark import (
    AgentBenchmark,
    AgentTask,
    CodexSequentialRunner,
    build_comparison,
    classify_codex_result,
    dataset_hash,
    fixture_hash,
    load_agent_dataset,
    parse_codex_output,
    run_codex_canary,
    timing_is_valid,
)


def task(scorer=None, prompt="p"):
    return AgentTask("x", "repository", prompt, 10, {"input.txt": "fixture"}, scorer or {"type": "exact_output", "expected": "ok"})


def capture(*, stdout="", stderr="", returncode=0, timed_out=False, wall_ms=10.0, ttft_ms=None, mutate=None):
    def run(argv, prompt, cwd, timeout):
        if mutate:
            mutate(Path(cwd))
        return {"stdout": stdout, "stderr": stderr, "returncode": returncode, "timed_out": timed_out, "wall_ms": wall_ms, "ttft_ms": ttft_ms}
    return run


def test_dataset_has_twelve_isolated_tasks():
    path = "ralfloop_agent/inference_lab/data/recursive_codex_tasks.json"
    tasks = load_agent_dataset(path)
    assert len(tasks) == 12
    assert len({item.id for item in tasks}) == 12
    assert len(dataset_hash(path)) == 64
    assert all(len(fixture_hash(item)) == 64 for item in tasks)


def test_candidate_isolation(tmp_path):
    item = task()
    bench = AgentBenchmark(tmp_path)
    one = bench.run_candidate("single_qwen_7b", [item], lambda *_: {"output": "ok"})
    two = bench.run_candidate("recursive_mas_native", [item], lambda *_: {"output": "ok"})
    assert one["pass_rate"] == 1.0 == two["pass_rate"]
    assert (tmp_path / "single_qwen_7b" / "x" / "input.txt").is_file()
    assert (tmp_path / "recursive_mas_native" / "x" / "input.txt").is_file()


def test_parser_stdout_text():
    assert parse_codex_output(stdout="answer", stderr="")["source"] == "stdout_text"


def test_parser_json():
    parsed = parse_codex_output(stdout='{"output":"answer"}', stderr="")
    assert parsed["output"] == "answer"
    assert parsed["source"] in {"stdout_json", "stdout_jsonl"}


def test_parser_jsonl_final_agent_message():
    stream = '\n'.join((json.dumps({"type": "thread.started"}), json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "final"}})))
    parsed = parse_codex_output(stdout=stream, stderr="")
    assert parsed["output"] == "final"
    assert parsed["source"] == "stdout_jsonl"


def test_parser_stderr_response():
    assert parse_codex_output(stdout="", stderr="answer")["source"] == "stderr"


def test_parser_result_file():
    parsed = parse_codex_output(stdout="", stderr="", result_file='{"ok":true}')
    assert parsed["source"] == "result_file"


def test_parser_diff_evidence():
    parsed = parse_codex_output(stdout="", stderr="", diff_text="diff --git")
    assert parsed["source"] == "diff"
    assert parsed["has_diff"] is True


def test_code_task_can_be_scored_from_diff(tmp_path):
    item = AgentTask("fix", "code_fix", "fix", 10, {"calc.py": "def add(a, b):\n return a - b\n", "test_calc.py": "from calc import add\ndef test_add(): assert add(2,3)==5\n"}, {"type": "pytest"})
    def mutate(root):
        (root / "calc.py").write_text("def add(a, b):\n return a + b\n")
    runner = CodexSequentialRunner(enabled=True, process_runner=capture(mutate=mutate))
    result = AgentBenchmark(tmp_path).run_candidate("codex", [item], runner)
    assert result["success_count"] == 1


def test_timeout_preserves_artifacts(tmp_path):
    runner = CodexSequentialRunner(enabled=True, process_runner=capture(stdout="partial", stderr="timeout", returncode=124, timed_out=True))
    result = runner(task(), tmp_path / "fixture")
    artifact = Path(result["artifact_dir"])
    assert result["classification"] == "infrastructure_error"
    assert (artifact / "stdout.raw").read_text() == "partial"
    assert (artifact / "stderr.raw").read_text() == "timeout"
    assert (artifact / "exit_code.txt").read_text().strip() == "124"


def test_nonzero_exit_with_output_is_task_failure(tmp_path):
    runner = CodexSequentialRunner(enabled=True, process_runner=capture(stdout="answer", returncode=2))
    result = runner(task(), tmp_path / "fixture")
    assert result["classification"] == "task_failure"


def test_spawn_error_still_writes_artifacts(tmp_path):
    def broken(*args):
        raise FileNotFoundError("codex")
    runner = CodexSequentialRunner(enabled=True, process_runner=broken)
    result = runner(task(), tmp_path / "fixture")
    assert result["classification"] == "infrastructure_error"
    assert (Path(result["artifact_dir"]) / "stderr.raw").is_file()


def test_authentication_is_infrastructure_error():
    parsed = parse_codex_output(stdout='{"type":"error","message":"401 Unauthorized"}', stderr="")
    classification, reason = classify_codex_result(returncode=1, timed_out=False, parsed=parsed, stdout="401 Unauthorized", stderr="")
    assert classification == "infrastructure_error"
    assert reason == "401_unauthorized"


def test_task_failure_is_scored(tmp_path):
    runner = CodexSequentialRunner(enabled=True, process_runner=capture(stdout="wrong"))
    result = AgentBenchmark(tmp_path).run_candidate("codex", [task()], runner)
    assert result["evaluable_count"] == 1
    assert result["failure_count"] == 1
    assert result["infrastructure_errors"] == 0


def test_canary_uses_result_file_and_does_not_count(tmp_path):
    def mutate(root):
        (root / "result.json").write_text('{"ok":true,"runner":"codex"}')
    runner = CodexSequentialRunner(enabled=True, process_runner=capture(mutate=mutate))
    result = run_codex_canary(runner, tmp_path / "canary")
    assert result["passed"] is True
    assert runner.calls == 0
    assert result["counts_toward_dataset_limit"] is False


def test_fixture_isolation_and_foreign_file_detection(tmp_path):
    def mutate(root):
        (root / "unexpected.txt").write_text("x")
    runner = CodexSequentialRunner(enabled=True, process_runner=capture(stdout="ok", mutate=mutate))
    result = runner(task(), tmp_path / "fixture")
    assert result["foreign_files"] == ["unexpected.txt"]
    assert not (tmp_path / "unexpected.txt").exists()


def test_foreign_file_forces_task_failure(tmp_path):
    def mutate(root):
        (root / "unexpected.txt").write_text("x")
    runner = CodexSequentialRunner(enabled=True, process_runner=capture(stdout="ok", mutate=mutate))
    result = AgentBenchmark(tmp_path).run_candidate("codex", [task()], runner)
    assert result["failure_count"] == 1


def test_command_disables_interaction_without_bypass(tmp_path):
    seen = {}
    def inspect(argv, prompt, cwd, timeout):
        seen["argv"] = argv
        return capture(stdout="ok")(argv, prompt, cwd, timeout)
    runner = CodexSequentialRunner(enabled=True, process_runner=inspect)
    runner(task(), tmp_path / "fixture")
    assert seen["argv"][:4] == ["codex", "--ask-for-approval", "never", "exec"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in seen["argv"]


def test_no_concurrent_codex(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    def slow(argv, prompt, cwd, timeout):
        entered.set(); release.wait(2)
        return capture(stdout="ok")(argv, prompt, cwd, timeout)
    first = CodexSequentialRunner(enabled=True, process_runner=slow)
    second = CodexSequentialRunner(enabled=True, process_runner=capture(stdout="ok"))
    thread = threading.Thread(target=lambda: first(task(), tmp_path / "one"))
    thread.start(); assert entered.wait(1)
    with pytest.raises(RuntimeError, match="codex_concurrent_run_forbidden"):
        second(task(), tmp_path / "two")
    release.set(); thread.join()


def test_maximum_twelve_codex_runs(tmp_path):
    runner = CodexSequentialRunner(enabled=True, process_runner=capture(stdout="ok"))
    for index in range(12):
        runner(task(), tmp_path / f"fixture-{index}")
    with pytest.raises(RuntimeError, match="codex_run_limit_exceeded"):
        runner(task(), tmp_path / "fixture-13")


def test_timing_ttft_must_not_exceed_wall():
    assert timing_is_valid(49.313, 4155.704)
    assert timing_is_valid(None, 4155.704)
    assert not timing_is_valid(4155.705, 4155.704)
    encoded = json.dumps({"ttft_ms": 49.313, "wall_ms": 4155.704})
    assert "49.313" in encoded and "4155.704" in encoded
    assert "49,313" not in encoded and "4155,704" not in encoded


def test_invalid_timing_becomes_infrastructure_error(tmp_path):
    runner = CodexSequentialRunner(enabled=True, process_runner=capture(stdout="ok", wall_ms=10, ttft_ms=11))
    result = runner(task(), tmp_path / "fixture")
    assert result["classification"] == "infrastructure_error"
    assert result["reason"] == "invalid_timing_ttft_gt_wall"


def test_codex_runner_disabled_in_tests(tmp_path):
    runner = CodexSequentialRunner(enabled=False)
    with pytest.raises(RuntimeError, match="codex_runner_disabled"):
        runner(task(), tmp_path)


def test_safety_violation_fails_candidate(tmp_path):
    item = AgentTask("x", "safety", "p", 10, {}, {"type": "contains_all", "expected": ["refuse"], "forbidden": []})
    result = AgentBenchmark(tmp_path).run_candidate("single_qwen_7b", [item], lambda *_: {"output": "refuse auto-approve"})
    assert result["pass_rate"] == 0.0
    assert result["safety_violations"] == 1


def test_comparison_keeps_codex_na_and_not_judge(tmp_path):
    run = tmp_path / "run"; run.mkdir()
    row = {"task_count": 1, "safety_violations": 0, "results": [{"passed": True, "error": None, "duration_ms": 12.5}]}
    for name in ("single_qwen_7b", "single_qwen_7b_with_domains", "recursive_mas_native", "recursive_mas_text_hybrid"):
        (run / f"{name}.json").write_text(json.dumps(row))
    codex = {"task_requested": 12, "task_evaluable": 0, "successes": 0, "failures": 0, "infrastructure_errors": 1, "pass_rate_evaluable": None, "pass_rate_overall": None}
    comparison = build_comparison(run, codex)
    assert comparison["winner"] is None
    assert comparison["codex_is_judge"] is False
    assert comparison["cross_candidate_result_sharing"] is False
