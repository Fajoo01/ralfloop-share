from ralfloop_agent.adapters.openshell_adapter import OpenShellAdapterStub
from ralfloop_agent.core.loop import RalfloopAgent
from ralfloop_agent.logging.audit import AuditLogger
from ralfloop_agent.providers.ollama import OllamaPlanner


def test_final_answer_summarizes_ollama_models() -> None:
    agent = RalfloopAgent(
        adapter=OpenShellAdapterStub(),
        planner=OllamaPlanner(model="qwen2.5:7b"),
        logger=AuditLogger(),
    )

    state = agent.run("Mostrami i modelli Ollama disponibili")

    assert state.status == "completed"
    assert state.stop_reason == "goal_completed"
    assert state.last_result is not None
    assert state.last_result.tool_name == "sandbox_http_fetch"
    assert state.final_answer is not None
    assert "Modelli Ollama disponibili:" in state.final_answer
    assert "qwen3.5:9b" in state.final_answer
