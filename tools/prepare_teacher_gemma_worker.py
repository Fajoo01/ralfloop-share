#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

MODEL = "gemma3:4b"
LABEL = "ralf-teacher-gemma=ready"
KUBECONFIG = "/etc/rancher/k3s/k3s.yaml"


def run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, text=True, capture_output=True, check=check)


def remote(vpn_name: str, *argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["vpnpc", "run", vpn_name, "--", *argv], check=check)


def remote_probe(vpn_name: str) -> dict[str, object]:
    script = r'''set -eu
printf 'cpu=%s\n' "$(nproc)"
awk '/MemTotal:/ {print "mem_kib=" $2}' /proc/meminfo
avail=$(df -Pk /usr/share/ollama 2>/dev/null | awk 'NR==2 {print $4}')
printf 'disk_kib=%s\n' "${avail:-0}"
command -v ollama >/dev/null && echo ollama=1 || echo ollama=0
test -x /usr/local/bin/ollama && echo ollama_bin=1 || echo ollama_bin=0
test -d /usr/local/lib/ollama && echo ollama_lib=1 || echo ollama_lib=0
id -u ollama 2>/dev/null | awk '{print "ollama_uid=" $1}'
id -g ollama 2>/dev/null | awk '{print "ollama_gid=" $1}'
if sudo -n -u ollama test -d /usr/share/ollama/.ollama/models 2>/dev/null; then echo model_store=1; else echo model_store=0; fi
ollama show gemma3:4b >/dev/null 2>&1 && echo model=1 || echo model=0
'''
    result = remote(vpn_name, "sh", "-lc", script)
    values: dict[str, object] = {}
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if not key or key.startswith("="):
            continue
        values[key] = int(value) if value.isdigit() else value
    return values


def qualifies(values: dict[str, object]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if int(values.get("cpu", 0)) < 4:
        reasons.append("cpu_lt_4")
    if int(values.get("mem_kib", 0)) < 7_000_000:
        reasons.append("ram_lt_7GB")
    if int(values.get("disk_kib", 0)) < 8_000_000:
        reasons.append("disk_free_lt_8GB")
    for key in ("ollama", "ollama_bin", "ollama_lib", "model_store"):
        if int(values.get(key, 0)) != 1:
            reasons.append(f"missing_{key}")
    return not reasons, reasons


def ensure_model(vpn_name: str) -> None:
    remote(vpn_name, "ollama", "pull", MODEL)


def kubectl(*args: str) -> subprocess.CompletedProcess[str]:
    sudo = shutil.which("sudo") or "sudo"
    return run([
        sudo, "-n", "/usr/local/bin/k3s", "kubectl",
        "--kubeconfig", KUBECONFIG, *args,
    ])


def label_node(node: str) -> None:
    kubectl("label", "node", node, LABEL, "--overwrite")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("vpn_name")
    parser.add_argument("k8s_node")
    parser.add_argument("--pull", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    values = remote_probe(args.vpn_name)
    if int(values.get("model", 0)) != 1 and args.pull:
        ensure_model(args.vpn_name)
        values = remote_probe(args.vpn_name)

    ok, reasons = qualifies(values)
    if int(values.get("model", 0)) != 1:
        ok = False
        reasons.append("missing_gemma3_4b")

    report = {
        "vpn_name": args.vpn_name,
        "k8s_node": args.k8s_node,
        "model": MODEL,
        "ready": ok,
        "reasons": sorted(set(reasons)),
        "probe": values,
        "labeled": False,
    }
    if ok and not args.dry_run:
        label_node(args.k8s_node)
        report["labeled"] = True

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
