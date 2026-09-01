from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


SPEC = spec_from_file_location(
    "bottazzi_llm_benchmark",
    Path(__file__).parents[1] / "scripts" / "bottazzi_llm_benchmark.py",
)
assert SPEC and SPEC.loader
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_deterministic_tool_scores():
    assert MODULE.score(
        "shell_json", '{"tool":"shell","args":{},"read_only":true}',
    )["success"]
    assert MODULE.score(
        "tool_retry", '{"tool":"read_file","path":"/tmp/demo.txt"}',
    )["success"]
    assert not MODULE.score("policy_deny", '{"allowed":true,"reason":"ok"}')["success"]


def test_repetitive_requires_all_twelve_rows():
    valid = "\n".join(
        f'{{"command":"echo {number}","timeout":10}}'
        for number in range(1, 13)
    )
    assert MODULE.score("repetitive", valid)["success"]
    assert not MODULE.score("repetitive", valid.splitlines()[0])["success"]
