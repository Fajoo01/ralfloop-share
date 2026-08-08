from __future__ import annotations

import json
from pathlib import Path

from ralfloop_agent.model_tools.log_reader import (
    discover_logs,
    open_log,
    search_logs,
)
from ralfloop_agent.model_tools.web_research import run_deep_web_research


def test_log_reader_allowlists_roots_excludes_symlinks_and_redacts(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    secret = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcd"
    (root / "backend.log").write_text(
        f"normal line\nAuthorization: Bearer abcdefghijklmnopqrstuvwxyz\nbot={secret}\nneedle failure\n",
        encoding="utf-8",
    )
    outside = tmp_path / "outside.log"
    outside.write_text("needle outside", encoding="utf-8")
    (root / "linked.log").symlink_to(outside)
    monkeypatch.setenv("RALF_MODEL_TOOL_LOG_ROOTS", str(root))

    files = discover_logs()
    assert [item.alias for item in files.values()] == ["root1/backend.log"]
    searched = search_logs("needle", files=files)
    assert len(searched["matches"]) == 1
    opened = open_log("L1", files=files, max_lines=20)
    assert "Bearer abcdefghijklmnopqrstuvwxyz" not in opened["text"]
    assert secret not in opened["text"]
    assert "[REDACTED]" in opened["text"]
    assert "[REDACTED_TELEGRAM_TOKEN]" in opened["text"]
    assert str(root) not in json.dumps(searched)


def test_log_open_is_bounded(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    (root / "backend.log").write_text(
        "\n".join(f"line {index}" for index in range(300)),
        encoding="utf-8",
    )
    monkeypatch.setenv("RALF_MODEL_TOOL_LOG_ROOTS", str(root))
    files = discover_logs()
    opened = open_log("L1", files=files, max_lines=9999)
    assert len(str(opened["text"]).splitlines()) == 120
    assert opened["start_line"] == 181
    assert opened["end_line"] == 300


def test_agentcpm_can_search_open_and_cite_redacted_local_log(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    (root / "backend.log").write_text(
        "service started\nERROR database timeout password=supersecret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RALF_MODEL_TOOL_LOG_ROOTS", str(root))
    actions = iter(
        (
            '{"tool":"log_search","arguments":{"query":"database timeout"}}',
            '{"tool":"log_open","arguments":{"source_id":"S1","max_lines":20}}',
            '{"tool":"web_finish","arguments":{"answer":"Il log segnala un timeout del database.","claims":[{"text":"Il log segnala un timeout del database.","citation_ids":["S1"]}]}}',
        )
    )
    observed = []

    def action(messages):
        observed.append(messages)
        return next(actions)

    result = run_deep_web_research(
        tmp_path,
        {"query": "Controlla i log per timeout", "max_steps": 3},
        action_provider=action,
        state_dir=tmp_path / "runs",
    )

    assert result["partial"] is False
    assert result["answer"] == "Il log segnala un timeout del database."
    assert result["citations"][0]["url"].startswith("log://")
    assert result["citations"][0]["opened"] is True
    opened_payload = json.loads(observed[2][-1]["content"])
    assert "supersecret" not in opened_payload["text"]
    assert "[REDACTED]" in opened_payload["text"]
    assert str(root) not in json.dumps(result)


def test_log_search_ranks_partial_terms_for_context_overflow(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    (root / "llama-server.log").write_text(
        "load_model AgentCPM-Explore.Q4_K_M.gguf\n"
        "send_error: request (8360 tokens) exceeds the available context size (8192 tokens)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RALF_MODEL_TOOL_LOG_ROOTS", str(root))
    files = discover_logs()

    searched = search_logs(
        "superamento context size AgentCPM errori",
        files=files,
        limit=5,
        max_age_hours=8760,
    )

    assert searched["matches"][0]["excerpt"].startswith("send_error:")
    assert searched["matches"][0]["matched_terms"] == ["context", "size"]
    assert searched["matches"][0]["match_score"] == 2


def test_agentcpm_rejects_unsupported_negative_local_log_claim(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    (root / "llama-server.log").write_text(
        "send_error: request (8298 tokens) exceeds the available context size (8192 tokens)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RALF_MODEL_TOOL_LOG_ROOTS", str(root))
    actions = iter(
        (
            '{"tool":"log_search","arguments":{"query":"context size error"}}',
            '{"tool":"log_open","arguments":{"source_id":"S1","max_lines":10}}',
            '{"tool":"web_finish","arguments":{"answer":"No errors were found.","claims":[{"text":"No errors were found in the logs.","citation_ids":["S1"]}]}}',
        )
    )
    result = run_deep_web_research(
        tmp_path,
        {"query": "Controlla i log recenti per errori di context size", "max_steps": 3},
        action_provider=lambda messages: next(actions),
        state_dir=tmp_path / "runs",
    )
    assert result["partial"] is True
    assert "web_finish_local_log_absence_unsupported" in result["errors"]


def test_agentcpm_local_log_answer_is_rendered_from_validated_claims(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    (root / "backend.log").write_text(
        "ERROR database timeout password=supersecret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RALF_MODEL_TOOL_LOG_ROOTS", str(root))
    actions = iter(
        (
            '{"tool":"log_search","arguments":{"query":"database timeout"}}',
            '{"tool":"log_open","arguments":{"source_id":"S1","max_lines":10}}',
            '{"tool":"web_finish","arguments":{"answer":"Il log segnala un timeout del database. Modello inventato 99B.","claims":[{"text":"Il log segnala un timeout del database.","citation_ids":["S1"]}]}}',
        )
    )
    result = run_deep_web_research(
        tmp_path,
        {"query": "Controlla i log recenti per timeout", "max_steps": 3},
        action_provider=lambda messages: next(actions),
        state_dir=tmp_path / "runs",
    )
    assert result["partial"] is False
    assert result["answer"] == "Il log segnala un timeout del database."
    assert "99B" not in result["answer"]
