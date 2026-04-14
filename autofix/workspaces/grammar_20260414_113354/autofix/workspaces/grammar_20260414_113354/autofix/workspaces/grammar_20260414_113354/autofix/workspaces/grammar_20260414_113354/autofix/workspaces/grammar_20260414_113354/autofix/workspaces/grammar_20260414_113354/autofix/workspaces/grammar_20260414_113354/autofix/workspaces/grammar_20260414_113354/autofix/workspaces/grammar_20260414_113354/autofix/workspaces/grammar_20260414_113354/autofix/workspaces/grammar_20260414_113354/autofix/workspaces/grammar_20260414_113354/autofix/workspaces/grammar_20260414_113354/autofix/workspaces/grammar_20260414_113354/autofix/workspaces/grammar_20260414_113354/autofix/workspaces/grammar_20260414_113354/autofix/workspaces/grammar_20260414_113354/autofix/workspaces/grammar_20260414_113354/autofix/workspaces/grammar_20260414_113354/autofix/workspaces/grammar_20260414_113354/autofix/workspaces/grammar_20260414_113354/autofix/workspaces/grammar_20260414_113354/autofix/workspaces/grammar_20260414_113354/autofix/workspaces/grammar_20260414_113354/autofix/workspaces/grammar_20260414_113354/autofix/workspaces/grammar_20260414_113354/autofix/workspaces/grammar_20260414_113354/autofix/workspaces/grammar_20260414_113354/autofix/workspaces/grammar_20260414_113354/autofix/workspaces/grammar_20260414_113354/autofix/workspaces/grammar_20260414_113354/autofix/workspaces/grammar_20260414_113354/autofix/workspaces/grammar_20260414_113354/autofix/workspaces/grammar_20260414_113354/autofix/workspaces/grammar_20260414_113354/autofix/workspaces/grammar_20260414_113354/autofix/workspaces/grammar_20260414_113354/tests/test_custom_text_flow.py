from ralfloop_agent.providers.ollama import OllamaPlanner


def test_planner_routes_custom_text_write_then_read() -> None:
    planner = OllamaPlanner(model="qwen2.5:7b")

    d0 = planner.choose_next_action('Scrivi "ciao mondo" e poi leggi out/messaggio.txt', 0)
    assert d0.tool_name == "sandbox_exec"
    assert "out/messaggio.txt" in d0.tool_input["command"]
    assert "ciao mondo" in d0.tool_input["command"]

    d1 = planner.choose_next_action('Scrivi "ciao mondo" e poi leggi out/messaggio.txt', 1)
    assert d1.tool_name == "sandbox_read_file"
    assert d1.tool_input["path"] == "out/messaggio.txt"
