from __future__ import annotations

from pathlib import Path

from ralfloop_agent.domains.bando_domain_builder import BandoBuildRequest, BandoDomainBuilder


def test_incomplete_identity_does_not_create_operational_complete_domain(tmp_path: Path):
    source = tmp_path / 'snippet.txt'
    source.write_text('Avviso senza ente e senza versione. Alcune spese forse ammissibili.', encoding='utf-8')

    result = BandoDomainBuilder(tmp_path / 'domains').build_draft(BandoBuildRequest(primary_document=str(source), output_root=str(tmp_path / 'domains')))

    assert result.ok is False
    assert result.status == 'identity_incomplete'
    assert result.approval_ready is False
    assert result.validation['blocking_issues']
    assert result.path is None
    assert not (tmp_path / 'domains/active').exists()
