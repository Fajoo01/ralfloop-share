from openshell_backend.skills.abc_relcalc import (
    EvidenceItem,
    calculate_relation,
    calculate_relation_dict,
)


def test_observed_facts_can_raise_score_with_safe_light_action():
    result = calculate_relation(
        [
            EvidenceItem(
                kind="observed_fact",
                description="Direct non-pressing invitation accepted",
                weight=18,
                confidence=0.9,
            ),
            EvidenceItem(
                kind="observed_fact",
                description="Repeated practical trust gesture",
                weight=12,
                confidence=0.8,
            ),
        ]
    )

    assert result.score > 70
    assert result.confidence >= 0.8
    assert result.next_safe_action == "light_non_pressing_presence"
    assert "weak_evidence_high_score" not in result.bias_flags


def test_inference_is_capped_and_cannot_create_certainty():
    result = calculate_relation(
        [
            {
                "kind": "inference",
                "description": "Interpretation without observable confirmation",
                "weight": 80,
                "confidence": 1.0,
            }
        ]
    )

    assert result.score <= 70
    assert result.confidence <= 0.5
    assert "weak_evidence_high_score" in result.bias_flags
    assert "too_many_inferences" in result.bias_flags


def test_contradiction_lowers_score_and_confidence():
    result = calculate_relation(
        [
            {
                "kind": "observed_fact",
                "description": "Warm observable interaction",
                "weight": 20,
                "confidence": 0.8,
            },
            {
                "kind": "contradiction",
                "description": "Contradictory distancing signal",
                "weight": 25,
                "confidence": 0.8,
            },
        ]
    )

    assert result.score < 60
    assert result.confidence < 0.8
    assert "contradiction_present" in result.bias_flags
    assert result.next_safe_action == "do_nothing_active"


def test_mind_reading_and_surveillance_tags_force_safe_observation():
    result = calculate_relation(
        [
            {
                "kind": "inference",
                "description": "Tone interpretation plus online access checking",
                "weight": 30,
                "confidence": 0.7,
                "tags": ["tono", "accesso_online"],
            }
        ]
    )

    assert "mind_reading_risk" in result.bias_flags
    assert "surveillance_risk" in result.bias_flags
    assert result.next_safe_action == "collect_observable_evidence"


def test_empty_evidence_is_neutral_and_safe():
    result = calculate_relation([])

    assert result.score == 50
    assert result.confidence == 0.0
    assert result.next_safe_action == "do_nothing_active"


def test_dict_api_is_stable():
    result = calculate_relation_dict(
        [
            {
                "kind": "external_signal",
                "description": "External but not decisive signal",
                "weight": 10,
                "confidence": 0.7,
                "tags": [],
            }
        ]
    )

    assert set(result) == {
        "score",
        "confidence",
        "bias_flags",
        "next_safe_action",
        "evidence_count",
        "summary",
    }
    assert result["evidence_count"] == 1
