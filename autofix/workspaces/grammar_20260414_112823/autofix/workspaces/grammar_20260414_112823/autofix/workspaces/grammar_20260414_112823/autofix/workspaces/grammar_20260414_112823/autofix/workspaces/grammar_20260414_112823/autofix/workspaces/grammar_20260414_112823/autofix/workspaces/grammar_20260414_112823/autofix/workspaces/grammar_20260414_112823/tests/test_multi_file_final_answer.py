from ralfloop_agent.adapters.openshell_adapter import OpenShellAdapterStub
from ralfloop_agent.core.loop import RalfloopAgent
from ralfloop_agent.logging.audit import AuditLogger
from ralfloop_agent.providers.ollama import OllamaPlanner


def test_multi_file_final_answer_includes_all_read_contents() -> None:
    agent = RalfloopAgent(
        adapter=OpenShellAdapterStub(),
        planner=OllamaPlanner(model="qwen2.5:7b"),
        logger=AuditLogger(),
    )

    goal = 'Scrivi "uno" in out/a.txt, scrivi "due" in out/b.txt e poi leggili'
    state = agent.run(goal)

    assert state.status == "completed"
    assert state.final_answer is not None
    assert "Contenuto dei file:" in state.final_answer
    assert "out/a.txt" in state.final_answer
    assert "uno" in state.final_answer
    assert "out/b.txt" in state.final_answer
    assert "due" in state.final_answer
