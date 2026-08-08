from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

from ralfloop_agent.domains.bandi_weekly_research import WeeklyConfig, run_weekly
from ralfloop_agent.domains.source_discovery import SearchProviderUnavailable, SearchResultCandidate


PRIMARY_URL = "https://www.comune.milano.it/bando-giovani-2026"
PRIMARY_TEXT = """
Bando giovani 2026 del Comune di Milano.
Beneficiari ammessi: APS, ETS iscritti al RUNTS e associazioni non profit.
Il progetto sostiene educazione, doposcuola, minori, cultura e inclusione sociale.
Scadenza per la presentazione delle domande: 30 settembre 2026.
Contributo massimo euro 80.000. Spese ammissibili: personale e laboratori.
Durata del progetto: 12 mesi. Documenti: statuto, bilancio e formulario.
"""


class FakeProvider:
    def __init__(
        self,
        *,
        fail_all: bool = False,
        fail_first: bool = False,
        include_aggregator: bool = False,
        title: str = "Bando giovani 2026",
    ) -> None:
        self.calls = 0
        self.fail_all = fail_all
        self.fail_first = fail_first
        self.include_aggregator = include_aggregator
        self.title = title

    def search(self, query: str, *, limit: int):
        self.calls += 1
        if self.fail_all or (self.fail_first and self.calls == 1):
            raise SearchProviderUnavailable("provider_unavailable")
        rows = [SearchResultCandidate("c1", query, self.title, PRIMARY_URL, "APS giovani", "bing", 1)]
        if self.include_aggregator:
            rows.append(SearchResultCandidate("c2", query, "Aggregatore", "https://example.org/lista-bandi", "snippet", "bing", 2))
        return rows[:limit]


class FakeFetcher:
    def __init__(self, root: Path, text: str = PRIMARY_TEXT) -> None:
        self.root = root
        self.text = text
        self.calls: list[str] = []

    def fetch(self, url: str):
        self.calls.append(url)
        cache = self.root / f"fetch-{len(self.calls)}"
        cache.mkdir(parents=True, exist_ok=True)
        content = self.text.encode()
        (cache / "content").write_bytes(content)
        return SimpleNamespace(
            ok=True,
            status="fetched",
            url=url,
            final_url=url,
            content_type="text/html",
            checksum="a" * 64,
            size_bytes=len(content),
            cache_path=str(cache),
            error=None,
        )


class MappingFetcher:
    def __init__(self, root: Path, documents: dict[str, str], *, unavailable: set[str] | None = None) -> None:
        self.root = root
        self.documents = documents
        self.unavailable = unavailable or set()
        self.calls: list[str] = []

    def fetch(self, url: str):
        self.calls.append(url)
        if url in self.unavailable:
            return SimpleNamespace(
                ok=False,
                status="fetch_error",
                url=url,
                final_url=url,
                content_type="application/pdf",
                checksum="",
                size_bytes=0,
                cache_path=None,
                error="fixture_unavailable",
            )
        content = self.documents[url].encode()
        cache = self.root / f"mapped-{len(self.calls)}"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "content").write_bytes(content)
        return SimpleNamespace(
            ok=True,
            status="fetched",
            url=url,
            final_url=url,
            content_type="text/html",
            checksum=(str(len(content)) * 64)[:64],
            size_bytes=len(content),
            cache_path=str(cache),
            error=None,
        )


def config(tmp_path: Path) -> WeeklyConfig:
    return WeeklyConfig(state_dir=tmp_path / "state", max_primary_fetches=5, max_results_per_query=3)


def test_weekly_run_creates_versioned_reports_and_high_priority_new_call(tmp_path: Path) -> None:
    result = run_weekly(config(tmp_path), provider=FakeProvider(), fetcher=FakeFetcher(tmp_path), today=date(2026, 7, 18))

    assert result.ok is True
    assert result.partial is False
    assert result.metrics["sources_discovered"] > 1
    assert result.metrics["duplicates_removed"] > 0
    assert result.metrics["primary_sources_opened"] == 1
    assert result.metrics["new_calls"] == 1
    assert result.metrics["high_priority_calls"] == 1
    assert result.opportunities[0]["lifecycle"] == "NEW"
    assert result.opportunities[0]["priority"] == "HIGH"
    assert Path(result.report_json).is_file()
    assert Path(result.report_markdown).is_file()


def test_second_identical_run_is_not_reported_as_new(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    run_weekly(cfg, provider=FakeProvider(), fetcher=FakeFetcher(tmp_path), today=date(2026, 7, 18))
    result = run_weekly(cfg, provider=FakeProvider(), fetcher=FakeFetcher(tmp_path), today=date(2026, 7, 18))

    assert result.metrics["new_calls"] == 0
    assert result.metrics["updated_calls"] == 0
    assert result.opportunities[0]["lifecycle"] == "UNCHANGED"


def test_changed_primary_content_is_updated_with_visible_change(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    run_weekly(cfg, provider=FakeProvider(), fetcher=FakeFetcher(tmp_path), today=date(2026, 7, 18))
    changed = PRIMARY_TEXT.replace("30 settembre 2026", "20 agosto 2026")
    result = run_weekly(cfg, provider=FakeProvider(), fetcher=FakeFetcher(tmp_path, changed), today=date(2026, 7, 18))

    assert result.metrics["updated_calls"] == 1
    assert result.opportunities[0]["lifecycle"] == "UPDATED"
    assert any(item.startswith("deadline:") for item in result.opportunities[0]["changes"])


def test_single_search_failure_marks_partial_but_keeps_real_results(tmp_path: Path) -> None:
    result = run_weekly(config(tmp_path), provider=FakeProvider(fail_first=True), fetcher=FakeFetcher(tmp_path), today=date(2026, 7, 18))

    assert result.ok is True
    assert result.partial is True
    assert result.metrics["calls_examined"] == 1
    assert result.errors[0]["stage"] == "search"


def test_total_search_backend_failure_is_explicit(tmp_path: Path) -> None:
    result = run_weekly(config(tmp_path), provider=FakeProvider(fail_all=True), fetcher=FakeFetcher(tmp_path), today=date(2026, 7, 18))

    assert result.ok is False
    assert result.status == "failed"
    assert any("bandi_search_backend_unavailable" in item["reason"] for item in result.errors)
    assert Path(result.report_json).is_file()


def test_discovery_only_aggregator_is_not_fetched_or_accepted(tmp_path: Path) -> None:
    fetcher = FakeFetcher(tmp_path)
    result = run_weekly(config(tmp_path), provider=FakeProvider(include_aggregator=True), fetcher=fetcher, today=date(2026, 7, 18))

    assert fetcher.calls == [PRIMARY_URL]
    assert all(item["primary_url"] == PRIMARY_URL for item in result.opportunities)
    assert any(item["reason"] == "discovery_only_no_primary_confirmation" for item in result.discarded)


def test_score_is_rule_based_not_probability_of_winning(tmp_path: Path) -> None:
    result = run_weekly(config(tmp_path), provider=FakeProvider(), fetcher=FakeFetcher(tmp_path), today=date(2026, 7, 18))
    opportunity = result.opportunities[0]

    assert opportunity["score"] == sum(rule["points"] for rule in opportunity["score_rules"])
    assert "probability" not in str(opportunity).lower()


def test_beneficiary_list_is_not_promoted_as_open_call(tmp_path: Path) -> None:
    text = """
    Elenco delle associazioni beneficiarie di contributi. Bando concluso.
    APS ed ETS ammessi con deliberazione del 30 settembre 2026.
    """
    result = run_weekly(
        config(tmp_path),
        provider=FakeProvider(title="Elenco delle associazioni beneficiarie di contributi"),
        fetcher=FakeFetcher(tmp_path, text),
        today=date(2026, 7, 18),
    )

    assert result.opportunities == []
    assert result.metrics["new_calls"] == 0
    assert any(item["reason"] == "historical_result_or_non_call_document" for item in result.discarded)


def test_unlabelled_future_date_does_not_create_fake_deadline(tmp_path: Path) -> None:
    text = """
    Bando giovani 2026. APS ed ETS del terzo settore.
    Il progetto è stato deliberato il 30 settembre 2026.
    """
    result = run_weekly(
        config(tmp_path),
        provider=FakeProvider(),
        fetcher=FakeFetcher(tmp_path, text),
        today=date(2026, 7, 18),
    )

    assert result.opportunities == []
    assert any(item["reason"] == "call_status_not_verified" for item in result.discarded)


def test_expired_call_is_discarded_not_high_priority(tmp_path: Path) -> None:
    text = """
    Bando pubblico per APS, ETS e associazioni non profit.
    Scadenza per la presentazione delle domande: 30 giugno 2026.
    """
    result = run_weekly(
        config(tmp_path),
        provider=FakeProvider(),
        fetcher=FakeFetcher(tmp_path, text),
        today=date(2026, 7, 18),
    )

    assert result.opportunities == []
    assert result.metrics["high_priority_calls"] == 0
    assert any(item["reason"] == "call_not_actionable" for item in result.discarded)


def test_generic_forms_portal_is_not_promoted(tmp_path: Path) -> None:
    text = """
    Servizi online del Comune di Milano. Modulo albo associazioni.
    Bando cultura. Scadenza 31 dicembre 2026. APS e RUNTS.
    """
    result = run_weekly(
        config(tmp_path),
        provider=FakeProvider(title="Servizi online del Comune di Milano"),
        fetcher=FakeFetcher(tmp_path, text),
        today=date(2026, 7, 18),
    )

    assert result.opportunities == []
    assert any(item["reason"] == "historical_result_or_non_call_document" for item in result.discarded)


def test_unrelated_association_reference_is_not_eligibility(tmp_path: Path) -> None:
    text = """
    Bando pubblico voucher per enti accreditati e persone con disabilità.
    Presentazione delle domande: scade il 26 febbraio 2027.
    La normativa cita consorzi e associazioni fra amministrazioni pubbliche.
    """
    result = run_weekly(
        config(tmp_path),
        provider=FakeProvider(title="Bando pubblico voucher servizi"),
        fetcher=FakeFetcher(tmp_path, text),
        today=date(2026, 7, 18),
    )

    assert result.opportunities == []
    assert any(item["reason"] == "beneficiary_eligibility_not_verified" for item in result.discarded)


def test_awarded_call_is_closed_even_if_project_end_is_future(tmp_path: Path) -> None:
    text = """
    Avviso pubblico per associazioni e APS. Stato: AGGIUDICATO.
    Data Scadenza: 08/04/2026. Le attività terminano il 31/07/2026.
    Possono presentare domanda le associazioni senza scopo di lucro.
    """
    result = run_weekly(
        config(tmp_path),
        provider=FakeProvider(title="Avviso pubblico contributi Municipio 2"),
        fetcher=FakeFetcher(tmp_path, text),
        today=date(2026, 7, 18),
    )

    assert result.opportunities == []
    assert any(item["reason"] == "call_not_actionable" for item in result.discarded)


def _linked_documents() -> tuple[str, str, dict[str, str]]:
    attachment_url = "https://www.comune.milano.it/docs/avviso.pdf"
    primary = f"""
    <html><body>
    <h1>Bando giovani 2026</h1>
    <p>Avviso pubblico. Possono presentare domanda APS ed ETS iscritti al RUNTS.</p>
    <p>Scadenza per la presentazione delle domande: 30 settembre 2026.</p>
    <a href="{attachment_url}">Avviso ufficiale</a>
    </body></html>
    """
    attachment = """
    Avviso pubblico Bando giovani 2026. Beneficiari ammessi: APS ed ETS iscritti al RUNTS.
    Territorio Municipio 2. Spese ammissibili: personale e laboratori.
    """
    return attachment_url, primary, {PRIMARY_URL: primary, attachment_url: attachment}


def test_bandi_hybrid_retrieval_preserves_evidence_provenance(tmp_path: Path) -> None:
    _, _, documents = _linked_documents()
    calls = []

    def semantic(query, rows):
        calls.append((query, rows))
        return {
            "ok": True,
            "output": {
                "ranked": [
                    {"document_id": item["document_id"], "score": 1 / index}
                    for index, item in enumerate(rows, 1)
                ]
            },
        }

    result = run_weekly(
        config(tmp_path),
        provider=FakeProvider(),
        fetcher=MappingFetcher(tmp_path, documents),
        semantic_invoke=semantic,
        today=date(2026, 7, 18),
    )
    opportunity = result.opportunities[0]
    evidence = opportunity["retrieval"]["evidence"]
    assert calls
    assert opportunity["retrieval_scope"] == "bandi_only"
    assert opportunity["semantic_retrieval_used"] is True
    assert opportunity["retrieval"]["mode"] == "hybrid_rrf"
    assert opportunity["document_completeness"]["status"] == "complete"
    assert len({item["document_id"] for item in evidence}) == 2
    assert all(item["source_id"] and item["canonical_url"] for item in evidence)
    assert all(item["text_hash"] for item in evidence)
    assert all(item["chunk_id"].startswith("E") for item in evidence)
    allowed = set(opportunity["citation_contract"]["allowed_evidence_ids"])
    cited = {
        citation_id
        for values in opportunity["critical_claim_citations"].values()
        for citation_id in values
    }
    assert cited <= allowed
    assert opportunity["citation_contract"]["llm_constructed_urls"] is False
    assert opportunity["citation_provenance"]["deadline"][0]["text_hash"]


def test_missing_critical_attachment_makes_eligibility_conditional(tmp_path: Path) -> None:
    attachment_url, _, documents = _linked_documents()
    result = run_weekly(
        config(tmp_path),
        provider=FakeProvider(),
        fetcher=MappingFetcher(tmp_path, documents, unavailable={attachment_url}),
        semantic_invoke=lambda *_: (_ for _ in ()).throw(AssertionError()),
        today=date(2026, 7, 18),
    )
    opportunity = result.opportunities[0]
    assert result.partial is True
    assert opportunity["document_completeness"]["status"] == "partial_critical"
    assert opportunity["eligibility_status"] == "conditional_missing_primary_documents"
    assert opportunity["tiremm_compatibility"] == "conditional_missing_primary_documents"
    assert "missing_critical_primary_documents" in opportunity["criticalities"]


def test_semantic_failure_has_explicit_bm25_fallback(tmp_path: Path) -> None:
    _, _, documents = _linked_documents()

    def unavailable(_query, _rows):
        return {"ok": False, "error_type": "tool_timeout"}

    result = run_weekly(
        config(tmp_path),
        provider=FakeProvider(),
        fetcher=MappingFetcher(tmp_path, documents),
        semantic_invoke=unavailable,
        today=date(2026, 7, 18),
    )
    retrieval = result.opportunities[0]["retrieval"]
    assert retrieval["mode"] == "bm25_fallback_explicit"
    assert retrieval["fallback_used"] is True
    assert "tool_timeout" in retrieval["fallback_reason"]
    assert result.opportunities[0]["semantic_retrieval_used"] is False
