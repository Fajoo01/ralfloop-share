from ralfloop_agent.integration.capability_adapter import route_to_legacy_dict
from src.router import route_task


def test_read_only_route_keeps_execution_contract_without_approval() -> None:
    payload = route_to_legacy_dict(route_task("Controlla cosa occupa spazio e non cancellare nulla"))

    assert payload["task_mode"] == "read_only_system_inspection"
    assert payload["needs_human_confirmation"] is False
    assert payload["needs_jury"] is False
    assert "df" in payload["shell_evidence_tools"]
    assert payload["required_output_fields"] == ["command", "path", "exit_code", "stdout", "stderr"]
    assert payload["workflow"] == ["validate_read_only_plan", "collect_real_evidence", "summarize_findings"]


def test_bandi_jury_wraps_workflow_without_losing_ralf_fields() -> None:
    payload = route_to_legacy_dict(route_task("analizza questo bando con giuria multiagent"))

    assert "bandi" in payload["domain_skills"]
    assert payload["needs_jury"] is True
    assert payload["jury_mode"] == "required"
    assert payload["collaboration_backend"]["backend_name"] == "text_mas_proxy"
    assert payload["workflow"][0] == "text_mas_deliberation"
    assert payload["workflow"][-1] == "text_mas_final_review"
    assert payload["workflow"].count("text_mas_deliberation") == 1
    assert payload["required_output_fields"][-2:] == ["stdout", "stderr"]
    assert "final_answer_without_jury_review" in payload["blocked_actions"]


def test_external_action_keeps_approval_and_jury_gates() -> None:
    payload = route_to_legacy_dict(route_task("invia email finale"))

    assert payload["task_mode"] == "external_action"
    assert payload["needs_human_confirmation"] is True
    assert payload["needs_jury"] is True
    assert "send_without_human_confirmation" in payload["blocked_actions"]
    assert "final_answer_without_jury_review" in payload["blocked_actions"]
