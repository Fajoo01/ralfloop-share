from ralfloop_agent.adapters.openshell_adapter import OpenShellAdapterStub
from ralfloop_agent.core.loop import RalfloopAgent
from ralfloop_agent.logging.audit import AuditLogger
from ralfloop_agent.providers.ollama import DeterministicPlanner


def main() -> None:
    agent = RalfloopAgent(
        adapter=OpenShellAdapterStub(base_dir=".sandbox"),
        planner=DeterministicPlanner(),
        logger=AuditLogger(store_path="./logs"),
    )
    state = agent.run("Scrivi hello in un file e verifica il contenuto")
    print(state.final_answer)


if __name__ == "__main__":
    main()
