from pathlib import Path

from ralfloop_agent.domains.builder import DomainBuilder
from ralfloop_agent.domains.promotion import DomainPromotionService
from ralfloop_agent.domains.registry import DomainRegistry


def test_human_approval_required(tmp_path):
    reg = DomainRegistry(tmp_path / "domains")
    result = DomainPromotionService(reg).promote("x", "1", approved_by="me", approval_token=None)
    assert result.ok is False
    assert result.approval_required is True


def test_jury_review_not_enough_for_promotion(tmp_path):
    reg = DomainRegistry(tmp_path / "domains")
    result = DomainPromotionService(reg).promote("x", "1", approved_by="jury", approval_token="wrong")
    assert result.status == "approval_required"


def test_promotion_with_token_and_valid_draft(tmp_path, monkeypatch):
    source = tmp_path / "manual.md"; source.write_text("policy", encoding="utf-8")
    reg = DomainRegistry(tmp_path / "domains")
    draft = DomainBuilder(reg).create_draft("Dominio valido", [str(source)], {})
    monkeypatch.setenv("DOMAIN_APPROVAL_TOKEN", "ok")
    result = DomainPromotionService(reg).promote(draft.domain_id, draft.version, approved_by="human", approval_token="ok", approval_reason="lab")
    assert result.ok is True
    assert result.status == "active"
    assert reg.find_active(draft.domain_id)["manifest"]["state"] == "active"
    assert reg.get_domain(draft.domain_id, draft.version)["manifest"]["state"] == "draft"


def test_active_update_requires_new_draft(tmp_path, monkeypatch):
    source = tmp_path / "manual.md"; source.write_text("policy", encoding="utf-8")
    reg = DomainRegistry(tmp_path / "domains")
    first = DomainBuilder(reg).create_draft("Dominio versionato", [str(source)], {"domain_id": "versioned", "version": "1.0.0"})
    second = DomainBuilder(reg).create_draft("Dominio versionato update", [str(source)], {"domain_id": "versioned", "version": "1.1.0"})
    assert first.version == "1.0.0"
    assert second.version == "1.1.0"


def test_active_version_is_immutable(tmp_path, monkeypatch):
    source = tmp_path / "manual.md"; source.write_text("policy", encoding="utf-8")
    reg = DomainRegistry(tmp_path / "domains")
    draft = DomainBuilder(reg).create_draft("Dominio immutable", [str(source)], {"domain_id": "immutable", "version": "1.0.0"})
    monkeypatch.setenv("DOMAIN_APPROVAL_TOKEN", "ok")
    first = DomainPromotionService(reg).promote(draft.domain_id, draft.version, approved_by="human", approval_token="ok")
    second = DomainPromotionService(reg).promote(draft.domain_id, draft.version, approved_by="human", approval_token="ok")
    assert first.ok is True
    assert second.ok is False
    assert second.status == "active_version_exists"
