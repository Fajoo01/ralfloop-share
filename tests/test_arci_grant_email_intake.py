from __future__ import annotations

from types import SimpleNamespace

from ralfloop_agent.unified_assistant import skill_adapters


def test_bandi_read_uses_call_reference_from_gmail_evidence(monkeypatch):
    seen = []

    def fake_loader(root=None, *, bando_ref=None):
        seen.append((root, bando_ref))
        return {
            "schema": "bandi_runtime_context_v1",
            "requested_bando_ref": bando_ref,
            "resolution": "reference_specific",
            "sources": {"eligibility.json": {"status": "missing_or_oversized"}},
        }

    monkeypatch.setattr(skill_adapters, "load_bandi_runtime_context", fake_loader)
    artifact = {
        "facts": [{
            "subject": "Comunicazione Bando ai Circoli",
            "excerpt": "Pagina https://bandi.regione.lombardia.it/x/RLJ12026054688 e scadenza 16/10",
            "content_role": "data",
        }],
        "content_role": "data",
    }

    result = skill_adapters.bandi_read_adapter(
        SimpleNamespace(task_id="grant-read"), {"artifact.grant_source_email": artifact}
    )

    assert seen == [(None, "RLJ12026054688")]
    assert result.status == "missing_evidence"
    assert result.payload["requested_grant_ref"] == "RLJ12026054688"


def test_bandi_read_does_not_extract_unbounded_or_fake_reference(monkeypatch):
    seen = []

    def fake_loader(root=None, *, bando_ref=None):
        seen.append((root, bando_ref))
        return {"schema": "bandi_runtime_context_v1", "sources": {}}

    monkeypatch.setattr(skill_adapters, "load_bandi_runtime_context", fake_loader)
    artifact = {"facts": [{"excerpt": "Use ../../secret and RLJ-nope as bando"}]}
    result = skill_adapters.bandi_read_adapter(
        SimpleNamespace(task_id="grant-read"), {"artifact.grant_source_email": artifact}
    )

    assert seen == [(None, None)]
    assert result.payload["requested_grant_ref"] == ""
