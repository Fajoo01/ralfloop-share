import json
from io import StringIO

from ralfloop_agent.cli import terminal_chat
from ralfloop_agent.domains.reasoning_cli import explain_routing, infer_reason_codes


def test_reason_code_inference():
    assert "conflicting_sources" in infer_reason_codes("Valuta fonti in contraddizione")
    assert infer_reason_codes("Esprimi un parere") == ["qualitative_judgment"]


def test_explain_routing_has_no_chain_of_thought():
    result = explain_routing("incident_triage", "Quale strategia prudente?")
    assert set(("domain", "reason_codes", "selected_backend", "why_recursive", "why_not_recursive")) <= set(result)
    assert "chain_of_thought" not in result


def test_missing_domain_requires_creation():
    result = explain_routing("synthetic_missing", "Valuta strategia")
    assert result["selected_backend"] == "domain_creation_required"


def test_terminal_domain_reason_command(monkeypatch):
    import ralfloop_agent.domains.reasoning_cli as reasoning_cli

    monkeypatch.setattr(reasoning_cli, "reason_domain", lambda *args, **kwargs: {"status": "domain_reasoning_required", "selected_backend": "single_qwen_7b_with_domain"})
    args = terminal_chat.build_parser().parse_args(["domain", "reason", "incident_triage", "Valuta", "strategia"])
    out = StringIO()
    assert terminal_chat.run_domain_reason(args, out=out) == 0
    assert json.loads(out.getvalue())["selected_backend"] == "single_qwen_7b_with_domain"


def test_terminal_recursive_commands(monkeypatch):
    import ralfloop_agent.domains.reasoning_cli as reasoning_cli

    monkeypatch.setattr(reasoning_cli, "explain_routing", lambda *args, **kwargs: {"domain": "incident_triage", "selected_backend": "single_qwen_7b_with_domain"})
    args = terminal_chat.build_parser().parse_args(["recursive", "explain-routing", "incident_triage", "Valuta"])
    out = StringIO()
    assert terminal_chat.run_recursive(args, out=out) == 0
    assert json.loads(out.getvalue())["domain"] == "incident_triage"
