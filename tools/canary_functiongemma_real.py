#!/usr/bin/env python3
"""Real, CPU-only FunctionGemma routing canary. It never executes a routed tool."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
from urllib.request import urlopen

from ralfloop_agent.local_arch.policy import RalfPolicy
from ralfloop_agent.local_arch.router import FunctionGemmaClient, LocalRouter, ToolRegistry


ROOT = Path(__file__).resolve().parents[1]
MODEL = Path("/home/sibilla-cumana/Dati/ralfloop-models/functiongemma-270m/functiongemma-270m-it-q8_0.gguf")
SERVER = Path("/home/sibilla-cumana/src/llama.cpp/build/bin/llama-server")
SHA256 = "83940d4dd9676710856f43523bed096164a595a96f6b34771610a03937de5270"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if hashlib.sha256(MODEL.read_bytes()).hexdigest() != SHA256:
        raise SystemExit("model_sha256_mismatch")
    command = [str(SERVER), "--model", str(MODEL), "--alias", "functiongemma-router",
               "--host", "127.0.0.1", "--port", "19104", "--ctx-size", "1536",
               "--threads", "4", "--threads-batch", "4", "--parallel", "1",
               "--n-gpu-layers", "0", "--jinja", "--offline", "--no-webui"]
    started = time.perf_counter()
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        while True:
            if process.poll() is not None:
                raise SystemExit("llama_server_start_failed")
            try:
                with urlopen("http://127.0.0.1:19104/health", timeout=0.2) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            if time.perf_counter() - started > 15:
                raise SystemExit("llama_server_start_timeout")
            time.sleep(0.05)
        startup_ms = round((time.perf_counter() - started) * 1000, 3)
        registry = ToolRegistry.load(ROOT / "config/local_arch_tools_v1.json")
        client = FunctionGemmaClient(timeout=20)
        router = LocalRouter(registry, client)
        cases = [("A", "trova il percorso minimo", "AF", "shortest_path"),
                 ("B", "richiesta su PDF scannerizzato con una tabella", "VR", "visual_rag"),
                 ("C", "pubblica il post", "AP", "external_action"),
                 ("D", "definisci una strategia ambigua per un problema nuovo", "LM", "colibri_director")]
        results = []
        for label, text, action, target in cases:
            decision = router.classify(text)
            passed = decision.route.a == action and decision.route.t == target
            row = {"case": label, "request": text, "expected": {"a": action, "t": target},
                   "route": decision.route.as_dict(), "source": decision.source,
                   "native_output": client.last_native_output, "metrics": dict(decision.metrics or {}),
                   "pass": passed}
            if label == "C":
                policy = RalfPolicy().evaluate(decision.route, operation="social_publish")
                row["policy"] = {"allowed": policy.allowed, "reason": policy.reason}
                row["pass"] = passed and not policy.allowed
            results.append(row)
        report = {"v": 1, "cpu_only": True, "side_effects": False, "startup_ms": startup_ms,
                  "model_sha256": SHA256, "pass": all(row["pass"] for row in results), "cases": results}
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        return 0 if report["pass"] else 1
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
