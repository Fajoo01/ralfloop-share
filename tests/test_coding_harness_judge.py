import json

import pytest

from ralfloop_agent.coding_harness import (
    CodingReview,
    coding_prompt,
    parse_coding_review,
)


def test_parse_pass():
    review = parse_coding_review(
        json.dumps({"verdict": "pass", "issues": [], "summary": ""})
    )
    assert isinstance(review, CodingReview)
    assert review.verdict == "pass"


def test_parse_repair():
    review = parse_coding_review(json.dumps({
        "verdict": "repair",
        "issues": [{
            "type": "correctness",
            "severity": "high",
            "file": "calc.py",
            "reason": "Addition still returns subtraction result.",
            "repair_instruction": "Replace subtraction with addition.",
        }],
        "summary": "",
    }))
    assert review.verdict == "repair"
    assert review.issues[0].type == "correctness"


def test_pass_cannot_have_issues():
    with pytest.raises(ValueError):
        parse_coding_review(json.dumps({
            "verdict": "pass",
            "issues": [{
                "type": "correctness",
                "severity": "high",
                "file": "x.py",
                "reason": "Material defect remains.",
                "repair_instruction": "Fix the defect.",
            }],
            "summary": "",
        }))


def test_prompt_contains_authoritative_validator():
    prompt = coding_prompt({
        "task": "Fix add",
        "diff": "- a-b\\n+ a+b",
        "validator": {"status": "green"},
    })
    assert "Deterministic validator results are authoritative facts." in prompt
    assert "\"status\":\"green\"" in prompt

def test_resident_ds4_default_budget_is_256():
    from ralfloop_agent.coding_harness import ResidentDs4CodingJudge

    judge = ResidentDs4CodingJudge()
    assert judge.max_output_tokens == 256
