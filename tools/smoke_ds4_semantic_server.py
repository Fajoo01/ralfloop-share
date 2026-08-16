#!/usr/bin/env python3
"""Guarded real DS4-server smoke: one local request, no approval or messaging."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ralfloop_agent.providers.gpu_engine_scheduler import (  # noqa: E402
    GpuEngineTransitionError,
    TransactionalGpuScheduler,
)
from ralfloop_agent.semantic_judge import SemanticJudgeConfig  # noqa: E402
from ralfloop_agent.semantic_judge.ds4_server import Ds4ServerSession  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one scheduler-guarded DS4-server smoke request")
    parser.add_argument("--artifact-dir", type=Path)
    args = parser.parse_args()
    profile = SemanticJudgeConfig.from_env().server_profile()
    transition: dict[str, object] = {}
    payload: dict[str, object] = {
        "status": "failed",
        "command": profile.command(),
        "environment": profile.environment_overrides(),
        "request": {key: value for key, value in profile.request_payload("<prompt>", max_tokens=16).items()
                    if key != "messages"},
    }
    try:
        with TransactionalGpuScheduler().engine_session("deepseek", task_id="ds4-server-smoke") as transition:
            with Ds4ServerSession(profile, artifact_dir=args.artifact_dir) as server:
                reply = server.review_prompt("Reply with exactly OK and nothing else.", max_tokens=16)
                diagnostics = reply.diagnostics.as_dict()
                payload.update({
                    "http_status": 200,
                    "content": reply.content.strip(),
                    "server_startup_ms": server.startup_ms,
                    "request_ms": reply.request_ms,
                    "input_tokens": reply.input_tokens,
                    "output_tokens": reply.output_tokens,
                    "diagnostics": diagnostics,
                    "server_log": str(server.log_path) if server.log_path else None,
                })
        safe = (
            payload.get("content") == "OK"
            and diagnostics["early_prealloc"] >= 1
            and diagnostics["lazy_alloc"] == 0
            and diagnostics["fences"] == 43
            and diagnostics["up_expert_oom"] == 0
            and diagnostics["failed"] == 0
            and (not transition.get("external_stopped") or transition.get("external_restored"))
        )
        payload["status"] = "passed" if safe else "criteria_failed"
    except GpuEngineTransitionError as exc:
        payload.update({"status": "gpu_busy", "error": f"{type(exc).__name__}:{exc}"})
    except Exception as exc:
        payload.update({"status": "runtime_failed", "error": f"{type(exc).__name__}:{exc}"})
    payload["gpu_transition"] = transition
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if payload["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
