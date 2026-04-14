import json
import sys
from pathlib import Path

from openshell_backend.skill_grammar_rag import maybe_answer_grammar_request

cases = json.loads(Path("tests/grammar/cases.json").read_text(encoding="utf-8"))
failures = []

def find_item(parsed, token: str):
    for item in parsed:
        if isinstance(item, dict) and str(item.get("token", "")) == token:
            return item
    return None

for case in cases:
    out = maybe_answer_grammar_request(case["input"])

    if case.get("must_not_be_none") and out is None:
        failures.append(f'{case["name"]}: output is None')
        continue

    try:
        parsed = json.loads(out)
    except Exception as e:
        failures.append(f'{case["name"]}: invalid json output: {e}')
        continue

    if not isinstance(parsed, list):
        failures.append(f'{case["name"]}: output is not a list')
        continue

    for expected in case.get("must_have_items", []):
        tok = str(expected.get("token", ""))
        got = find_item(parsed, tok)
        if got is None:
            failures.append(f'{case["name"]}: missing token {tok!r}')
            continue

        for key, exp_val in expected.items():
            if key == "token":
                continue
            got_val = str(got.get(key, ""))
            if got_val != str(exp_val):
                failures.append(
                    f'{case["name"]}: token {tok!r} field {key!r} expected {exp_val!r} got {got_val!r}'
                )

if failures:
    print("GRAMMAR_TESTS_KO")
    for f in failures:
        print(" -", f)
    sys.exit(1)

print("GRAMMAR_TESTS_OK")
