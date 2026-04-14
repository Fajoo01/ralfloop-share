from ralfloop_agent.core.loop import RalfloopAgent
from ralfloop_agent.adapters.openshell_adapter import OpenShellAdapterStub
from ralfloop_agent.logging.audit import AuditLogger
from ralfloop_agent.providers.ollama import OllamaPlanner


def test_subdir_empty_message_mentions_directory() -> None:
    agent = RalfloopAgent(
        adapter=OpenShellAdapterStub(),
        planner=OllamaPlanner(model="qwen2.5:7b"),
        logger=AuditLogger(),
    )

    state = agent.run("Mostrami i file nella cartella out")
    assert state.final_answer is not None
    assert "out" in state.final_answer.lower() or "workspace" in state.final_answer.lower()
