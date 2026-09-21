#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

CURRENT = Path("/home/sibilla-cumana/ralfloop-production/current")
DATA_DIR = Path("/home/sibilla-cumana/ralfloop_data/atm_telegram")
GRAPH = DATA_DIR / "atm-router-current.bin"
TOPOLOGY = DATA_DIR / "atm-direct-topology.json"
GTFS = DATA_DIR / "gtfs.zip"
PYTHON = Path("/home/sibilla-cumana/ralfloop_agent_scaffold/.venv/bin/python")
MODEL_URL = os.getenv("RALF_ATM_HEAL_MODEL_URL", "http://127.0.0.1:19110/v1/chat/completions")
MODEL_NAME = os.getenv("RALF_ATM_HEAL_MODEL", "qwen2.5-3b")
STATE_DIR = Path(os.getenv("RALF_ATM_HEAL_STATE_DIR", "/run/ralf-atm-self-heal"))
RECOVERY_ROOT = Path(os.getenv("RALF_ATM_HEAL_RECOVERY_ROOT", "/var/lib/ralf-atm-self-heal"))
ATM_PROBE = "https://giromilano.atm.it/proxy.tpportal/api/tpPortal/tpl/stops/13487/linesummary"

ALLOWED_ACTIONS = {
    "retry_refresh",
    "refresh_cached",
    "compile_router_and_refresh",
    "restart_atm_broker",
    "restart_backend",
    "stop_and_alert",
}


def run(cmd: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def http_json(url: str, *, timeout: float = 3.0, headers: dict[str, str] | None = None) -> Any:
    req = urllib.request.Request(url, headers=headers or {"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _surface_end(topology: dict[str, Any]) -> str:
    return str(topology.get("surface_end_date") or topology.get("feed_end_date") or "")

def _date_valid(raw: str) -> bool:
    if not raw:
        return False
    try:
        end = datetime.strptime(raw, "%Y%m%d").date()
    except ValueError:
        return False
    return end >= date.today()


def service_active(name: str) -> bool:
    result = run(["systemctl", "is-active", "--quiet", name], timeout=5)
    return result.returncode == 0


def backend_ok() -> bool:
    try:
        payload = http_json("http://127.0.0.1:19090/health", timeout=2.0)
    except Exception:
        return False
    return isinstance(payload, dict) and bool(payload.get("ok", True))


def upstream_ok() -> bool:
    try:
        payload = http_json(
            ATM_PROBE,
            timeout=4.0,
            headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0 ralfloop-atm-telegram/0.1"},
        )
    except Exception:
        return False
    return isinstance(payload, (dict, list))

def recovery_router(release: Path) -> Path:
    return RECOVERY_ROOT / f"atm-router-{release.name}"


def health_snapshot() -> dict[str, Any]:
    release = CURRENT.resolve()
    router = release / "tools/atm_router/atm-router"
    recovery = recovery_router(release)
    source = release / "tools/atm_router/atm_router.c"
    topology: dict[str, Any] = {}
    try:
        topology = json.loads(TOPOLOGY.read_text(encoding="utf-8"))
    except Exception:
        pass
    end = _surface_end(topology)
    snapshot = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "release": str(release),
        "router_primary_ok": router.is_file() and os.access(router, os.X_OK),
        "router_recovery_ok": recovery.is_file() and os.access(recovery, os.X_OK),
        "router_source_ok": source.is_file(),
        "graph_ok": GRAPH.is_file() and GRAPH.stat().st_size > 1_000_000,
        "topology_ok": TOPOLOGY.is_file() and bool(topology.get("patterns")) and _date_valid(end),
        "feed_version": topology.get("feed_version"),
        "surface_end_date": end,
        "backend_ok": backend_ok(),
        "broker_ok": service_active("ralf-atm-mcp-broker.service"),
        "upstream_ok": upstream_ok(),
    }
    snapshot["router_ok"] = bool(snapshot["router_primary_ok"] or snapshot["router_recovery_ok"])
    snapshot["healthy"] = all(
        snapshot[key]
        for key in ("router_ok", "graph_ok", "topology_ok", "backend_ok", "broker_ok", "upstream_ok")
    )
    return snapshot

def model_choice(snapshot: dict[str, Any], reason: str) -> tuple[str | None, str]:
    prompt = {
        "role": "ATM self-healing controller",
        "reason": reason,
        "diagnostics": snapshot,
        "allowed_actions": sorted(ALLOWED_ACTIONS),
        "rules": [
            "Return JSON only with keys action and reason.",
            "Never invent commands or paths.",
            "Use compile_router_and_refresh only when router_primary_ok is false and router_source_ok is true.",
            "Use restart_backend only when backend_ok is false.",
            "Use restart_atm_broker only when broker_ok is false.",
            "If only upstream_ok is false, choose stop_and_alert because remote ATM cannot be repaired locally.",
            "Prefer retry_refresh for a graph refresh failure when local prerequisites exist.",
        ],
    }
    body = json.dumps(
        {
            "model": MODEL_NAME,
            "messages": [
                {"role": "system", "content": "Choose one safe repair action. Output JSON only."},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            "temperature": 0,
            "max_tokens": 100,
        }
    ).encode("utf-8")
    req = urllib.request.Request(MODEL_URL, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
        content = str(payload["choices"][0]["message"]["content"]).strip()
        decision = json.loads(content)
        action = str(decision.get("action") or "")
        reason_text = str(decision.get("reason") or "")
        if action in ALLOWED_ACTIONS:
            return action, reason_text
        return None, f"invalid_action:{action}"
    except Exception as exc:
        return None, f"{type(exc).__name__}:{exc}"


def fallback_action(snapshot: dict[str, Any], reason: str) -> str:
    if not snapshot.get("backend_ok"):
        return "restart_backend"
    if not snapshot.get("broker_ok"):
        return "restart_atm_broker"
    if not snapshot.get("router_primary_ok") and snapshot.get("router_source_ok"):
        return "compile_router_and_refresh"
    if not snapshot.get("graph_ok") or not snapshot.get("topology_ok"):
        return "retry_refresh"
    if reason.startswith("ralf-atm-graph-refresh"):
        return "retry_refresh"
    return "stop_and_alert"

def guarded_action(snapshot: dict[str, Any], proposed: str | None, reason: str) -> str:
    if not snapshot.get("backend_ok"):
        return "restart_backend"
    if not snapshot.get("broker_ok"):
        return "restart_atm_broker"
    if not snapshot.get("router_primary_ok") and snapshot.get("router_source_ok"):
        return "compile_router_and_refresh"
    if not snapshot.get("graph_ok") or not snapshot.get("topology_ok"):
        return "retry_refresh"
    if not snapshot.get("upstream_ok"):
        return "stop_and_alert"
    if snapshot.get("healthy") and not reason.startswith("ralf-atm-graph-refresh"):
        return "stop_and_alert"
    action = proposed if proposed in {"retry_refresh", "refresh_cached", "stop_and_alert"} else fallback_action(snapshot, reason)
    if action == "refresh_cached" and not GTFS.is_file():
        return "retry_refresh"
    return action


def _refresh(extra: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    release = CURRENT.resolve()
    cmd = ["runuser", "-u", "sibilla-cumana", "--", str(PYTHON), str(release / "scripts/refresh_atm_router_graph.py")]
    cmd.extend(extra or [])
    return run(cmd, timeout=420)


def execute(action: str) -> subprocess.CompletedProcess[str] | None:
    if action == "retry_refresh":
        return _refresh()
    if action == "refresh_cached":
        return _refresh(["--no-download"])
    if action == "restart_backend":
        result = run(["systemctl", "restart", "ralfloop-backend.service"], timeout=30)
        time.sleep(2)
        return result
    if action == "restart_atm_broker":
        result = run(["systemctl", "restart", "ralf-atm-mcp-broker.service"], timeout=30)
        time.sleep(1)
        return result
    if action == "compile_router_and_refresh":
        release = CURRENT.resolve()
        source = release / "tools/atm_router/atm_router.c"
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        recovery = recovery_router(release)
        recovery.parent.mkdir(parents=True, exist_ok=True)
        compiled = run([
            "cc", "-O3", "-std=c11", "-Wall", "-Wextra", "-Werror", "-pedantic",
            "-o", str(recovery), str(source), "-lm",
        ], timeout=60)
        if compiled.returncode != 0:
            return compiled
        recovery.chmod(0o755)
        return _refresh(["--router", str(recovery)])
    return None


def _write_event(payload: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / "events.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

def _recent_repair_count(window_seconds: int = 1800) -> int:
    path = STATE_DIR / "events.jsonl"
    if not path.is_file():
        return 0
    cutoff = time.time() - window_seconds
    count = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[-100:]:
            row = json.loads(line)
            if row.get("kind") == "repair" and float(row.get("epoch") or 0) >= cutoff:
                count += 1
    except Exception:
        return 0
    return count


def repair(reason: str) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_DIR / "repair.lock"
    with lock_path.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("ATM_SELF_HEAL_SKIPPED already_running")
            return 0
        before = health_snapshot()
        proposed, model_reason = model_choice(before, reason)
        action = guarded_action(before, proposed, reason)
        destructive = action != "stop_and_alert"
        if destructive and _recent_repair_count() >= 3:
            action = "stop_and_alert"
            model_reason = "rate_limit:3_repairs_in_30m"
        event: dict[str, Any] = {
            "kind": "repair" if action != "stop_and_alert" else "escalation",
            "epoch": time.time(),
            "reason": reason,
            "model_action": proposed,
            "model_reason": model_reason,
            "action": action,
            "before": before,
        }
        if action == "stop_and_alert":
            event["after"] = before
            _write_event(event)
            print("ATM_SELF_HEAL_ESCALATE " + json.dumps(event, ensure_ascii=False, sort_keys=True))
            return 0
        result = execute(action)
        event["returncode"] = None if result is None else result.returncode
        event["stdout_tail"] = "" if result is None else result.stdout[-2000:]
        event["stderr_tail"] = "" if result is None else result.stderr[-2000:]
        after = health_snapshot()
        event["after"] = after
        _write_event(event)
        if result is not None and result.returncode == 0 and after.get("healthy"):
            print("ATM_SELF_HEAL_OK " + json.dumps(event, ensure_ascii=False, sort_keys=True))
            return 0
        print("ATM_SELF_HEAL_INCOMPLETE " + json.dumps(event, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--reason", default="manual")
    args = parser.parse_args()
    if args.check:
        snapshot = health_snapshot()
        print("ATM_HEALTH " + json.dumps(snapshot, ensure_ascii=False, sort_keys=True))
        return 0 if snapshot.get("healthy") else 1
    if args.repair:
        return repair(args.reason)
    parser.error("scegli --check oppure --repair")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
