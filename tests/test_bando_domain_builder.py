from __future__ import annotations

import json
from pathlib import Path

import yaml

from ralfloop_agent.domains.bando_domain_builder import BandoBuildRequest, BandoDomainBuilder
from ralfloop_agent.domains.storage import sha256_tree

ACT_DOC = Path('/home/bandi/Scaricati/Regolamento_BandoACT_2026.pdf')
PONTI_DOC = Path('/home/bandi/Scaricati/Bando-Nuovi-ponti-culturali-2.pdf')
BUILDER_SOURCE = Path('ralfloop_agent/domains/bando_domain_builder.py')


def test_builder_core_has_no_act_hardcoding():
    source = BUILDER_SOURCE.read_text(encoding='utf-8')
    assert 'fondazione_unipolis_act_2026' not in source
    assert 'ACT 2026' not in source
    assert 'Fondazione Unipolis' not in source


def test_act_rebuilds_from_documents_without_hardcoded_identity(tmp_path: Path):
    result = BandoDomainBuilder(tmp_path / 'domains').build_draft(BandoBuildRequest(primary_document=str(ACT_DOC), output_root=str(tmp_path / 'domains')))

    assert result.ok is True
    assert result.bando_id.startswith('fondazione_unipolis_')
    assert 'act_aspirare_coinvolgere_trasformare' in result.bando_id
    assert result.identity['issuer'] == 'Fondazione Unipolis'
    assert result.identity['deadline'] == '2026-04-09T13:00:00+02:00'
    assert {rule['rule_id'] for rule in result.rules} >= {'application_deadline', 'contribution_max_rate', 'contribution_max_amount'}
    assert not (tmp_path / 'domains/active/bandi').exists() or not list((tmp_path / 'domains/active/bandi').glob('*/*'))


def test_non_official_source_does_not_create_binding_rule(tmp_path: Path):
    source = tmp_path / 'notes.md'
    source.write_text('LLM generated summary. Bando X. Contributo massimo 99%.', encoding='utf-8')

    result = BandoDomainBuilder(tmp_path / 'domains').build_draft(BandoBuildRequest(primary_document=str(source), output_root=str(tmp_path / 'domains')))

    assert result.ok is False
    assert result.status == 'identity_incomplete'
    assert result.rules == []


def test_missing_category_stays_uncovered(tmp_path: Path):
    result = BandoDomainBuilder(tmp_path / 'domains').build_draft(BandoBuildRequest(primary_document=str(PONTI_DOC), output_root=str(tmp_path / 'domains')))

    assert result.ok is True
    assert 'cofinancing' in result.uncovered_categories
    assert all(rule['category'] != 'cofinancing' for rule in result.rules)


def test_ambiguous_calculation_basis_is_marked(tmp_path: Path):
    source = tmp_path / 'call.md'
    source.write_text('Bando Test 2026\nFondazione Demo indice il bando. Richiesta contributo fino a un massimo del 50%.', encoding='utf-8')

    result = BandoDomainBuilder(tmp_path / 'domains').build_draft(BandoBuildRequest(primary_document=str(source), output_root=str(tmp_path / 'domains')))

    assert result.calculations
    assert result.validation['warnings']
    extraction = BandoDomainBuilder(tmp_path / 'domains').extract_calculations(result_identity(result), BandoDomainBuilder(tmp_path / 'domains').classify_documents(BandoBuildRequest(primary_document=str(source))))
    assert extraction.ambiguous
    assert extraction.ambiguous[0]["reason"] == "ambiguous_calculation_basis"
    assert extraction.ambiguous[0]["jury_required"] is True


def result_identity(result):
    from ralfloop_agent.domains.bando_domain_builder import BandoIdentityProposal
    return BandoIdentityProposal(**result.identity)
