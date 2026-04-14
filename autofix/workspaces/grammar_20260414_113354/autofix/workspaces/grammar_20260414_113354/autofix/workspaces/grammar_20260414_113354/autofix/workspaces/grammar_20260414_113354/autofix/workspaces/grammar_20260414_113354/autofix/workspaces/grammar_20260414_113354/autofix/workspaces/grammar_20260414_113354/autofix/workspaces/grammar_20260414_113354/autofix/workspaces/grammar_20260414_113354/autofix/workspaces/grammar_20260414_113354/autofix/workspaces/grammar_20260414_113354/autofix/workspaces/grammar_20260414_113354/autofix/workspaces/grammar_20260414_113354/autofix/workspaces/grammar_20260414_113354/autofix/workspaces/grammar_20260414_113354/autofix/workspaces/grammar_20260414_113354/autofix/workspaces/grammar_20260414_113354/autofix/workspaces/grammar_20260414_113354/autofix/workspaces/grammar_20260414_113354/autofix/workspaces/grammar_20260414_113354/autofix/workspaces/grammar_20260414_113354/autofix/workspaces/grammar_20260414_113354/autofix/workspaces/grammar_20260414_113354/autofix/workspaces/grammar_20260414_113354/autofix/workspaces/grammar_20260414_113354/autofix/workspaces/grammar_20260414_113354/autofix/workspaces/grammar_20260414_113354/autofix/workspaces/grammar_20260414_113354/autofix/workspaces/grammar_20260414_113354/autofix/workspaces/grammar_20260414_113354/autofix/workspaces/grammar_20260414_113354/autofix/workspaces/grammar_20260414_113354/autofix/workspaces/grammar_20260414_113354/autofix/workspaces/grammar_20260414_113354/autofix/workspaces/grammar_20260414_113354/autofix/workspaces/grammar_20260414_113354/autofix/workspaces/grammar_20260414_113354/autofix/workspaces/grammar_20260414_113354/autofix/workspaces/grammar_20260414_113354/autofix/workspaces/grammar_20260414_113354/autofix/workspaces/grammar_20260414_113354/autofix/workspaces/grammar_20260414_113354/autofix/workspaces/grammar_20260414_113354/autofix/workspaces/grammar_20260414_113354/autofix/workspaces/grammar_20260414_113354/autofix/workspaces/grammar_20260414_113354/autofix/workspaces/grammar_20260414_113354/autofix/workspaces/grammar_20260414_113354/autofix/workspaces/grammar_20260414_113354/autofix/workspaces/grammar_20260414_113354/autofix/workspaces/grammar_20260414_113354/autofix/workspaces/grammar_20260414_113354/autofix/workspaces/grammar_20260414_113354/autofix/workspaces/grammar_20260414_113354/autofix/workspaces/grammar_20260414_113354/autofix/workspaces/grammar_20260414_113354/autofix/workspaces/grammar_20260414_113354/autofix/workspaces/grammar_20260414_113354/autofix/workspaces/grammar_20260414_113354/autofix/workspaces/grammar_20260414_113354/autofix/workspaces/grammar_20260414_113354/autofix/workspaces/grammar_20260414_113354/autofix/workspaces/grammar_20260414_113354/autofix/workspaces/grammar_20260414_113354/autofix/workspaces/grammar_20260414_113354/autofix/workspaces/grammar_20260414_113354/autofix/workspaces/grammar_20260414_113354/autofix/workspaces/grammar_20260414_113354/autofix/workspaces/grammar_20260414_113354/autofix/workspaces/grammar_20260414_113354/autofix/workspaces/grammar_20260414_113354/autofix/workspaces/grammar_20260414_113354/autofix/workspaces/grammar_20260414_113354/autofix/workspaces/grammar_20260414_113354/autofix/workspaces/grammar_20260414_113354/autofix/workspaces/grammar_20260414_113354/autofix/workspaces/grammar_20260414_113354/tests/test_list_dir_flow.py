from ralfloop_agent.adapters.openshell_adapter import OpenShellAdapterStub
from ralfloop_agent.core.loop import RalfloopAgent
from ralfloop_agent.logging.audit import AuditLogger
from ralfloop_agent.providers.ollama import OllamaPlanner


def test_workspace_listing_flow() -> None:
    agent = RalfloopAgent(
        adapter=OpenShellAdapterStub(),
        planner=OllamaPlanner(model="qwen2.5:7b"),
        logger=AuditLogger(),
    )

    state = agent.run("Mostrami i file nella workspace")

    assert state.status == "completed"
    assert state.stop_reason == "goal_completed"
    assert state.last_result is not None
    assert state.last_result.tool_name == "sandbox_list_dir"
    assert state.final_answer is not None
    assert "Contenuto della workspace:" in state.final_answer or "La workspace è vuota." in state.final_answer
