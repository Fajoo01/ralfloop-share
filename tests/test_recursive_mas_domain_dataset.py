import json

from ralfloop_agent.domains.recursive_mas_domain_dataset import (
    CATEGORIES,
    CRITIC_FIELDS,
    DOMAINS,
    PLANNER_FIELDS,
    SOLVER_FIELDS,
    deterministic_splits,
    generate_dataset,
    validate_dataset,
    write_dataset,
)


def test_dataset_has_required_distribution_and_synthetic_domains():
    cases, traces = generate_dataset()
    report = validate_dataset(cases, traces)
    assert report["case_count"] == 240
    assert report["category_counts"] == {category: 40 for category in CATEGORIES}
    assert {case["domain_id"] for case in cases} == set(DOMAINS)
    assert report["duplicates"] == 0


def test_split_is_deterministic_and_isolated():
    cases, _ = generate_dataset()
    one = deterministic_splits(cases)
    two = deterministic_splits(list(reversed(cases)))
    assert one == two
    assert {name: len(ids) for name, ids in one.items()} == {"train": 168, "validation": 36, "test": 36}
    assert not set(one["train"]) & set(one["validation"])
    assert not set(one["train"]) & set(one["test"])
    assert not set(one["validation"]) & set(one["test"])


def test_role_targets_are_structured_without_free_chain_of_thought():
    _, traces = generate_dataset()
    for trace in traces:
        assert set(trace["planner_target"]) == PLANNER_FIELDS
        assert set(trace["critic_target"]) == CRITIC_FIELDS
        assert set(trace["solver_target"]) == SOLVER_FIELDS
        serialized = json.dumps(trace, ensure_ascii=False).lower()
        assert "chain_of_thought" not in serialized


def test_dataset_writes_hash_manifest(tmp_path):
    manifest = write_dataset(tmp_path)
    assert manifest["case_count"] == 240
    assert manifest["split_sizes"] == {"train": 168, "validation": 36, "test": 36}
    assert len(manifest["dataset_sha256"]) == 64
    assert manifest["synthetic_only"] is True
    assert (tmp_path / "recursive_domain_adapter_cases.jsonl").is_file()
    assert (tmp_path / "recursive_domain_adapter_traces.jsonl").is_file()
    assert (tmp_path / "recursive_domain_adapter_splits.json").is_file()
