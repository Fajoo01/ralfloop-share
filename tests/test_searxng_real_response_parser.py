from ralfloop_agent.domains.source_discovery import _parse_searxng_results, canonicalize_url


def test_canonicalize_removes_tracking_and_fragment():
    url = "https://Example.org/doc.pdf?utm_source=x&id=3#section"
    assert canonicalize_url(url) == "https://example.org/doc.pdf?id=3"


def test_parser_ignores_missing_url_and_dedups():
    rows = [
        {"title": "No URL"},
        {"title": "Doc", "url": "https://example.org/doc.pdf?utm_source=x", "engine": "duckduckgo"},
        {"title": "Doc dup", "url": "https://example.org/doc.pdf", "engine": "brave"},
    ]
    out = _parse_searxng_results("q", rows, 10)
    assert len(out) == 1
    assert out[0].metadata["canonical_url"] == "https://example.org/doc.pdf"


def test_parser_preserves_semantic_query_params():
    rows = [
        {"title": "Doc", "url": "https://example.org/download?id=10&utm_campaign=x"},
        {"title": "Other", "url": "https://example.org/download?id=11"},
    ]
    out = _parse_searxng_results("q", rows, 10)
    assert len(out) == 2
