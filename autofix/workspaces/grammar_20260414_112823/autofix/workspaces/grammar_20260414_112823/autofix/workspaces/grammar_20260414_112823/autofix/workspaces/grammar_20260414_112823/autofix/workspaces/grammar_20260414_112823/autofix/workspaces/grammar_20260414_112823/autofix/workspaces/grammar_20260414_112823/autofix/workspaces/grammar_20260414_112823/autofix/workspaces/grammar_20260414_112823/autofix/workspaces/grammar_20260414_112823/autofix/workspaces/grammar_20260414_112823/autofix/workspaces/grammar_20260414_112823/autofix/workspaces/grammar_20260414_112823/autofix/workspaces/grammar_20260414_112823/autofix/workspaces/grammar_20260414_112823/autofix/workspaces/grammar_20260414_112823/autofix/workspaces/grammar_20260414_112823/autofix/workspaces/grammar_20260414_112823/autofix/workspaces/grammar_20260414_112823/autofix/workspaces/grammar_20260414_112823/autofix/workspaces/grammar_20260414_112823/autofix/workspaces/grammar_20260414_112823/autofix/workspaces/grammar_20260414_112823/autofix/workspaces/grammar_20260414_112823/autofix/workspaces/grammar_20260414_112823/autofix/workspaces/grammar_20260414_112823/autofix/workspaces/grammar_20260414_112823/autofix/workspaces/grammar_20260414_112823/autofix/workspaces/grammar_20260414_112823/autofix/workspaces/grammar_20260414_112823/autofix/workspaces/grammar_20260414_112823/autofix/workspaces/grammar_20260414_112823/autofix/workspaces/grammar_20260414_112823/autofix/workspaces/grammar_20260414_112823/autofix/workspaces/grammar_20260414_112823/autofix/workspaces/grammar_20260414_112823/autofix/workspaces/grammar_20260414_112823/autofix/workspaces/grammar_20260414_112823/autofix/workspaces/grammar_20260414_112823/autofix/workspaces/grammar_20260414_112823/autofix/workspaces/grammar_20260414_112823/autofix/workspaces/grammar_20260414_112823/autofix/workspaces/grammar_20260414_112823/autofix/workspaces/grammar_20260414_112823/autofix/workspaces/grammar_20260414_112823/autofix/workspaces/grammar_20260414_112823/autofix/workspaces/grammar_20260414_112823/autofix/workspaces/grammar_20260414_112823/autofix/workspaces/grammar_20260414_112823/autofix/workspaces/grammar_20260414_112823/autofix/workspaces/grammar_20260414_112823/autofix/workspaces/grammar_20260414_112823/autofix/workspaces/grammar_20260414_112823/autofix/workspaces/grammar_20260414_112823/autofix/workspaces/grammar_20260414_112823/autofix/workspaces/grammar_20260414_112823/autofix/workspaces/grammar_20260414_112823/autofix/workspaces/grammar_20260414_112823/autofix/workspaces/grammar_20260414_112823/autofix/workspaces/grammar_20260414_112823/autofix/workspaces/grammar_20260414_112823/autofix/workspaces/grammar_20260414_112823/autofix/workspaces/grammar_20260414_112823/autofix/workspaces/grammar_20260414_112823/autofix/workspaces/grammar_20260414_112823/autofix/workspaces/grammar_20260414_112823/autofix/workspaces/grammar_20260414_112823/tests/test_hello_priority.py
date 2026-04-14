from ralfloop_agent.providers.ollama import DeterministicPlanner, OllamaPlanner


def test_hello_goal_has_priority_over_generic_file_listing() -> None:
    goal = "Scrivi hello in un file e verifica il contenuto"

    for planner in (DeterministicPlanner(), OllamaPlanner(model="qwen2.5:7b")):
        d0 = planner.choose_next_action(goal, 0)
        assert d0.tool_name == "sandbox_exec"

        d1 = planner.choose_next_action(goal, 1)
        assert d1.tool_name == "sandbox_read_file"
        assert d1.tool_input["path"] == "out/hello_exec.txt"
