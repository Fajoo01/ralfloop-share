from ralfloop_agent.adapters.openshell_adapter import OpenShellAdapterStub
from ralfloop_agent.core.loop import RalfloopAgent
from ralfloop_agent.logging.audit import AuditLogger
from ralfloop_agent.providers.ollama import OllamaPlanner


def test_multi_file_goal_runs_until_second_read() -> None:
    agent = RalfloopAgent(
        adapter=OpenShellAdapterStub(),
        planner=OllamaPlanner(model="qwen2.5:7b"),
        logger=AuditLogger(),
    )

    goal = 'Scrivi "uno" in out/a.txt, scrivi "due" in out/b.txt e poi leggili'
    state = agent.run(goal)

    assert state.status == "completed"
    assert state.stop_reason == "goal_completed"
    assert state.last_result is not None
    assert state.last_result.tool_name == "sandbox_read_file"
    assert state.last_action is not None
    assert state.last_action["tool_input"]["path"] == "out/b.txt"
