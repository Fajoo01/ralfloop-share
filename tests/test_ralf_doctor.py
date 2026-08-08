from __future__ import annotations

import io
import json
from pathlib import Path

from ralfloop_agent.cli import terminal_chat
from ralfloop_agent.doctor import DoctorReport, render_doctor, run_doctor
from ralfloop_agent.model_tools import ModelToolRegistry


class Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self.payload

    def close(self):
        pass


class Session:
    def get(self, url, timeout):
        if url.endswith("/health"):
            return Response({"status": "ok"})
        if url.endswith("/props"):
            return Response({"model_alias": "qwen2.5:7b", "chat_template": "template"})
        if url.endswith("/v1/models"):
            return Response({"data": [{"id": "qwen2.5:7b"}]})
        if url.endswith("/openapi.json"):
            return Response({"paths": {"/chat": {}, "/tasks/run": {}}})
        return Response({}, 404)


def test_doctor_is_read_only_and_reports_all_core_checks(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    audit.write_text("", encoding="utf-8")
    registry = ModelToolRegistry.load(cache_root=tmp_path / "cache")
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    report = run_doctor(
        cwd=tmp_path,
        session=Session(),
        model_registry=registry,
        backend_url="http://backend",
        audit_log=audit,
    )

    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    assert report.ok is True
    assert before == after
    assert report.checks["llama_cpp"]["provider"] == "llama_cpp"
    assert report.checks["llama_cpp"]["fallback"] == "none"
    assert report.checks["routing"]["ok"] is True
    assert report.checks["encoding"]["utf8_ok"] is True
    assert len(report.checks["model_tools"]["tools"]) == 10
    assert "provider=llama_cpp" in render_doctor(report)


def test_doctor_cli_supports_json(monkeypatch) -> None:
    report = DoctorReport(ok=True, checks={"llama_cpp": {}, "model_tools": {"tools": []}}, warnings=[])
    monkeypatch.setattr("ralfloop_agent.doctor.run_doctor", lambda **kwargs: report)
    args = terminal_chat.build_parser().parse_args(["doctor", "--json"])
    out = io.StringIO()
    assert terminal_chat.run_doctor_command(args, out=out, err=io.StringIO()) == 0
    assert json.loads(out.getvalue())["ok"] is True
