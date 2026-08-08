from __future__ import annotations

import json
from pathlib import Path

import pytest

from ralfloop_agent.domains.storage import sha256_tree


@pytest.fixture
def approval_env(monkeypatch, tmp_path):
    monkeypatch.setenv("RALFLOOP_ENABLE_TELEGRAM_APPROVAL_GATE", "1")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_DB", str(tmp_path / "approval.sqlite"))
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_ALLOWED_USER_IDS", "111")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_ALLOWED_CHAT_IDS", "111")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_REQUIRE_PRIVATE_CHAT", "1")
    return tmp_path


def make_domain(root: Path, bando_id: str = "fondazione_unipolis_act_2026", version: str = "1.0.0") -> Path:
    path = root / "drafts" / "bandi" / bando_id / version
    (path / "rules").mkdir(parents=True, exist_ok=True)
    (path / "tests").mkdir(parents=True, exist_ok=True)
    (path / "calculations").mkdir(parents=True, exist_ok=True)
    (path / "domain.yaml").write_text(
        "\n".join(
            [
                f"bando_id: {bando_id}",
                f"domain_id: bandi/{bando_id}",
                "issuer: Fondazione Unipolis",
                "state: draft",
                "status: draft",
                "title: ACT 2026",
                f"version: {version}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (path / "rules" / "deadlines.yaml").write_text(
        "rules:\n- rule_id: application_deadline\n  field: deadline\n  value: '2026-04-09T13:00:00+02:00'\n  source_ref: regolamento\n",
        encoding="utf-8",
    )
    (path / "sources.jsonl").write_text(json.dumps({"source_id": "regolamento", "binding_level": "binding_official"}) + "\n", encoding="utf-8")
    (path / "conflicts.jsonl").write_text("", encoding="utf-8")
    (path / "tests" / "tests.jsonl").write_text(json.dumps({"test_id": "deadline", "rule_ids": ["application_deadline"]}) + "\n", encoding="utf-8")
    (path / "manifest.sha256").write_text(sha256_tree(path) + "\n", encoding="utf-8")
    (root / "registry.json").write_text(json.dumps({"domains": [{"domain_id": f"bandi/{bando_id}", "version": version, "state": "draft", "path": str(path)}]}) + "\n", encoding="utf-8")
    return path


def canary_plan() -> dict:
    return {
        "exact_input": "Qual è la scadenza per presentare candidatura al Bando ACT 2026?",
        "expected_domain": "fondazione_unipolis_act_2026",
        "expected_version": "1.0.0",
        "expected_rule_id": "application_deadline",
        "expected_deterministic_answer": "2026-04-09T13:00:00+02:00",
        "jury_expected": False,
        "recursive_mas_expected": False,
        "legacy_fallback_expected": False,
        "side_effects": False,
    }
