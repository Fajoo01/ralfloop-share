from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import sys
from typing import Any

from pydantic import BaseModel, Field
import requests

from ralfloop_agent.integration.interaction_router import classify_interaction
from ralfloop_agent.model_tools import ModelToolRegistry
from ralfloop_agent.providers.llama_cpp_server import LlamaCppServerConfig


class DoctorReport(BaseModel):
    ok: bool
    checks: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


def _get_json(session: requests.Session, url: str, timeout: tuple[float, float]) -> tuple[dict[str, Any] | None, str | None]:
    response = None
    try:
        response = session.get(url, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        return (payload, None) if isinstance(payload, dict) else (None, "response_not_object")
    except (requests.RequestException, ValueError, TypeError) as exc:
        return None, f"{type(exc).__name__}:{exc}"
    finally:
        if response is not None:
            response.close()


def _llama_processes() -> list[dict[str, Any]]:
    rows = []
    proc = Path("/proc")
    for entry in proc.iterdir() if proc.is_dir() else ():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
            argv = [item.decode("utf-8", errors="replace") for item in raw.split(b"\0") if item]
        except OSError:
            continue
        if any("llama-server" in item for item in argv):
            rows.append({"pid": int(entry.name), "argv": argv})
    return sorted(rows, key=lambda row: row["pid"])


def run_doctor(
    *,
    cwd: str | Path | None = None,
    session: requests.Session | None = None,
    model_registry: ModelToolRegistry | None = None,
    backend_url: str = "http://127.0.0.1:19090",
    audit_log: str | Path = "/home/sibilla-cumana/ralfloop_agent_scaffold/.openshell_backend/audit.jsonl",
) -> DoctorReport:
    selected_session = session or requests.Session()
    config = LlamaCppServerConfig.from_env()
    timeout = (2.0, min(5.0, config.idle_timeout_sec))
    health, health_error = _get_json(selected_session, f"{config.base_url}/health", timeout)
    props, props_error = _get_json(selected_session, f"{config.base_url}/props", timeout)
    models, models_error = _get_json(selected_session, f"{config.base_url}/v1/models", timeout)
    backend, backend_error = _get_json(selected_session, f"{backend_url}/openapi.json", timeout)
    template = str((props or {}).get("chat_template") or "")
    routes = {
        "read_only": classify_interaction("Controlla cosa occupa spazio. Non cancellare nulla.").__dict__,
        "mixed": classify_interaction("Non cancellare i modelli, ma elimina i temporanei.").__dict__,
        "chat": classify_interaction("Ciao Ralf").__dict__,
    }
    routing_ok = (
        routes["read_only"]["capability"] == "read_only_system_inspection"
        and routes["read_only"]["approval_required"] is False
        and routes["mixed"]["capability"] == "protected_external_action"
        and routes["mixed"]["approval_required"] is True
    )
    registry = model_registry or ModelToolRegistry.load()
    model_health = registry.health()
    dependencies = {
        name: importlib.util.find_spec(name) is not None
        for name in ("requests", "pydantic", "torch", "transformers", "safetensors", "gliner")
    }
    selected_cwd = Path(cwd or os.getcwd()).resolve()
    audit_path = Path(audit_log)
    checks = {
        "llama_cpp": {
            "ok": health == {"status": "ok"},
            "provider": "llama_cpp",
            "endpoint": config.base_url,
            "model_id": config.model,
            "model_path": str(config.model_path),
            "context_size": config.context,
            "gpu_layers": config.gpu_layers,
            "request_timeout_sec": config.request_timeout_sec,
            "fallback": config.fallback,
            "health_error": health_error,
            "models_error": models_error,
            "props_error": props_error,
            "models": models,
            "model_alias": (props or {}).get("model_alias"),
            "quantization": "encoded_in_gguf_not_reported_by_props",
            "chat_template_sha256": hashlib.sha256(template.encode("utf-8")).hexdigest() if template else None,
            "chat_template_present": bool(template),
            "processes": _llama_processes(),
        },
        "backend": {
            "ok": backend is not None,
            "endpoint": backend_url,
            "error": backend_error,
            "routes": sorted((backend or {}).get("paths", {})),
        },
        "routing": {"ok": routing_ok, "canaries": routes},
        "model_tools": model_health,
        "dependencies": dependencies,
        "permissions": {
            "cwd": str(selected_cwd),
            "readable": os.access(selected_cwd, os.R_OK),
            "writable": os.access(selected_cwd, os.W_OK),
        },
        "encoding": {
            "filesystem": sys.getfilesystemencoding(),
            "stdout": getattr(sys.stdout, "encoding", None),
            "utf8_ok": sys.getfilesystemencoding().casefold() == "utf-8",
        },
        "audit": {
            "path": str(audit_path),
            "exists": audit_path.exists(),
            "readable": audit_path.is_file() and os.access(audit_path, os.R_OK),
        },
        "quick_tests": {"routing_canary": routing_ok, "read_only_only": True},
    }
    warnings = [
        f"{row['tool_id']}:{row['status']}"
        for row in model_health["tools"]
        if row["status"] != "ready"
    ]
    if config.fallback != "none":
        warnings.append("llama_cpp_fallback_enabled")
    ok = bool(checks["llama_cpp"]["ok"] and checks["backend"]["ok"] and routing_ok)
    return DoctorReport(ok=ok, checks=checks, warnings=warnings)


def render_doctor(report: DoctorReport) -> str:
    llama = report.checks["llama_cpp"]
    model_tools = report.checks["model_tools"]["tools"]
    rows = [
        f"status={'ok' if report.ok else 'degraded'}",
        f"provider={llama['provider']}",
        f"endpoint={llama['endpoint']}",
        f"model={llama['model_id']}",
        f"fallback={llama['fallback']}",
        f"routing={'ok' if report.checks['routing']['ok'] else 'failed'}",
        f"model_tools_ready={sum(row['status'] == 'ready' for row in model_tools)}/{len(model_tools)}",
    ]
    rows.extend(f"warning={warning}" for warning in report.warnings)
    return "\n".join(rows)


__all__ = ["DoctorReport", "render_doctor", "run_doctor"]
