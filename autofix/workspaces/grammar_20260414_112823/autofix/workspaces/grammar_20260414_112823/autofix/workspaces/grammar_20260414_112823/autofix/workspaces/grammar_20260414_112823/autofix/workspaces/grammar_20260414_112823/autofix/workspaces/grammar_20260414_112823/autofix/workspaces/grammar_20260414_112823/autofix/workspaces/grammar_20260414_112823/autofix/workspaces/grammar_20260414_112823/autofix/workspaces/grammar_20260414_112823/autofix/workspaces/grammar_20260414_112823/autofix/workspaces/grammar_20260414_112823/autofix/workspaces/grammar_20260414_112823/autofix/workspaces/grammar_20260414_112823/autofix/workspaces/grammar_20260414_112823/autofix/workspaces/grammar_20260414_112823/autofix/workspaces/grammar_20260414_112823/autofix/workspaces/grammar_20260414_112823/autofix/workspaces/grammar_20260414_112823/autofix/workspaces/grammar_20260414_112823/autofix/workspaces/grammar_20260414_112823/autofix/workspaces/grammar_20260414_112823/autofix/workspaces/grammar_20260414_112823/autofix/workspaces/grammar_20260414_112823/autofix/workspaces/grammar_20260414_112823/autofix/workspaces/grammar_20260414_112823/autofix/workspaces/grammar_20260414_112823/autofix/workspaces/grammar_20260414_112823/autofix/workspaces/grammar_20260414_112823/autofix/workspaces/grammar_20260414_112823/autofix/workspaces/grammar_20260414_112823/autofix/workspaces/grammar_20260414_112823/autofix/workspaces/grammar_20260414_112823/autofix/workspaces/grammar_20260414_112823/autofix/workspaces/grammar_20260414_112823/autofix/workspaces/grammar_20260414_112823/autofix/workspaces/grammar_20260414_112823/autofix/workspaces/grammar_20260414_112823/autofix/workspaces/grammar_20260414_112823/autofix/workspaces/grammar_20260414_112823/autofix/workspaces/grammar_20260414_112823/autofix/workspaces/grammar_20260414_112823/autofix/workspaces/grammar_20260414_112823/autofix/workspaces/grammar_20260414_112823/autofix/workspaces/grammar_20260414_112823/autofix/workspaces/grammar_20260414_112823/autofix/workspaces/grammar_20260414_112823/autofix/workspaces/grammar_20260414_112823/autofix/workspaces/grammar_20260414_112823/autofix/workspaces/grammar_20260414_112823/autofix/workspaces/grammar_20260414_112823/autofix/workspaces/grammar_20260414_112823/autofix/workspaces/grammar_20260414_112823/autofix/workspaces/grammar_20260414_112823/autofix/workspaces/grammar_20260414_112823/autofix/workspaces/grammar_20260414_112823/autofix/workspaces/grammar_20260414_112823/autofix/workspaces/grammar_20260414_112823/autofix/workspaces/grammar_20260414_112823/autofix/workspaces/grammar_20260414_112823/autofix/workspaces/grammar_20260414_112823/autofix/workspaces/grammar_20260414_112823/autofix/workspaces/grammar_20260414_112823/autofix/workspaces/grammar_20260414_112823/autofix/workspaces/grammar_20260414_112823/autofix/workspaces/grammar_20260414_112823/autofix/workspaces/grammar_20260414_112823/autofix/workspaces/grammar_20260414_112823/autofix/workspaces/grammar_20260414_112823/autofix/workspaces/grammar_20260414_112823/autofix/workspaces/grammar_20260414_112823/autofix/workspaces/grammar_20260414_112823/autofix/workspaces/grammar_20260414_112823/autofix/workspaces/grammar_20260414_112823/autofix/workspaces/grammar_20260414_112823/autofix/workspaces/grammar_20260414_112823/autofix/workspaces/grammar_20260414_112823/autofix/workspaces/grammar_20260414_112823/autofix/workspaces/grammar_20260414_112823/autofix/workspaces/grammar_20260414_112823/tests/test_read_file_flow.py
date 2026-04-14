from ralfloop_agent.providers.ollama import OllamaPlanner


def test_planner_routes_explicit_file_read() -> None:
    planner = OllamaPlanner(model="qwen2.5:7b")
    decision = planner.choose_next_action("Leggi il file out/hello_exec.txt", 0)

    assert decision.tool_name == "sandbox_read_file"
    assert decision.tool_input["path"] == "out/hello_exec.txt"
