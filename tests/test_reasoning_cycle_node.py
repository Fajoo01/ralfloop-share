import json

from ralfloop_agent.experimental.reasoning_cycle_node import main, run_reasoning_cycle


def test_reasoning_cycle_output_is_json_serializable():
    packet = run_reasoning_cycle(
        user_goal="Implementa nodo sperimentale separato",
        observations=["Baseline shell/math funziona."],
        constraints=["NON toccare runtime attivo", "Nessuna rete"],
        memory={"policy": "default-deny"},
        last_result={"ok": False, "stderr": "timeout in previous probe"},
    )

    encoded = json.dumps(packet.to_dict(), ensure_ascii=False)

    assert "internal_state_packet" in encoded
    assert packet.node == "reasoning_cycle_node"
    assert packet.decision.status == "continue"
    assert packet.selected_next_action["action_type"] == "write_experimental_module"


def test_no_write_constraint_selects_evidence_before_mutation():
    packet = run_reasoning_cycle(
        user_goal="fix il loop e patcha il runtime",
        observations=["Failure reproduced locally."],
        constraints=["read-only: non modificare file"],
    )

    objections = [item.objection for item in packet.objections]

    assert packet.selected_next_action["action_type"] == "collect_evidence"
    assert packet.selected_next_action["writes_allowed"] is False
    assert any("No-write" in item for item in objections)


def test_runtime_boundary_keeps_node_experimental():
    packet = run_reasoning_cycle(
        user_goal="Implement reasoning cycle node",
        observations=["Existing baseline remains active."],
        constraints=["NON toccare runtime attivo Cheshire Cat"],
    )

    assert packet.selected_next_action["action_type"] == "write_experimental_module"
    assert packet.selected_next_action["writes_allowed"] is True
    assert any(item.hypothesis_id == "h_runtime_boundary" for item in packet.hypotheses)


def test_empty_goal_stops_parseably():
    packet = run_reasoning_cycle(user_goal="")

    assert packet.decision.status == "stop"
    assert packet.stop_reason == "empty_user_goal"
    assert packet.selected_next_action["commands"] == []


def test_external_action_requires_confirmation():
    packet = run_reasoning_cycle(
        user_goal="manda email alla Regione",
        observations=["Draft exists."],
        constraints=["default-deny"],
    )

    assert packet.decision.status == "blocked"
    assert packet.stop_reason == "human_confirmation_required"
    assert packet.selected_next_action["requires_human_confirmation"] is True


def test_cli_prints_parseable_json(capsys):
    assert main([
        "--goal",
        "verifica bug locale",
        "--observation",
        "pytest target failed",
        "--constraint",
        "Nessuna rete",
    ]) == 0

    payload = json.loads(capsys.readouterr().out)

    assert payload["node"] == "reasoning_cycle_node"
    assert payload["decision"]["status"] == "continue"
    assert payload["selected_next_action"]["action_type"] == "run_targeted_verification"
