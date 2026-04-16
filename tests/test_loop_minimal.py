from pathlib import Path
import json
from typing import Any

from ralfloop_agent.adapters.openshell_adapter import OpenShellAdapterStub
from ralfloop_agent.core.decisions import ActionDecision
from ralfloop_agent.core.loop import RalfloopAgent
from ralfloop_agent.logging.audit import AuditLogger
from ralfloop_agent.providers.ollama import DeterministicPlanner


class SequencePlanner:
    def __init__(self, decisions: list[ActionDecision], write_pairs: list[tuple[str, str]]) -> None:
        self._decisions = decisions
        self._write_pairs = write_pairs

    def choose_next_action(self, user_goal: str, iteration: int) -> ActionDecision:
        if not self._decisions:
            raise AssertionError(f"planner exhausted for goal: {user_goal}")
        return self._decisions.pop(0)

    def _extract_write_pairs(self, goal: str) -> list[tuple[str, str]]:
        return list(self._write_pairs)


class RepeatingReadPlanner:
    def __init__(self, write_pairs: list[tuple[str, str]]) -> None:
        self._write_pairs = write_pairs

    def choose_next_action(self, user_goal: str, iteration: int) -> ActionDecision:
        if iteration < len(self._write_pairs):
            path, content = self._write_pairs[iteration]
            return ActionDecision(
                tool_name="sandbox_write_file",
                tool_input={"path": path, "content": content},
                why=f"Scrivo {path}",
            )
        first_path, _ = self._write_pairs[0]
        return ActionDecision(
            tool_name="sandbox_read_file",
            tool_input={"path": first_path},
            why=f"Rileggo sempre {first_path}",
        )

    def _extract_write_pairs(self, goal: str) -> list[tuple[str, str]]:
        return [(path, content.rstrip("\n")) for path, content in self._write_pairs]


class KeepSandboxAdapter(OpenShellAdapterStub):
    def destroy_sandbox(self, sandbox_id: str) -> None:
        _ = sandbox_id


def _decision(tool_name: str, tool_input: dict[str, Any], why: str = "test") -> ActionDecision:
    return ActionDecision(tool_name=tool_name, tool_input=tool_input, why=why)


def test_loop_execs_and_reads_file(tmp_path: Path) -> None:
    adapter = OpenShellAdapterStub(base_dir=str(tmp_path / ".sandbox"))
    logger = AuditLogger(store_path=str(tmp_path / "logs"))
    planner = DeterministicPlanner()
    agent = RalfloopAgent(adapter=adapter, planner=planner, logger=logger)

    state = agent.run("Scrivi hello in un file e verifica il contenuto")

    assert state.status == "completed"
    assert state.stop_reason == "goal_completed"
    assert state.last_result is not None
    assert "hello from sandbox_exec" in state.last_result.stdout
    assert (tmp_path / "logs" / "audit.jsonl").exists()

    audit = (tmp_path / "logs" / "audit.jsonl").read_text(encoding="utf-8")
    assert "sandbox_exec" in audit
    assert "sandbox_read_file" in audit


def test_operational_goal_rejects_code_imitation_and_returns_real_two_file_contents(tmp_path: Path) -> None:
    adapter = OpenShellAdapterStub(base_dir=str(tmp_path / ".sandbox"))
    logger = AuditLogger(store_path=str(tmp_path / "logs"))
    planner = SequencePlanner(
        decisions=[
            _decision(
                "sandbox_write_file",
                {
                    "path": "out/spesa.txt",
                    "content": 'with open("out/spesa.txt", "w") as f:\n    f.write("latte, pane, pomodori")\n',
                },
            ),
            _decision(
                "sandbox_write_file",
                {
                    "path": "out/note.txt",
                    "content": "cat > out/note.txt <<'EOF'\nricordati di chiamare Sonia\nEOF\n",
                },
            ),
            _decision("sandbox_read_file", {"path": "out/spesa.txt"}),
            _decision("sandbox_read_file", {"path": "out/note.txt"}),
            _decision(
                "sandbox_write_file",
                {"path": "out/spesa.txt", "content": "latte, pane, pomodori\n"},
            ),
            _decision(
                "sandbox_write_file",
                {"path": "out/note.txt", "content": "ricordati di chiamare Sonia\n"},
            ),
            _decision("sandbox_read_file", {"path": "out/spesa.txt"}),
            _decision("sandbox_read_file", {"path": "out/note.txt"}),
        ],
        write_pairs=[
            ("out/spesa.txt", "latte, pane, pomodori"),
            ("out/note.txt", "ricordati di chiamare Sonia"),
        ],
    )
    agent = RalfloopAgent(adapter=adapter, planner=planner, logger=logger)

    state = agent.run("Scrivi due file out/spesa.txt e out/note.txt, poi leggili entrambi")

    assert state.status == "completed"
    assert state.stop_reason == "goal_completed"
    assert state.final_answer is not None
    assert "latte, pane, pomodori" in state.final_answer
    assert "ricordati di chiamare Sonia" in state.final_answer
    assert "with open(" not in state.final_answer
    assert "cat >" not in state.final_answer


def test_operational_goal_variant_is_not_phrase_hardcoded(tmp_path: Path) -> None:
    adapter = OpenShellAdapterStub(base_dir=str(tmp_path / ".sandbox"))
    logger = AuditLogger(store_path=str(tmp_path / "logs"))
    planner = SequencePlanner(
        decisions=[
            _decision("sandbox_write_file", {"path": "out/a.txt", "content": 'print("alpha")\n'}),
            _decision(
                "sandbox_write_file",
                {
                    "path": "out/b.txt",
                    "content": 'with open("out/b.txt", "w") as f:\n    f.write("beta")\n',
                },
            ),
            _decision("sandbox_read_file", {"path": "out/a.txt"}),
            _decision("sandbox_read_file", {"path": "out/b.txt"}),
            _decision("sandbox_write_file", {"path": "out/a.txt", "content": "alpha vero\n"}),
            _decision("sandbox_write_file", {"path": "out/b.txt", "content": "beta vero\n"}),
            _decision("sandbox_read_file", {"path": "out/a.txt"}),
            _decision("sandbox_read_file", {"path": "out/b.txt"}),
        ],
        write_pairs=[("out/a.txt", "alpha vero"), ("out/b.txt", "beta vero")],
    )
    agent = RalfloopAgent(adapter=adapter, planner=planner, logger=logger)

    state = agent.run("Crea due file out/a.txt e out/b.txt con contenuti diversi, poi leggili entrambi")

    assert state.status == "completed"
    assert state.stop_reason == "goal_completed"
    assert state.final_answer is not None
    assert "alpha vero" in state.final_answer
    assert "beta vero" in state.final_answer
    assert "print(" not in state.final_answer
    assert "with open(" not in state.final_answer


def test_multi_file_progression_uses_memory_to_avoid_repeat_read_loop(tmp_path: Path) -> None:
    adapter = OpenShellAdapterStub(base_dir=str(tmp_path / ".sandbox"))
    logger = AuditLogger(store_path=str(tmp_path / "logs"))
    planner = RepeatingReadPlanner(
        write_pairs=[
            ("out/spesa.txt", "latte, pane, pomodori\n"),
            ("out/note.txt", "ricordati di chiamare Sonia\n"),
        ]
    )
    agent = RalfloopAgent(adapter=adapter, planner=planner, logger=logger)

    state = agent.run("Scrivi due file out/spesa.txt e out/note.txt, poi leggili entrambi e dimmi il contenuto finale.")

    assert state.status == "completed"
    assert state.stop_reason == "goal_completed"
    assert state.final_answer is not None
    assert "latte, pane, pomodori" in state.final_answer
    assert "ricordati di chiamare Sonia" in state.final_answer
    read_actions = [
        item["tool_input"]["path"]
        for item in state.action_history
        if item["tool_name"] == "sandbox_read_file"
    ]
    assert read_actions == ["out/spesa.txt", "out/note.txt"]


def test_multi_file_progression_variant_uses_next_unread_path(tmp_path: Path) -> None:
    adapter = OpenShellAdapterStub(base_dir=str(tmp_path / ".sandbox"))
    logger = AuditLogger(store_path=str(tmp_path / "logs"))
    planner = RepeatingReadPlanner(
        write_pairs=[
            ("out/a.txt", "alpha vero\n"),
            ("out/b.txt", "beta vero\n"),
        ]
    )
    agent = RalfloopAgent(adapter=adapter, planner=planner, logger=logger)

    state = agent.run("Crea due file out/a.txt e out/b.txt con contenuti diversi, poi leggili entrambi")

    assert state.status == "completed"
    assert state.stop_reason == "goal_completed"
    assert state.final_answer is not None
    assert "alpha vero" in state.final_answer
    assert "beta vero" in state.final_answer
    read_actions = [
        item["tool_input"]["path"]
        for item in state.action_history
        if item["tool_name"] == "sandbox_read_file"
    ]
    assert read_actions == ["out/a.txt", "out/b.txt"]


def test_runtime_like_case_one_passes_with_real_deterministic_planner(tmp_path: Path) -> None:
    adapter = OpenShellAdapterStub(base_dir=str(tmp_path / ".sandbox"))
    logger = AuditLogger(store_path=str(tmp_path / "logs"))
    planner = DeterministicPlanner()
    agent = RalfloopAgent(adapter=adapter, planner=planner, logger=logger)

    goal = 'Scrivi due file: out/spesa.txt con "latte, pane, pomodori" e out/note.txt con "ricordati di chiamare Sonia". Poi leggili entrambi e dimmi il contenuto finale.'
    state = agent.run(goal)

    assert state.status == "completed"
    assert state.stop_reason == "goal_completed"
    assert state.final_answer is not None
    assert "latte, pane, pomodori" in state.final_answer
    assert "ricordati di chiamare Sonia" in state.final_answer


def test_runtime_like_case_two_passes_with_real_deterministic_planner(tmp_path: Path) -> None:
    adapter = OpenShellAdapterStub(base_dir=str(tmp_path / ".sandbox"))
    logger = AuditLogger(store_path=str(tmp_path / "logs"))
    planner = DeterministicPlanner()
    agent = RalfloopAgent(adapter=adapter, planner=planner, logger=logger)

    goal = "Crea due file out/a.txt e out/b.txt con contenuti diversi, poi leggili entrambi e dimmi il contenuto finale."
    state = agent.run(goal)

    assert state.status == "completed"
    assert state.stop_reason == "goal_completed"
    assert state.final_answer is not None
    assert "out/a.txt" in state.final_answer
    assert "out/b.txt" in state.final_answer
    assert "contenuto a" in state.final_answer
    assert "contenuto b" in state.final_answer


def test_runtime_writes_temp_scorecard_and_session_summary_as_artifacts(tmp_path: Path) -> None:
    adapter = KeepSandboxAdapter(base_dir=str(tmp_path / ".sandbox"))
    logger = AuditLogger(store_path=str(tmp_path / "logs"))
    planner = DeterministicPlanner()
    agent = RalfloopAgent(adapter=adapter, planner=planner, logger=logger)

    state = agent.run("Scrivi hello in un file e verifica il contenuto")

    artifact_names = {Path(path).name for path in state.artifacts}
    assert "run_scorecard.json" in artifact_names
    assert "session_summary.json" in artifact_names
    assert state.last_result is not None
    last_artifact_names = {Path(path).name for path in state.last_result.artifacts}
    assert "run_scorecard.json" in last_artifact_names
    assert "session_summary.json" in last_artifact_names

    sandbox_root = tmp_path / ".sandbox"
    scorecards = list(sandbox_root.glob("*/workspace/tmp/debug/run_scorecard.json"))
    summaries = list(sandbox_root.glob("*/workspace/tmp/debug/session_summary.json"))
    assert len(scorecards) == 1
    assert len(summaries) == 1

    scorecard = json.loads(scorecards[0].read_text(encoding="utf-8"))
    session_summary = json.loads(summaries[0].read_text(encoding="utf-8"))

    assert scorecard["goal"]["user_goal"] == "Scrivi hello in un file e verifica il contenuto"
    assert scorecard["promotion_decision"] == {
        "decision": "keep_temp",
        "reason": "successful evidence-backed run",
        "temporary_only": True,
        "rag_ingest": False,
        "vectorize": False,
        "stable_memory_write": False,
        "manuals_write": False,
    }
    assert session_summary["user_goal"] == "Scrivi hello in un file e verifica il contenuto"
    assert session_summary["stop_reason"] == "goal_completed"
    assert session_summary["read_files"]
    assert any(path.endswith("run_scorecard.json") for path in scorecard["evidence"]["artifacts"])
