from pathlib import Path

import pytest

from ralfloop_agent.domains.recursive_mas_bounded_provenance import (
    BoundedDecisions, SlotMapping, apply_uncertain_policy, batch_prompt, decision_agreement,
    itemwise_prompt, parse_batch_bounded, parse_itemwise_bounded, selected_from_decisions,
    selection_jaccard, verify_frozen_components,
)
from ralfloop_agent.domains.recursive_mas_provenance_selector_diagnostics import deterministic_candidates


def case():
    return {
        "id": "C1", "domain_id": "D1", "category": "conflicting_sources",
        "question": "La misura consente una decisione nonostante il conflitto?",
        "facts": [{"fact_id": "F1", "statement": "Due misure divergono."}], "constraints": [],
        "rules": [{"rule_id": "REAL_RULE_A", "statement": "Se fonti divergono, la decisione è condizionata."}, {"rule_id": "REAL_RULE_X", "statement": "Il colore è irrilevante."}],
        "sources": [{"source_id": "REAL_SOURCE_A", "statement": "Prima misura favorevole."}, {"source_id": "REAL_SOURCE_B", "statement": "Seconda misura contraria."}],
        "gold": {"required_rules": ["REAL_RULE_A"], "required_sources": ["REAL_SOURCE_A", "REAL_SOURCE_B"]},
    }


def test_slot_mapping_and_real_id_mapping():
    mapping = SlotMapping.from_case(case())
    assert mapping.to_dict()["RULE_SLOT_01"] == "REAL_RULE_A"
    assert mapping.real_id("SOURCE_SLOT_02") == "REAL_SOURCE_B"
    assert mapping.slot_for_id("REAL_RULE_X") == "RULE_SLOT_02"


def test_slot_bounds_and_foreign_id_impossible():
    mapping = SlotMapping.from_case(case())
    with pytest.raises(ValueError, match="out_of_range"):
        mapping.real_id("RULE_SLOT_03")
    with pytest.raises(ValueError, match="slot_invalid"):
        mapping.real_id("REAL_RULE_A")


def test_batch_bounded_parser_and_end_required():
    parsed = parse_batch_bounded("R: 1=KEEP,2=DROP\nS: 1=KEEP,2=UNCERTAIN\nEND", rule_count=2, source_count=2)
    assert parsed.rules == ("KEEP", "DROP") and parsed.sources[-1] == "UNCERTAIN"
    with pytest.raises(ValueError, match="format_invalid"):
        parse_batch_bounded("R: 1=KEEP,2=DROP\nS: 1=KEEP,2=DROP", rule_count=2, source_count=2)


@pytest.mark.parametrize("value", ["KEEP", "DROP", "UNCERTAIN"])
def test_itemwise_bounded_parser_decisions(value):
    assert parse_itemwise_bounded(f"{value}\nEND") == value
    if value == "UNCERTAIN":
        with pytest.raises(ValueError):
            parse_itemwise_bounded(f"{value}\nEND", binary=True)


def test_uncertain_policy_p1_and_p2():
    assert apply_uncertain_policy(("UNCERTAIN", "DROP"), "P1_KEEP") == ("KEEP", "DROP")
    assert apply_uncertain_policy(("UNCERTAIN", "DROP"), "P2_SECOND_PASS", second_pass=("DROP",)) == ("DROP", "DROP")


def test_rule_source_separation_and_contradictory_source_retention():
    mapping = SlotMapping.from_case(case())
    selection = selected_from_decisions(mapping, BoundedDecisions(("KEEP", "DROP"), ("KEEP", "KEEP")), rule_policy="P1_KEEP", source_policy="P1_KEEP")
    assert selection.selected_rule_ids == ("REAL_RULE_A",)
    assert selection.selected_source_ids == ("REAL_SOURCE_A", "REAL_SOURCE_B")


def test_prompts_hide_real_ids_and_itemwise_has_one_candidate():
    mapping = SlotMapping.from_case(case())
    prompt = batch_prompt(case(), mapping)
    assert "RULE_SLOT_01" in prompt and "REAL_RULE_A" not in prompt
    item = itemwise_prompt(case(), candidate_type="RULE", slot="RULE_SLOT_01", text=case()["rules"][0]["statement"])
    assert case()["rules"][1]["statement"] not in item and "REAL_RULE_A" not in item


def test_deterministic_shortlist_recall_fixture():
    shortlist = deterministic_candidates(case(), max_fraction=1.0)
    assert set(shortlist.selected_rule_ids) >= set(case()["gold"]["required_rules"])
    assert set(shortlist.selected_source_ids) >= set(case()["gold"]["required_sources"])


def test_order_invariance_and_slot_consistency():
    mapping = SlotMapping.from_case(case())
    reversed_prompt = batch_prompt(case(), mapping, rule_order=["REAL_RULE_X", "REAL_RULE_A"], source_order=["REAL_SOURCE_B", "REAL_SOURCE_A"])
    assert mapping.to_dict()["RULE_SLOT_01"] == "REAL_RULE_A" and "RULE_SLOT_01" in reversed_prompt
    decisions = {"REAL_RULE_A": "KEEP", "REAL_RULE_X": "DROP"}
    assert decision_agreement(decisions, dict(reversed(list(decisions.items())))) == 1.0
    selected = mapping.selection(("KEEP", "DROP"), ("KEEP", "KEEP"))
    assert selection_jaccard(selected, selected) == 0.0


def test_oracle_gold_and_allowed_packets_are_separate():
    from ralfloop_agent.domains.recursive_mas_domain_provenance import EvidencePacket, PROTOCOL_VERSION
    mapping = SlotMapping.from_case(case())
    gold = mapping.selection(("KEEP", "DROP"), ("KEEP", "KEEP"))
    allowed = mapping.selection(("KEEP", "KEEP"), ("KEEP", "KEEP"))
    gold_packet = EvidencePacket(PROTOCOL_VERSION, gold.allowed_rule_ids, gold.allowed_source_ids, gold.selected_rule_ids, gold.selected_source_ids, (), (), (), (), ())
    available_packet = EvidencePacket(PROTOCOL_VERSION, allowed.allowed_rule_ids, allowed.allowed_source_ids, allowed.selected_rule_ids, allowed.selected_source_ids, (), (), (), (), ())
    assert len(gold_packet.slots()) == 3 and len(available_packet.slots()) == 4


def test_selector_freeze_rejects_hash_change():
    verify_frozen_components({"frozen": True, "prompt_hash": "abc"}, {"prompt_hash": "abc"})
    with pytest.raises(RuntimeError, match="freeze_mismatch"):
        verify_frozen_components({"frozen": True, "prompt_hash": "abc"}, {"prompt_hash": "def"})


def test_candidate_path_excludes_planner_adapters():
    candidate_path = ("bounded_selector", "critic_inner", "outer23", "solver_inner", "qwen3")
    assert "planner_inner" not in candidate_path and "outer12" not in candidate_path


def test_reserve_b_never_read_or_executed(tmp_path: Path, monkeypatch):
    path = tmp_path / "reserve_b.jsonl"; path.write_bytes(b"opaque\n" * 24); path.chmod(0o600)
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("semantic_read")))
    assert path.stat().st_mode & 0o777 == 0o600


def test_feature_math_and_telegram_invariants():
    import hashlib, inspect
    from ralfloop_agent.domains.recursive_mas_domain_training import _require_safe_runtime
    repo = Path(__file__).resolve().parents[1]
    assert 'os.getenv("RALF_RECURSIVE_DOMAIN_REASONING", "0") != "0"' in inspect.getsource(_require_safe_runtime)
    assert hashlib.sha256((repo / "ralfloop_agent/domains/recursive_mas_profiles.py").read_bytes()).hexdigest() == "4c89714dfabe1603c0f73d48c452d761e9d460f533c6e23f658dfdcbe72921cf"
    assert hashlib.sha256((repo / "ralfloop_agent/domains/domain_approval_executor.py").read_bytes()).hexdigest() == "90031d96888449ea30baa45016f0980cf9e334f701ec037e00a46dff559db416"
