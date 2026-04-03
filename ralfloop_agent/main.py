from ralfloop_agent.adapters.openshell_adapter import OpenShellAdapterStub
from ralfloop_agent.core.loop import RalfloopAgent
from ralfloop_agent.logging.audit import AuditLogger
from ralfloop_agent.providers.ollama import OllamaPlanner


def main() -> None:
    adapter = OpenShellAdapterStub()
    logger = AuditLogger()
    planner = OllamaPlanner(model="qwen2.5:7b")
    agent = RalfloopAgent(adapter=adapter, planner=planner, logger=logger)

    state = agent.run("Scrivi hello in un file e verifica il contenuto")
    print(state.final_answer or "Nessuna risposta finale.")
    print()
    print("stop_reason:", state.stop_reason)
    print("last_tool:", state.last_result.tool_name if state.last_result else None)


if __name__ == "__main__":
    main()
