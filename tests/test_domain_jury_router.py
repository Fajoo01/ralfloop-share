from ralfloop_agent.domains.jury_router import DomainJuryRouter


def test_conflict_sources_activate_jury():
    out = DomainJuryRouter().should_use_jury(domain_resolution={"status": "resolved"}, classification="mixed", deterministic_result={"complete": False, "matched_rules": ["r"], "conflicts": [{"blocking": False}], "unresolved_questions": []})
    assert out["use_jury"] is True
    assert "conflicting_sources" in out["reason_codes"]


def test_qualitative_request_activates_jury():
    out = DomainJuryRouter().should_use_jury(domain_resolution={"status": "resolved"}, classification="non_deterministic", deterministic_result={"complete": False, "matched_rules": [], "unresolved_questions": ["recommendation_required"]})
    assert out["use_jury"] is True
    assert "qualitative_judgment" in out["reason_codes"]


def test_deterministic_complete_disables_jury():
    out = DomainJuryRouter().should_use_jury(domain_resolution={"status": "resolved"}, classification="deterministic", deterministic_result={"complete": True})
    assert out["use_jury"] is False
    assert out["reason_codes"] == ["deterministic_complete"]


def test_human_confirmation_pending_disables_jury():
    out = DomainJuryRouter().should_use_jury(domain_resolution={"status": "resolved"}, classification="external_action", human_confirmation_pending=True)
    assert out["use_jury"] is False
    assert out["reason_codes"] == ["human_confirmation_pending"]
