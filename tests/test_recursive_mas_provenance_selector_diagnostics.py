from pathlib import Path

import pytest

from ralfloop_agent.domains.recursive_mas_provenance_selector_diagnostics import (
    EvidenceSelection, aggregate_selection, combine_selections, deterministic_candidates, development_gate,
    order_sensitivity, preliminary_gate, reserve_seal_audit, selection_from_payload, selection_row,
)


def case():
    return {
        "id": "C1", "domain_id": "D1", "category": "incomplete_rules",
        "question": "Se la verifica manca, quale scelta è consentita?",
        "facts": [{"statement": "La verifica richiesta non è disponibile."}], "constraints": ["Non decidere senza verifica."],
        "rules": [{"rule_id": "R1", "statement": "Se la verifica manca, la scelta resta condizionata."}, {"rule_id": "R2", "statement": "Il colore non modifica la decisione."}],
        "sources": [{"source_id": "S1", "statement": "Il registro conferma che la verifica manca."}, {"source_id": "S2", "statement": "Il catalogo descrive il colore."}],
        "gold": {"required_rules": ["R1"], "required_sources": ["S1"]},
    }


def test_allowed_selected_and_allowlist():
    selected = selection_from_payload(case(), {"selected_rule_ids": ["R1"], "selected_source_ids": ["S1"]})
    assert selected.allowed_rule_ids == ("R1", "R2") and selected.selected_rule_ids == ("R1",)
    with pytest.raises(ValueError, match="not_allowed"):
        selection_from_payload(case(), {"selected_rule_ids": ["RX"], "selected_source_ids": []})


def test_union_intersection_and_rescue():
    left = EvidenceSelection(("R1", "R2"), ("S1", "S2"), ("R1",), ("S1",))
    right = EvidenceSelection(("R1", "R2"), ("S1", "S2"), ("R2",), ("S1", "S2"))
    assert combine_selections(left, right, "union").selected_rule_ids == ("R1", "R2")
    assert combine_selections(left, right, "intersection").selected_source_ids == ("S1",)


def test_critic_only_qwen3_schema_and_critic_rescue():
    critic_only = selection_from_payload(case(), {"selected_rule_ids": ["R2"], "selected_source_ids": ["S2"]})
    planner = selection_from_payload(case(), {"selected_rule_ids": ["R1"], "selected_source_ids": []})
    rescue = combine_selections(planner, critic_only, "union")
    assert rescue.selected_rule_ids == ("R1", "R2") and rescue.selected_source_ids == ("S2",)


def test_candidate_is_generic_bounded_deterministic():
    first = deterministic_candidates(case())
    assert first == deterministic_candidates(case()) and "R1" in first.selected_rule_ids
    assert set(first.selected_rule_ids) <= set(first.allowed_rule_ids)


def test_metrics_and_gates():
    good = selection_row(case(), selection_from_payload(case(), {"selected_rule_ids": ["R1"], "selected_source_ids": ["S1"]}))
    bad = selection_row(case(), selection_from_payload(case(), {"selected_rule_ids": ["R2"], "selected_source_ids": ["S2"]}))
    assert aggregate_selection([good])["missing_evidence_rate"] == 0
    assert aggregate_selection([bad])["overselection_rate"] == 1
    metrics = aggregate_selection([good])
    assert preliminary_gate(metrics, metrics)["passed"]


def test_development_end_to_end_gate():
    value = {
        "semantic_complete_count": 32, "schema_valid_count": 34, "rule_recall": .9, "source_recall": .9,
        "rule_precision": .8, "source_precision": .8, "contradiction_recall": .9, "counterargument_coverage": .9,
        "recommendation_presence": 1.0, "invented_rule_ids": 0, "invented_source_ids": 0,
        "safety_violations": 0, "approval_violations": 0,
    }
    original = {**value, "semantic_complete_count": 31}
    assert development_gate(value, value, original)["passed"]


def test_order_sensitivity():
    one = selection_from_payload(case(), {"selected_rule_ids": ["R1"], "selected_source_ids": ["S1"]})
    two = selection_from_payload(case(), {"selected_rule_ids": ["R2"], "selected_source_ids": ["S1"]})
    assert order_sensitivity(one, [two])["changed"]


def test_oracle_packet_runner_uses_gold_ids_without_gold_text():
    from ralfloop_agent.domains.recursive_mas_domain_provenance import EvidencePacket, PROTOCOL_VERSION
    selected = selection_from_payload(case(), {"selected_rule_ids": ["R1"], "selected_source_ids": ["S1"]})
    packet = EvidencePacket(PROTOCOL_VERSION, selected.allowed_rule_ids, selected.allowed_source_ids, selected.selected_rule_ids, selected.selected_source_ids, ("R1",), (), ("S1",), (), ())
    assert [slot.evidence_id for slot in packet.slots()] == ["R1", "S1"]
    assert "gold_structured_output" not in packet.to_dict()


def test_reserve_not_semantically_read_or_executed(tmp_path: Path):
    path = tmp_path / "reserve_b.jsonl"; path.write_bytes(b"opaque\n" * 24); path.chmod(0o600)
    import hashlib
    result = reserve_seal_audit(path, hashlib.sha256(path.read_bytes()).hexdigest())
    assert result["sealed"] and not result["semantic_read"] and not result["executed"]


def test_reserve_checker_never_uses_text_or_json(monkeypatch, tmp_path: Path):
    path = tmp_path / "reserve_b.jsonl"; path.write_bytes(b"sealed\n" * 24); path.chmod(0o600)
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("semantic_read")))
    import hashlib
    assert reserve_seal_audit(path, hashlib.sha256(path.read_bytes()).hexdigest())["sealed"]


def test_feature_flag_defaults_disabled():
    import inspect
    from ralfloop_agent.domains.recursive_mas_domain_training import _require_safe_runtime
    assert 'os.getenv("RALF_RECURSIVE_DOMAIN_REASONING", "0") != "0"' in inspect.getsource(_require_safe_runtime)


def test_math_profile_and_telegram_gate_unchanged():
    import hashlib
    repo = Path(__file__).resolve().parents[1]
    assert hashlib.sha256((repo / "ralfloop_agent/domains/recursive_mas_profiles.py").read_bytes()).hexdigest() == "4c89714dfabe1603c0f73d48c452d761e9d460f533c6e23f658dfdcbe72921cf"
    assert hashlib.sha256((repo / "ralfloop_agent/domains/domain_approval_executor.py").read_bytes()).hexdigest() == "90031d96888449ea30baa45016f0980cf9e334f701ec037e00a46dff559db416"
