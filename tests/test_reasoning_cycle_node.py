import json

from ralfloop_agent.experimental import reasoning_cycle_node as node
from ralfloop_agent.experimental.reasoning_cycle_node import main, run_reasoning_cycle
from ralfloop_agent.experimental.reasoning_cycle_node import observation_from_tool_result


def test_observation_from_tool_result_empty_payloads():
    assert observation_from_tool_result(None) is None
    assert observation_from_tool_result({}) is None


def test_observation_from_tool_result_ok_false():
    observation = observation_from_tool_result({"ok": False})

    assert observation == "ok=false"


def test_observation_from_tool_result_exit_code_non_zero():
    observation = observation_from_tool_result({"ok": True, "exit_code": 2})

    assert "exit_code=2" in observation


def test_observation_from_tool_result_stderr_traceback():
    observation = observation_from_tool_result({"stderr": "Traceback\nRuntimeError: boom"})

    assert "traceback" in observation
    assert "runtimeerror" in observation
    assert "stderr: Traceback RuntimeError: boom" in observation


def test_observation_from_tool_result_stdout_useful():
    observation = observation_from_tool_result({"stdout": "6 passed in 0.02s"})

    assert observation == "stdout: 6 passed in 0.02s"


def test_observation_from_tool_result_timeout():
    observation = observation_from_tool_result({"ok": False, "stderr": "command timeout after 8s"})

    assert "ok=false" in observation
    assert "timeout" in observation


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
    assert packet.selected_next_action["action_type"] == "inspect_failure"
    assert packet.selected_next_action["writes_allowed"] is False


def test_output_json_dumps_compatible_with_tool_observation():
    packet = run_reasoning_cycle(
        user_goal="verifica errore locale",
        last_result={"ok": False, "exit_code": 1, "stderr": "failed"},
    )

    payload = json.dumps(packet.to_dict(), ensure_ascii=False)

    assert "ok=false" in payload
    assert "internal_state_packet" in payload


def test_no_write_constraint_selects_evidence_before_mutation():
    packet = run_reasoning_cycle(
        user_goal="fix il loop e patcha il runtime",
        observations=["Failure reproduced locally."],
        constraints=["read-only: non modificare file"],
    )

    objections = [item.objection for item in packet.objections]

    assert packet.decision.status == "blocked"
    assert packet.stop_reason == "hard_policy_violation"
    assert packet.selected_next_action["action_type"] == "none"
    assert packet.selected_next_action["writes_allowed"] is False
    assert any("No-write" in item for item in objections)


def test_runtime_boundary_keeps_node_experimental():
    packet = run_reasoning_cycle(
        user_goal="Implement reasoning cycle node",
        observations=["Existing baseline remains active."],
        constraints=["NON toccare runtime attivo di produzione"],
    )

    assert packet.selected_next_action["action_type"] == "write_experimental_module"
    assert packet.selected_next_action["writes_allowed"] is True
    assert any(item.hypothesis_id == "h_runtime_boundary" for item in packet.hypotheses)


def test_empty_goal_stops_parseably():
    packet = run_reasoning_cycle(user_goal="")

    assert packet.decision.status == "stop"
    assert packet.stop_reason == "empty_user_goal"
    assert packet.selected_next_action["action_type"] == "none"
    assert packet.selected_next_action["commands"] == []
    assert packet.selected_next_action["writes_allowed"] is False


def test_external_action_requires_confirmation():
    packet = run_reasoning_cycle(
        user_goal="manda email alla Regione",
        observations=["Draft exists."],
        constraints=["default-deny"],
    )

    assert packet.decision.status == "blocked"
    assert packet.stop_reason == "human_confirmation_required"
    assert packet.selected_next_action["requires_human_confirmation"] is True


def test_confidence_is_clamped(monkeypatch):
    def fake_select(*args):
        return (
            "continue",
            {
                "action_type": "orient_readonly",
                "commands": [],
                "writes_allowed": False,
                "requires_human_confirmation": False,
            },
            2.5,
            None,
        )

    monkeypatch.setattr(node, "_select_next_action", fake_select)

    packet = node.run_reasoning_cycle(user_goal="verifica")

    assert packet.confidence == 1.0
    assert packet.decision.confidence == 1.0


def test_contradiction_detection_adds_actionable_objections():
    packet = run_reasoning_cycle(
        user_goal="patch runtime core and fetch http status",
        observations=["success path reported"],
        constraints=["NON toccare runtime attivo", "read-only", "Nessuna rete"],
        last_result={"ok": True, "stderr": "Traceback: failed"},
    )

    objections = [item.objection for item in packet.objections]

    assert "Runtime protected but goal asks to modify it." in objections
    assert "No-write constraint conflicts with requested mutation." in objections
    assert "No-network constraint conflicts with requested network/external action." in objections
    assert "State mixes success and failure signals." in objections


def test_hard_policy_violation_blocks_forbidden_goal():
    packet = run_reasoning_cycle(
        user_goal="Patch the runtime to add logging, then fetch the updated config from http://example.com/config.",
        constraints=["no network", "baseline intoccabile", "read-only"],
    )

    payload = packet.to_dict()

    assert packet.decision.status == "blocked"
    assert packet.stop_reason == "hard_policy_violation"
    assert packet.selected_next_action["action_type"] == "none"
    assert packet.selected_next_action["commands"] == []
    assert packet.selected_next_action["writes_allowed"] is False
    assert packet.selected_next_action["requires_human_confirmation"] is False
    assert packet.confidence <= 0.2
    assert packet.objections
    assert packet.evidence_needed
    assert payload["decision"]["selected_next_action"]["action_type"] == "none"
    assert json.dumps(payload, ensure_ascii=False)


def test_failed_tool_traceback_prioritizes_inspection_before_write():
    packet = run_reasoning_cycle(
        user_goal="Fix the failing parser with the smallest safe change.",
        constraints=["non toccare runtime attivo", "patch minima", "nessuna rete"],
        last_result={
            "ok": False,
            "exit_code": 1,
            "stderr": "Traceback (most recent call last): RuntimeError: parser exploded",
        },
    )

    assert packet.decision.status == "continue"
    assert packet.selected_next_action["action_type"] in {"inspect_failure", "run_targeted_verification"}
    assert packet.selected_next_action["action_type"] != "write_experimental_module"
    assert packet.selected_next_action["writes_allowed"] is False
    assert packet.selected_next_action["commands"]


def test_ambiguous_readonly_goal_can_still_inspect():
    packet = run_reasoning_cycle(
        user_goal="Understand where logging is configured",
        constraints=["read-only", "no network"],
    )

    assert packet.decision.status == "continue"
    assert packet.selected_next_action["action_type"] == "orient_readonly"
    assert packet.selected_next_action["writes_allowed"] is False
    assert packet.selected_next_action["commands"]


def test_bilingual_policy_blocks_scarica_config():
    packet = run_reasoning_cycle(
        user_goal="Aggiorna il runtime e scarica la configurazione da http://example.com/config",
        constraints=["niente rete", "runtime intoccabile", "sola lettura"],
    )

    assert packet.decision.status == "blocked"
    assert packet.stop_reason == "hard_policy_violation"
    assert packet.selected_next_action["action_type"] == "none"
    assert packet.selected_next_action["commands"] == []
    assert packet.selected_next_action["writes_allowed"] is False
    assert packet.confidence <= 0.2


def test_bilingual_policy_blocks_remote_publish_with_no_network():
    packet = run_reasoning_cycle(
        user_goal="Pubblica il risultato finale sul servizio remoto configurato",
        constraints=["default-deny", "niente rete"],
    )

    assert packet.decision.status == "blocked"
    assert packet.selected_next_action["action_type"] == "none"
    assert packet.selected_next_action["commands"] == []
    assert packet.selected_next_action["writes_allowed"] is False


def test_bilingual_policy_requests_confirmation_for_mail():
    packet = run_reasoning_cycle(
        user_goal="Manda la mail finale al Comune adesso",
        constraints=["default-deny", "azioni esterne richiedono conferma"],
    )

    assert packet.decision.status == "blocked"
    assert packet.stop_reason == "human_confirmation_required"
    assert packet.selected_next_action["action_type"] == "request_confirmation"
    assert packet.selected_next_action["requires_human_confirmation"] is True
    assert packet.selected_next_action["writes_allowed"] is False
    assert packet.selected_next_action["commands"] == []


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
