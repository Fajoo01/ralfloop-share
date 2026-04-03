from pathlib import Path

from ralfloop_agent.adapters.openshell_adapter import OpenShellAdapterStub
from ralfloop_agent.core.loop import RalfloopAgent
from ralfloop_agent.logging.audit import AuditLogger
from ralfloop_agent.providers.ollama import DeterministicPlanner


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
