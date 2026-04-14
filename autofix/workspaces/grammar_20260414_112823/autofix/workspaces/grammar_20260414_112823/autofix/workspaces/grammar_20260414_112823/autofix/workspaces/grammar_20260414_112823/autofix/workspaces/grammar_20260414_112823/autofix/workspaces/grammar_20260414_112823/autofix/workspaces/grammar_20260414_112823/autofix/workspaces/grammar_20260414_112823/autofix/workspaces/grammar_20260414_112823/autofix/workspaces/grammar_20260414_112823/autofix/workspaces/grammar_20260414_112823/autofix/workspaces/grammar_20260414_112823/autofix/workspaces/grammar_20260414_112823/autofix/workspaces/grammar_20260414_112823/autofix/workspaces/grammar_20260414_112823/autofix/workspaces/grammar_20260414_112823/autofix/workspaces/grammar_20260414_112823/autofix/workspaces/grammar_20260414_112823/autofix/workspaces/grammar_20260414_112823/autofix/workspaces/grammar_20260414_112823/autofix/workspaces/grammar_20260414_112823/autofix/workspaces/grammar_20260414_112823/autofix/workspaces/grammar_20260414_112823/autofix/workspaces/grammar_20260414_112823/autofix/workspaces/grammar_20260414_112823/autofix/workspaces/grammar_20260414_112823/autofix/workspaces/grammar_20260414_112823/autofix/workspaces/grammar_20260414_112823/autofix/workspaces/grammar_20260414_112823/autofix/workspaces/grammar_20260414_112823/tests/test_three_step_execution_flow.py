from ralfloop_agent.adapters.openshell_adapter import OpenShellAdapterStub
from ralfloop_agent.core.loop import RalfloopAgent
from ralfloop_agent.logging.audit import AuditLogger
from ralfloop_agent.providers.ollama import OllamaPlanner


def test_three_step_goal_executes_until_read_file() -> None:
    agent = RalfloopAgent(
        adapter=OpenShellAdapterStub(),
        planner=OllamaPlanner(model="qwen2.5:7b"),
        logger=AuditLogger(),
    )

    goal = 'Scrivi "ciao mondo" in out/messaggio.txt, mostrami i file nella cartella out e poi leggilo'
    state = agent.run(goal)

    assert state.status == "completed"
    assert state.stop_reason == "goal_completed"
    assert state.last_result is not None
    assert state.last_result.tool_name == "sandbox_read_file"
    assert state.final_answer is not None
    assert "ciao mondo" in state.final_answer
