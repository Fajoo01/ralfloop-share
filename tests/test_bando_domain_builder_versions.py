from __future__ import annotations

from pathlib import Path

from ralfloop_agent.domains.bando_domain_builder import BandoBuildRequest, BandoDomainBuilder


def test_amendment_creates_patch_version_and_compare(tmp_path: Path):
    call = tmp_path / 'call.md'
    amendment = tmp_path / 'rettifica.md'
    call.write_text('Bando Cultura 2026\nFondazione Demo indice il bando. Richiesta contributo compresa fra 10.000 e 50.000 euro.', encoding='utf-8')
    amendment.write_text('Rettifica ufficiale Bando Cultura 2026 Fondazione Demo. Richiesta contributo compresa fra 10.000 e 60.000 euro.', encoding='utf-8')
    root = tmp_path / 'domains'
    builder = BandoDomainBuilder(root)

    first = builder.build_draft(BandoBuildRequest(primary_document=str(call), output_root=str(root)))
    second = builder.build_draft(BandoBuildRequest(primary_document=str(call), amendments=[str(amendment)], output_root=str(root)))

    assert first.version == '1.0.0'
    assert second.version == '1.0.1'
    assert builder.compare_versions(first.bando_id, '1.0.0', '1.0.1', root)['update_type'] == 'new_version'


def test_new_edition_creates_distinct_bando_id(tmp_path: Path):
    a = tmp_path / 'call2026.md'
    b = tmp_path / 'call2027.md'
    a.write_text('Bando Cultura 2026\nFondazione Demo indice il bando. Richiesta contributo compresa fra 10.000 e 50.000 euro.', encoding='utf-8')
    b.write_text('Bando Cultura 2027\nFondazione Demo indice il bando. Richiesta contributo compresa fra 10.000 e 50.000 euro.', encoding='utf-8')
    root = tmp_path / 'domains'
    builder = BandoDomainBuilder(root)

    first = builder.build_draft(BandoBuildRequest(primary_document=str(a), output_root=str(root)))
    second = builder.build_draft(BandoBuildRequest(primary_document=str(b), output_root=str(root)))

    assert first.bando_id != second.bando_id
    assert first.identity['edition'] == '2026'
    assert second.identity['edition'] == '2027'
