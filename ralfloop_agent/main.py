import os
import sys

from ralfloop_agent.adapters.openshell_adapter import OpenShellAdapterStub
from ralfloop_agent.adapters.openshell_real_adapter import OpenShellAdapterReal
from ralfloop_agent.core.loop import RalfloopAgent
from ralfloop_agent.logging.audit import AuditLogger
from ralfloop_agent.providers.ollama import OllamaPlanner


def build_adapter():
    mode = os.getenv("RALFLOOP_ADAPTER", "local").strip().lower()

    if mode == "openshell":
        local_stub = OpenShellAdapterStub()
        return OpenShellAdapterReal(local_fallback=local_stub)

    return OpenShellAdapterStub()


def main() -> None:
    goal = " ".join(sys.argv[1:]).strip() or "Scrivi hello in un file e verifica il contenuto"

    adapter = build_adapter()
    logger = AuditLogger()
    planner = OllamaPlanner(model="qwen2.5:7b")
    agent = RalfloopAgent(adapter=adapter, planner=planner, logger=logger)

    state = agent.run(goal)
    print(state.final_answer or "Nessuna risposta finale.")
    print()
    print("stop_reason:", state.stop_reason)
    print("last_tool:", state.last_result.tool_name if state.last_result else None)


if __name__ == "__main__":
    main()
