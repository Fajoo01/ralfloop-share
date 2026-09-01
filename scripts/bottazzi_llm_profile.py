#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

from ralfloop_agent.providers.llm_profiles import LlmProfileError, load_llm_profile


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate or run an opt-in Bottazzi LLM profile")
    parser.add_argument("--server", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19196)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "::1"}:
        raise LlmProfileError("experimental_server_must_be_loopback")
    if not 1024 <= args.port <= 65535:
        raise LlmProfileError("invalid_experimental_server_port")
    profile = load_llm_profile()
    if profile.name == "legacy":
        raise LlmProfileError("legacy_profile_is_managed_by_existing_runtime")
    if not args.server.is_file():
        raise LlmProfileError(f"server_not_found:{args.server}")
    if profile.model_path is None or not profile.model_path.is_file():
        raise LlmProfileError(f"model_not_found:{profile.model_path}")
    expected_hash = os.getenv("BOTTAZZI_QWEN35_MODEL_SHA256", "").strip().lower()
    if args.execute:
        if len(expected_hash) != 64 or any(ch not in "0123456789abcdef" for ch in expected_hash):
            raise LlmProfileError("qwen35_model_sha256_required")
        digest = hashlib.sha256()
        with profile.model_path.open("rb") as source:
            for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != expected_hash:
            raise LlmProfileError("qwen35_model_sha256_mismatch")
    command = [
        str(args.server), "--model", str(profile.model_path),
        "--host", args.host, "--port", str(args.port), *profile.server_args(),
    ]
    print(json.dumps({
        "profile": profile.name, "execute": args.execute, "command": command,
    }, sort_keys=True), flush=True)
    if not args.execute:
        return 0
    return subprocess.run(command, env=os.environ.copy(), check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
