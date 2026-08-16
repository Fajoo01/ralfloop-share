#!/usr/bin/env python3
"""Host-only transactional Qwen smoke test followed by Magnolia draft delivery.

This runner never calls a Gmail mutation. It must be run outside a device-isolated
Codex sandbox so that /proc and nvidia-smi describe the host.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ralfloop_agent.providers.chat import FallbackChatProvider, build_chat_provider
from ralfloop_agent.providers.gpu_engine_scheduler import ExternalEngineLifecycle, TransactionalGpuScheduler
from tools.run_magnolia_workflow import main as magnolia_main


def load_env(path: Path) -> None:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def main() -> int:
    load_env(Path("/etc/ralfloop/fast-chat.env"))
    # This is the existing authoritative approval configuration used by the
    # backend and Telegram delivery services. Magnolia must inherit it too.
    load_env(Path("/etc/ralfloop/telegram-approval.env"))
    external = ExternalEngineLifecycle()
    provenance = external.discover()
    initial_status = external.client.status()
    print(f"AGENTCPM_INITIAL_STATUS={'active' if initial_status['active'] else 'inactive'}", flush=True)
    print(f"AGENTCPM_MAIN_PID={initial_status['main_pid']}", flush=True)
    print(f"AGENTCPM_OWNER_UID={initial_status.get('owner_uid')}", flush=True)
    report = {
        "email_sent": False,
        "agentcpm_initial_state": "active" if provenance else "inactive",
        "agentcpm_provenance": (
            {
                "manager": provenance.manager,
                "unit": provenance.unit,
                "owner_uid": provenance.owner_uid,
                "pid": provenance.pid,
                "parent_pid": provenance.parent_pid,
                "argv": list(provenance.argv),
                "model_path": str(provenance.model_path),
                "port": provenance.port,
            }
            if provenance else None
        ),
    }
    try:
        with TransactionalGpuScheduler(external=external).engine_session("qwen_chat", task_id="magnolia-real") as transition:
            provider = build_chat_provider()
            primary = provider.primary if isinstance(provider, FallbackChatProvider) else provider
            report.update(transition)
            report.update({"provider_effective": getattr(primary, "name", ""), "model_effective": getattr(primary, "default_model", ""), "endpoint": getattr(primary, "base_url", "")})
            print(f"VRAM_REAL_AFTER_STOP={transition.get('vram_after_release_mib')}", flush=True)
            print(f"QWEN_REQUIRED={getattr(external, 'required_gpu_memory_mib', transition.get('required_gpu_memory_mib'))}", flush=True)
            print(f"QWEN_PROVIDER_EFFECTIVE={report['provider_effective']}", flush=True)
            print(f"QWEN_MODEL_EFFECTIVE={report['model_effective']}", flush=True)
            print(f"QWEN_ENDPOINT={report['endpoint']}", flush=True)
            for label in ("first_call_seconds", "second_call_seconds"):
                started = time.monotonic()
                result = provider.chat([{"role": "user", "content": "Rispondi esclusivamente con OK"}])
                report[label] = round(time.monotonic() - started, 3)
                report[label.replace("seconds", "result")] = {"text": result.text, "provider": result.provider, "model": result.model, "metadata": result.metadata}
                print(f"{label.upper()}={report[label]}", flush=True)
                if result.provider != "llama_cpp" or result.model != "qwen3.5:9b" or result.metadata.get("fallback_used") is True:
                    raise RuntimeError("qwen_real_inference_fell_back")
            print(json.dumps({"qwen": report}, ensure_ascii=False, indent=2), flush=True)
            status = magnolia_main(host_diagnostics=True)
            if status:
                raise RuntimeError(f"magnolia_workflow_failed:{status}")
    finally:
        report["agentcpm_healthy_after"] = external.healthy()
        report["email_sent"] = False
        print(json.dumps({"final": report}, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
