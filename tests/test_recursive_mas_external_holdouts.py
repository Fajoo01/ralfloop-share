from __future__ import annotations

import hashlib

from ralfloop_agent.domains.recursive_mas_domain_dataset import CATEGORIES, generate_dataset
from ralfloop_agent.domains.recursive_mas_external_holdouts import (
    FINAL_DOMAINS,
    RESERVE_DOMAINS,
    contamination_report,
    generate_external_holdouts,
    jsonl_bytes,
    validate_external_holdouts,
)


def test_external_domain_uniqueness():
    ids = [domain.domain_id for domain in (*FINAL_DOMAINS, *RESERVE_DOMAINS)]
    assert len(ids) == len(set(ids)) == 10
    previous, _ = generate_dataset()
    assert not set(ids) & {case["domain_id"] for case in previous}


def test_final_a_size_categories_and_conclusions():
    final, _ = generate_external_holdouts()
    assert len(final) == 36
    assert {category: sum(case["category"] == category for case in final) for category in CATEGORIES} == {category: 6 for category in CATEGORIES}
    assert {name: sum(case["expected_recommendation_type"] == name for case in final) for name in ("favorable", "contrary", "conditional")} == {name: 12 for name in ("favorable", "contrary", "conditional")}


def test_reserve_b_size_categories_and_conclusions():
    _, reserve = generate_external_holdouts()
    assert len(reserve) == 24
    assert {category: sum(case["category"] == category for case in reserve) for category in CATEGORIES} == {category: 4 for category in CATEGORIES}
    assert {name: sum(case["expected_recommendation_type"] == name for case in reserve) for name in ("favorable", "contrary", "conditional")} == {name: 8 for name in ("favorable", "contrary", "conditional")}


def test_final_reserve_disjointness():
    final, reserve = generate_external_holdouts()
    report = validate_external_holdouts(final, reserve)
    assert set(report.values()) >= {0, 24, 36}


def test_external_contamination_audit_against_original_240():
    final, reserve = generate_external_holdouts()
    previous, _ = generate_dataset()
    report = contamination_report(final, reserve, previous)
    assert not report["FINAL_A"]["contaminated"]
    assert not report["RESERVE_B"]["contaminated"]
    assert report["FINAL_A"]["max_5gram_similarity"] < 0.55
    assert report["RESERVE_B"]["max_5gram_similarity"] < 0.55


def test_dataset_generation_is_deterministic():
    first = generate_external_holdouts()
    second = generate_external_holdouts()
    assert hashlib.sha256(jsonl_bytes(first[0])).digest() == hashlib.sha256(jsonl_bytes(second[0])).digest()
    assert hashlib.sha256(jsonl_bytes(first[1])).digest() == hashlib.sha256(jsonl_bytes(second[1])).digest()
