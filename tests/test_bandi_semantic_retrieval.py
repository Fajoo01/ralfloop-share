from __future__ import annotations

from dataclasses import replace

from ralfloop_agent.domains.bandi_semantic_retrieval import (
    AvailableDocument,
    ExpectedDocument,
    assess_document_completeness,
    bm25_rank,
    claim_citations_from_evidence,
    detect_document_change,
    discover_official_documents,
    evidence_context,
    guard_critical_claims,
    hybrid_retrieve,
    make_evidence,
    retrieve_for_bandi,
    validate_citations,
)


def evidence(source: str, document: str, text: str, *, page: int | None = None, section: str | None = None):
    return make_evidence(
        source_id=source,
        document_id=document,
        canonical_url_value=f"https://bandi.regione.lombardia.it/{document}",
        source_type="pdf" if page else "web",
        text=text,
        page=page,
        section=section,
        anchor="a1",
    )


def test_provenance_survives_semantic_ranking() -> None:
    first = evidence("S1", "D1", "La scadenza è il 30 settembre 2026.", page=7, section="Termini")
    second = evidence("S2", "D2", "Testo irrilevante.")

    def semantic(_query, documents):
        return {"ranked": [{"document_id": documents[0]["document_id"], "score": 0.91}]}

    result = hybrid_retrieve("scadenza", [first, second], semantic_invoke=semantic, top_k=1)
    ranked = result.evidence[0]
    assert ranked.source_id == "S1"
    assert ranked.document_id == "D1"
    assert ranked.page == 7
    assert ranked.section == "Termini"
    assert ranked.anchor == "a1"
    assert ranked.text_hash == first.text_hash
    assert ranked.canonical_url == first.canonical_url


def test_semantic_output_cannot_reconstruct_or_invent_source() -> None:
    row = evidence("S1", "D1", "Fonte primaria.")

    def semantic(_query, _documents):
        return {"ranked": [{"document_id": "FOREIGN", "score": 9}, {"document_id": row.chunk_id, "score": 1}]}

    result = hybrid_retrieve("fonte", [row], semantic_invoke=semantic)
    assert result.invented_document_ids == ("FOREIGN",)
    assert result.evidence[0].source_id == "S1"


def test_evidence_context_uses_canonical_ids_not_llm_urls() -> None:
    row = evidence("S1", "D1", "Fonte primaria.", page=2, section="Beneficiari")
    context = evidence_context([row])
    assert f"[EVIDENCE:{row.chunk_id}]" in context
    assert "source_id: S1" in context
    assert "page: 2" in context
    assert f"text_hash: {row.text_hash}" in context


def test_invented_citation_is_blocked() -> None:
    row = evidence("S1", "D1", "Fonte primaria.")
    validation = validate_citations([row.chunk_id, "EINVENTED"], [row])
    assert validation.valid is False
    assert validation.invented_citation_ids == ("EINVENTED",)


def test_valid_citation_renders_full_deterministic_provenance() -> None:
    row = evidence("S1", "D1", "Fonte primaria.", page=3, section="Requisiti")
    validation = validate_citations([row.chunk_id], [row])
    assert validation.valid is True
    assert validation.provenance[0]["source_id"] == "S1"
    assert validation.provenance[0]["page"] == 3
    assert validation.provenance[0]["text_hash"] == row.text_hash


def test_claim_citations_are_selected_from_evidence_allowlist() -> None:
    eligibility = evidence("S1", "D1", "Beneficiari ammessi: APS iscritte al RUNTS.")
    deadline = evidence("S2", "D2", "Scadenza per le domande: 30 settembre 2026.", page=4)
    citations, provenance = claim_citations_from_evidence([eligibility, deadline])
    allowed = {eligibility.chunk_id, deadline.chunk_id}
    assert set(citations["eligibility"]) <= allowed
    assert set(citations["deadline"]) <= allowed
    assert provenance["deadline"][0]["page"] == 4
    assert provenance["eligibility"][0]["text_hash"] == eligibility.text_hash


def test_hybrid_union_deduplicates_by_immutable_chunk_id() -> None:
    first = evidence("S1", "D1", "Scadenza 30 settembre 2026.")
    second = evidence("S2", "D2", "Beneficiari APS.")

    def semantic(_query, _documents):
        return {"ranked": [{"document_id": first.chunk_id, "score": 1}, {"document_id": first.chunk_id, "score": .9}, {"document_id": second.chunk_id, "score": .8}]}

    result = hybrid_retrieve("scadenza", [first, second], semantic_invoke=semantic, top_k=2)
    assert len({item.chunk_id for item in result.evidence}) == 2
    assert result.mode == "hybrid_rrf"


def test_semantic_unavailable_has_explicit_bm25_fallback() -> None:
    first = evidence("S1", "D1", "Scadenza 30 settembre 2026.")

    def unavailable(_query, _documents):
        return {"ok": False, "error_type": "tool_timeout"}

    result = hybrid_retrieve("scadenza", [first], semantic_invoke=unavailable)
    assert result.mode == "bm25_fallback_explicit"
    assert result.fallback_used is True
    assert "tool_timeout" in str(result.fallback_reason)


def test_small_single_document_uses_simple_bm25_path() -> None:
    first = evidence("S1", "D1", "Scadenza breve.")
    result = retrieve_for_bandi("scadenza", [first], semantic_invoke=lambda *_: (_ for _ in ()).throw(AssertionError()))
    assert result.mode == "bm25_small_corpus"
    assert result.semantic_status == "not_requested"


def test_completeness_complete() -> None:
    expected = [ExpectedDocument("official_call_text", "https://example.org/bando.pdf", True)]
    available = [AvailableDocument("D1", "official_call_text", "https://example.org/bando.pdf", "abc")]
    result = assess_document_completeness(expected, available)
    assert result.status == "complete"
    assert result.eligibility_status == "evidence_available"


def test_completeness_partial_critical_for_missing_call_text() -> None:
    expected = [ExpectedDocument("official_call_text", "https://example.org/bando.pdf", True)]
    result = assess_document_completeness(expected, [])
    assert result.status == "partial_critical"
    assert result.eligibility_status == "conditional_missing_primary_documents"


def test_completeness_does_not_substitute_same_type_at_different_url() -> None:
    expected = [ExpectedDocument("official_call_text", "https://example.org/allegato.pdf", True)]
    available = [AvailableDocument("D1", "official_call_text", "https://example.org/pagina", "abc")]
    result = assess_document_completeness(expected, available)
    assert result.status == "partial_critical"
    assert result.missing_documents == tuple(expected)


def test_missing_critical_document_forces_unknown_claims() -> None:
    row = evidence("S1", "D1", "APS ammesse.")
    completeness = assess_document_completeness(
        [ExpectedDocument("official_call_text", "https://example.org/bando.pdf", True)],
        [],
    )
    claims, _ = guard_critical_claims(
        {"eligibility": "yes", "deadline": "2026-09-30"},
        {"eligibility": [row.chunk_id], "deadline": [row.chunk_id]},
        [row],
        completeness,
    )
    assert claims["eligibility"] == "unknown"
    assert claims["deadline"] == "2026-09-30"
    assert claims["eligibility_status"] == "conditional_missing_primary_documents"


def test_missing_citation_forces_unknown_in_complete_call() -> None:
    row = evidence("S1", "D1", "APS ammesse.")
    completeness = assess_document_completeness(
        [ExpectedDocument("official_call_text", row.canonical_url, True)],
        [AvailableDocument("D1", "official_call_text", row.canonical_url, "abc")],
    )
    claims, validations = guard_critical_claims(
        {"eligibility": "yes"}, {"eligibility": []}, [row], completeness
    )
    assert claims["eligibility"] == "unknown"
    assert validations["eligibility"].valid is False


def test_empty_claim_does_not_require_fabricated_citation() -> None:
    row = evidence("S1", "D1", "Nessuna spesa esclusa esplicita.")
    completeness = assess_document_completeness([], [])
    claims, validations = guard_critical_claims(
        {"excluded_expenses": []}, {}, [row], completeness
    )
    assert claims["excluded_expenses"] == []
    assert validations == {}


def test_document_change_detection() -> None:
    old = AvailableDocument("D1", "official_call_text", "https://example.org/bando", "old")
    same = replace(old)
    changed = replace(old, content_hash="new")
    assert detect_document_change(None, old) == "new"
    assert detect_document_change(old, same) == "unchanged"
    assert detect_document_change(old, changed) == "updated"


def test_official_document_discovery_is_bounded_and_classified() -> None:
    html = """
    <a href="/docs/avviso.pdf">Avviso ufficiale</a>
    <a href="/docs/faq.pdf">FAQ ufficiali</a>
    <a href="https://evil.example/allegato.pdf">Allegato esterno</a>
    <a href="/news">Notizia</a>
    """
    rows = discover_official_documents(html, "https://bandi.regione.lombardia.it/call/1")
    assert [(row.document_type, row.critical) for row in rows] == [
        ("official_call_text", True),
        ("official_faq", False),
    ]


def test_bm25_is_deterministic() -> None:
    first = evidence("S1", "D1", "La scadenza è il 30 settembre.")
    second = evidence("S2", "D2", "Elenco beneficiari.")
    assert [row.chunk_id for row in bm25_rank("scadenza", [first, second])] == [first.chunk_id, second.chunk_id]
