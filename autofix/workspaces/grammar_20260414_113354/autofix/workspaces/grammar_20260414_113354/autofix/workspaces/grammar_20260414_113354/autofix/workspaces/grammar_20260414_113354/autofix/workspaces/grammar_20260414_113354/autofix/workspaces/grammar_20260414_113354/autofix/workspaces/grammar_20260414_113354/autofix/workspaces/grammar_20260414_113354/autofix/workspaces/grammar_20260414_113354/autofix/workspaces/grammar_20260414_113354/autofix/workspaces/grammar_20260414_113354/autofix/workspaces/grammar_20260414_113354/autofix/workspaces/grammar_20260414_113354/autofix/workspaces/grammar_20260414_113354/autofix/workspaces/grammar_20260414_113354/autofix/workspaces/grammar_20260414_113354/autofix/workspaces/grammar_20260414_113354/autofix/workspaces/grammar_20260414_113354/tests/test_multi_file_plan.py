from ralfloop_agent.providers.ollama import OllamaPlanner


def test_planner_routes_two_file_write_then_read_goal() -> None:
    planner = OllamaPlanner(model="qwen2.5:7b")
    goal = 'Scrivi "uno" in out/a.txt, scrivi "due" in out/b.txt e poi leggili'

    d0 = planner.choose_next_action(goal, 0)
    assert d0.tool_name == "sandbox_exec"
    assert "out/a.txt" in d0.tool_input["command"]
    assert "uno" in d0.tool_input["command"]

    d1 = planner.choose_next_action(goal, 1)
    assert d1.tool_name == "sandbox_exec"
    assert "out/b.txt" in d1.tool_input["command"]
    assert "due" in d1.tool_input["command"]

    d2 = planner.choose_next_action(goal, 2)
    assert d2.tool_name == "sandbox_read_file"
    assert d2.tool_input["path"] == "out/a.txt"

    d3 = planner.choose_next_action(goal, 3)
    assert d3.tool_name == "sandbox_read_file"
    assert d3.tool_input["path"] == "out/b.txt"
