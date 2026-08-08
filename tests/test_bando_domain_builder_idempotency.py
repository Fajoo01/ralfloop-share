from __future__ import annotations

from pathlib import Path
import json

from ralfloop_agent.domains.bando_domain_builder import BandoBuildRequest, BandoDomainBuilder

PONTI_DOC = Path('/home/bandi/Scaricati/Bando-Nuovi-ponti-culturali-2.pdf')


def test_same_documents_are_idempotent(tmp_path: Path):
    root = tmp_path / 'domains'
    builder = BandoDomainBuilder(root)
    req = BandoBuildRequest(primary_document=str(PONTI_DOC), output_root=str(root))

    first = builder.build_draft(req)
    second = builder.build_draft(req)
    registry = json.loads((root / 'registry.json').read_text(encoding='utf-8'))
    registry_rows = [
        row for row in registry["domains"]
        if row["domain_id"] == f"bandi/{first.bando_id}" and row["version"] == first.version
    ]

    assert first.bando_id == second.bando_id
    assert first.version == second.version
    assert [r['rule_id'] for r in first.rules] == [r['rule_id'] for r in second.rules]
    assert first.content_hash == second.content_hash
    assert len(registry_rows) == 1
