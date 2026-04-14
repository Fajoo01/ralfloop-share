from ralfloop_agent.providers.ollama import OllamaPlanner


def test_planner_routes_subdir_listing_goal() -> None:
    planner = OllamaPlanner(model="qwen2.5:7b")

    d = planner.choose_next_action("Mostrami i file nella cartella out", 0)
    assert d.tool_name == "sandbox_list_dir"
    assert d.tool_input["path"] == "out"
