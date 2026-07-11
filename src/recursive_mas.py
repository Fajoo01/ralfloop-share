from __future__ import annotations

import warnings

from src.text_mas_proxy import build_text_mas_trace, summarize_text_mas_trace


def build_recursive_mas_trace(*args, **kwargs):
    warnings.warn(
        "build_recursive_mas_trace is deprecated; use build_text_mas_trace. "
        "This trace is not native RecursiveMAS hidden-state recursion.",
        DeprecationWarning,
        stacklevel=2,
    )
    return build_text_mas_trace(*args, **kwargs)


def summarize_recursive_mas_trace(*args, **kwargs):
    warnings.warn(
        "summarize_recursive_mas_trace is deprecated; use summarize_text_mas_trace.",
        DeprecationWarning,
        stacklevel=2,
    )
    return summarize_text_mas_trace(*args, **kwargs)
