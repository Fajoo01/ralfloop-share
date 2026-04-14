from ralfloop_agent.providers.ollama import OllamaPlanner


def test_planner_routes_compound_write_then_read_goal() -> None:
    planner = OllamaPlanner(model="qwen2.5:7b")

    d0 = planner.choose_next_action("Scrivi hello e poi leggi out/hello_exec.txt", 0)
    assert d0.tool_name == "sandbox_exec"
    assert "out/hello_exec.txt" in d0.tool_input["command"]

    d1 = planner.choose_next_action("Scrivi hello e poi leggi out/hello_exec.txt", 1)
    assert d1.tool_name == "sandbox_read_file"
    assert d1.tool_input["path"] == "out/hello_exec.txt"
