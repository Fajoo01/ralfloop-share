from __future__ import annotations

import pytest

from ralfloop_agent.local_arch.contracts import ContractError
from ralfloop_agent.local_arch.router import ToolRegistry, parse_native_function_call


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry.load("config/local_arch_tools_v1.json")


@pytest.mark.parametrize("raw,action,target", [
    ("<start_function_call>call:AF{target:<escape>shortest_path</escape>,confidence:0.94}", "AF", "shortest_path"),
    ("<start_function_call>call:ET{target:<escape>graph_solver<escape>,confidence:0.91}", "ET", "graph_solver"),
    ("<start_function_call>call:VR{target:<escape>visual_rag</escape>,confidence:0.9}", "VR", "visual_rag"),
    ("<start_function_call>call:AP{target:<escape>social_publish</escape>,confidence:1}", "AP", "social_publish"),
])
def test_valid_native_calls(registry, raw, action, target):
    route = parse_native_function_call(raw, registry)
    assert (route.a, route.t) == (action, target)


@pytest.mark.parametrize("raw,error", [
    ("<start_function_call>call:{target:<escape>x</escape>,confidence:1}", "missing_tool_name"),
    ("<start_function_call>call:XX{target:<escape>x</escape>,confidence:1}", "unknown_capability"),
    ("<start_function_call>call:AF{target:<escape>x</escape>,confidence:1}<start_function_call>call:FN{target:<escape>x</escape>,confidence:1}", "native_call_count"),
    ("<start_function_response>ok<end_function_response>", "function_response_rejected"),
    ("<start_function_call>call:AF{target:<escape>x</escape>,confidence:1", "malformed_native_braces"),
    ("<start_function_call>call:AF{target:<escape>x,confidence:1}", "malformed_escape"),
    ("scelgo <start_function_call>call:AF{target:<escape>x</escape>,confidence:1}", "unexpected_native_prefix"),
    ("<start_function_call>call:AF{target:<escape>x</escape>,confidence:1} testo", "malformed_native_braces"),
    ("<start_function_call>call:AF{target:<escape>x</escape>,confidence:1.2}", "invalid_confidence"),
    ("<start_function_call>call:ET{target:<escape>invented</escape>,confidence:1}", "hallucinated_or_unavailable_tool"),
    ("<start_function_call>call:AF{properties:<escape>x</escape>,confidence:1}", "schema_echo"),
    ("<start_function_call>call:ET|AF{target:<escape>x</escape>,confidence:1}", "pipe_enum"),
    ("<start_function_call>call:AF{target:<escape>x</escape>,confidence:", "malformed_native_braces"),
])
def test_invalid_native_calls(registry, raw, error):
    with pytest.raises(ContractError, match=error):
        parse_native_function_call(raw, registry)


def test_native_end_marker_is_accepted(registry):
    route = parse_native_function_call(
        "<start_function_call>call:AF{target:<escape>shortest_path<escape>,confidence:0.94}<end_function_call>",
        registry,
    )
    assert route.t == "shortest_path"


def test_missing_target_can_be_repaired_only_from_unique_registry_target(registry):
    raw = "<start_function_call>call:VR{confidence:1}"
    with pytest.raises(ContractError, match="invalid_native_schema"):
        parse_native_function_call(raw, registry)
    route = parse_native_function_call(raw, registry, repair_unique_target=True)
    assert (route.a, route.t, route.c) == ("VR", "visual_rag", 1.0)
