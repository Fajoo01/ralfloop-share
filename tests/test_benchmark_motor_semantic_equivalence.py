from __future__ import annotations

import json

from tools.benchmark_motor_semantic_equivalence import (
    local_cases,
    offline_token_report,
    sanitize_local_text,
    synthetic_cases,
)


def test_adversarial_corpus_has_minimum_pairs_and_unique_ids():
    cases = synthetic_cases()
    ids = [case.case_id for case in cases]
    assert len(cases) >= 12
    assert len(ids) == len(set(ids))
    joined = " ".join(" ".join([*case.facts, *case.rules]) for case in cases).casefold()
    assert "do not" in joined
    assert "only if" in joined
    assert "29" in joined and "30" in joined
    assert "alice@example.org" in joined
    assert "alicia@example.org" in joined
    assert "may archive" in joined
    assert "human" in joined


def test_sanitizer_preserves_equality_without_publishing_originals():
    first = sanitize_local_text("mail a person@example.org ID 123456 path /home/user/secret")
    again = sanitize_local_text("mail a person@example.org ID 123456 path /home/user/secret")
    other = sanitize_local_text("mail a other@example.org ID 654321 path /home/user/other")
    assert first == again
    assert first != other
    assert "person@example.org" not in first
    assert "123456" not in first
    assert "/home/user/secret" not in first


def test_local_case_loader_hashes_ids_and_sanitizes_text(tmp_path):
    secret = "private-person@example.org"
    record = {
        "session_id": "private-session-id",
        "history": [
            {"role": "user", "content": ("Evidence " + secret + " is pending. ") * 20},
            {"role": "assistant", "content": "Do not send before approval ID 123456."},
        ],
    }
    (tmp_path / "session.json").write_text(json.dumps(record), encoding="utf-8")
    cases = local_cases(tmp_path, 1, min_chars=10)
    assert len(cases) == 1
    case = cases[0]
    assert case.case_id.startswith("local-")
    assert "private-session-id" not in case.case_id
    encoded = json.dumps(case.model_dump(), ensure_ascii=False)
    assert secret not in encoded
    assert "123456" not in encoded


def test_token_only_report_can_never_promote():
    case = synthetic_cases()[0]
    report = offline_token_report([case], lambda value: len(json.dumps(value.model_dump())), use_grammar=False)
    assert report["summary"]["semantic_equivalence_measured"] is False
    assert report["summary"]["promotion_allowed"] is False
    assert report["summary"]["blockers"] == ["live_semantics_not_measured"]
    assert "case_key" in report["observations"][0]
