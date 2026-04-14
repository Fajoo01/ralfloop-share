from ralfloop_agent.providers.ollama import OllamaPlanner


def test_planner_routes_three_step_goal() -> None:
    planner = OllamaPlanner(model="qwen2.5:7b")
    goal = 'Scrivi "ciao mondo" in out/messaggio.txt, mostrami i file nella cartella out e poi leggilo'

    d0 = planner.choose_next_action(goal, 0)
    assert d0.tool_name == "sandbox_exec"

    d1 = planner.choose_next_action(goal, 1)
    assert d1.tool_name == "sandbox_list_dir"
    assert d1.tool_input["path"] == "out"

    d2 = planner.choose_next_action(goal, 2)
    assert d2.tool_name == "sandbox_read_file"
    assert d2.tool_input["path"] == "out/messaggio.txt"
