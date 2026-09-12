"""Persist AgentCPM q8_0 KV cache through a user-systemd drop-in.

This is an operator deployment tool. It is not part of the Teacher capability
surface and never exposes lifecycle or infrastructure actions to students.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip()
    return values


def main() -> int:
    home = Path.home()
    env_path = home / ".config/ralfloop/agentcpm.env"
    if not env_path.is_file():
        raise SystemExit("agentcpm_env_missing")

    values = _read_env(env_path)
    source_value = values.get("RALF_DEEP_RESEARCH_CONFIG", "")
    if not source_value:
        raise SystemExit("agentcpm_config_path_missing")
    source = Path(os.path.expandvars(source_value)).expanduser().resolve()
    if not source.is_file():
        raise SystemExit("agentcpm_config_missing")

    config_dir = home / ".config/ralfloop"
    config_dir.mkdir(parents=True, exist_ok=True)
    target = config_dir / "deep_research-agentcpm-q8.json"
    data = json.loads(source.read_text(encoding="utf-8"))
    server = data.get("agentcpm_server")
    if not isinstance(server, dict):
        raise SystemExit("agentcpm_server_config_missing")
    server["cache_type_k"] = "q8_0"
    server["cache_type_v"] = "q8_0"
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    target.chmod(0o600)

    drop_dir = home / ".config/systemd/user/ralfloop-agentcpm.service.d"
    drop_dir.mkdir(parents=True, exist_ok=True)
    drop = drop_dir / "60-kvq8.conf"
    previous_drop = drop.read_bytes() if drop.exists() else None
    drop.write_text(
        "[Service]\n"
        f"Environment=RALF_DEEP_RESEARCH_CONFIG={target}\n",
        encoding="utf-8",
    )
    drop.chmod(0o600)

    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "restart", "ralfloop-agentcpm.service"], check=True)
        for attempt in range(60):
            try:
                with urllib.request.urlopen("http://127.0.0.1:19093/health", timeout=3) as response:
                    payload = json.load(response)
                if payload == {"status": "ok"}:
                    break
            except Exception:
                if attempt == 59:
                    raise
                time.sleep(1)
    except Exception:
        if previous_drop is None:
            drop.unlink(missing_ok=True)
        else:
            drop.write_bytes(previous_drop)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "--user", "restart", "ralfloop-agentcpm.service"], check=False)
        raise

    pid = subprocess.check_output(
        ["systemctl", "--user", "show", "ralfloop-agentcpm.service", "-p", "MainPID", "--value"],
        text=True,
    ).strip()
    cmdline = ""
    proc = Path("/proc") / pid / "cmdline"
    if pid and pid != "0" and proc.is_file():
        cmdline = proc.read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    if "--cache-type-k q8_0" not in cmdline or "--cache-type-v q8_0" not in cmdline:
        raise SystemExit("agentcpm_q8_not_effective")

    print(json.dumps({
        "status": "healthy",
        "service": "ralfloop-agentcpm.service",
        "cache_type_k": "q8_0",
        "cache_type_v": "q8_0",
        "config": str(target),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
