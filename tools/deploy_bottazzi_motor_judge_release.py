from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any
from urllib.request import Request, urlopen

RELEASE_ROOT = Path("/home/sibilla-cumana/ralfloop-production/releases")
PRODUCTION_CURRENT = Path("/home/sibilla-cumana/ralfloop-production/current")
JUDGE_ROOT = Path("/home/sibilla-cumana/ralfloop-motor-judge")
JUDGE_CURRENT = JUDGE_ROOT / "current"
JUDGE_PREVIOUS = JUDGE_ROOT / "previous"
ENV_FILE = Path("/etc/ralfloop/bottazzi-motor-judge.env")
UNIT_FILE = Path("/etc/systemd/system/bottazzi-motor-judge.service")
SERVICE = "bottazzi-motor-judge.service"
PYTHON = Path("/home/sibilla-cumana/ralfloop_agent_scaffold/.venv/bin/python")
PRODUCTION_DS4_PORT = 19194
JUDGE_PORT = 19196


def _manifest_ok(release: Path) -> bool:
    manifest = release / "MANIFEST.sha256"
    if not manifest.is_file():
        return False
    try:
        for line in manifest.read_text(encoding="utf-8").splitlines():
            digest, relative = line.split("  ", 1)
            target = release / relative
            if not target.is_file():
                return False
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                return False
    except (OSError, ValueError):
        return False
    return True


def _release_identity_ok(release: Path) -> bool:
    meta = release / "RELEASE.json"
    if not meta.is_file():
        return False
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return data.get("commit") == release.resolve().name


def validate_release(release: Path) -> dict[str, bool]:
    resolved = release.resolve()
    unit = resolved / "deploy/systemd/bottazzi-motor-judge.service"
    unit_text = unit.read_text(encoding="utf-8") if unit.is_file() else ""
    checks = {
        "under_release_root": resolved.parent == RELEASE_ROOT.resolve(),
        "release_identity": _release_identity_ok(resolved),
        "manifest": _manifest_ok(resolved),
        "judge_module": (resolved / "ralfloop_agent/integration/bottazzi_motor_judge_service.py").is_file(),
        "unit_asset": unit.is_file(),
        "dedicated_code_root": (
            "/home/sibilla-cumana/ralfloop-motor-judge/current" in unit_text
            and "/home/sibilla-cumana/ralfloop-production/current" not in unit_text
        ),
    }
    checks["allowed"] = all(checks.values())
    return checks


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _force_env_value(text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"(?m)^{re.escape(key)}=.*$")
    row = f"{key}={value}"
    if pattern.search(text):
        return pattern.sub(row, text, count=1)
    return text.rstrip() + "\n" + row + "\n"


def _atomic_link(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.with_name(f".{link.name}.tmp-{os.getpid()}")
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(target.resolve())
    os.replace(temporary, link)


def _listener_pid(port: int) -> int | None:
    output = subprocess.run(
        ["ss", "-ltnp"], check=True, text=True, capture_output=True,
    ).stdout
    for line in output.splitlines():
        if f":{port} " not in line:
            continue
        match = re.search(r"pid=(\d+)", line)
        if match:
            return int(match.group(1))
    return None


def _candidate_env(release: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(_read_env(ENV_FILE))
    env["PYTHONPATH"] = str(release.resolve())
    env["BOTTAZZI_MOTOR_JUDGE_DENSE_READAHEAD"] = "0"
    return env


def candidate_preflight(release: Path) -> bool:
    completed = subprocess.run(
        [
            str(PYTHON), "-m",
            "ralfloop_agent.integration.bottazzi_motor_judge_service",
            "preflight",
        ],
        cwd=release,
        env=_candidate_env(release),
        text=True,
        capture_output=True,
        timeout=30,
    )
    return completed.returncode == 0


def rollout_plan(release: Path) -> dict[str, Any]:
    resolved = release.resolve()
    current_target = (
        JUDGE_CURRENT.resolve()
        if JUDGE_CURRENT.exists() or JUDGE_CURRENT.is_symlink()
        else PRODUCTION_CURRENT.resolve()
    )
    return {
        "candidate": str(resolved),
        "release_checks": validate_release(resolved),
        "candidate_preflight": candidate_preflight(resolved),
        "judge_current_before": str(current_target),
        "production_19194_pid": _listener_pid(PRODUCTION_DS4_PORT),
        "judge_19196_pid": _listener_pid(JUDGE_PORT),
        "dense_readahead_target": False,
        "mutates_production": False,
    }


def _backup_state(old_target: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    root = JUDGE_ROOT / "rollbacks" / stamp
    root.mkdir(parents=True, exist_ok=False)
    (root / "state.json").write_text(
        json.dumps({"old_target": str(old_target.resolve())}, indent=2) + "\n",
        encoding="utf-8",
    )
    if ENV_FILE.is_file():
        shutil.copy2(ENV_FILE, root / "bottazzi-motor-judge.env")
    if UNIT_FILE.is_file():
        shutil.copy2(UNIT_FILE, root / "bottazzi-motor-judge.service")
    return root


def _install_candidate_config(release: Path) -> None:
    original = ENV_FILE.read_text(encoding="utf-8")
    updated = _force_env_value(
        original, "BOTTAZZI_MOTOR_JUDGE_DENSE_READAHEAD", "0"
    )
    temporary = ENV_FILE.with_name(f".{ENV_FILE.name}.tmp-{os.getpid()}")
    temporary.write_text(updated, encoding="utf-8")
    os.chmod(temporary, ENV_FILE.stat().st_mode & 0o777)
    os.replace(temporary, ENV_FILE)
    shutil.copy2(
        release / "deploy/systemd/bottazzi-motor-judge.service",
        UNIT_FILE,
    )


def _systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", *args], check=check, text=True,
        capture_output=True, timeout=240,
    )


def _wait_service_active(timeout_sec: float = 210.0) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        state = _systemctl("is-active", SERVICE, check=False).stdout.strip()
        if state == "active" and _listener_pid(JUDGE_PORT):
            return True
        time.sleep(1.0)
    return False


def _render_user_prompt(text: str) -> str:
    return (
        "<｜begin▁of▁sentence｜><｜User｜>" + text
        + "<｜Assistant｜></think>"
    )


def _count_tokens(text: str, env: dict[str, str]) -> int:
    server = Path(env["BOTTAZZI_MOTOR_JUDGE_BIN"])
    cli = server.with_name("ds4")
    model = Path(env["BOTTAZZI_MOTOR_JUDGE_MODEL"])
    completed = subprocess.run(
        [str(cli), "--dump-tokens", "-m", str(model), "-p", _render_user_prompt(text)],
        check=True, text=True, capture_output=True, timeout=20,
    )
    values = json.loads(completed.stdout.splitlines()[0])
    if not isinstance(values, list):
        raise RuntimeError("canary_token_dump_invalid")
    return len(values)


def _calibrate_canary(target_tokens: int, env: dict[str, str]) -> tuple[str, int]:
    rows: list[str] = []
    tokens = 0
    for index in range(512):
        rows.append(f"f{index}=verified; ")
        if index % 4 != 3:
            continue
        text = "".join(rows)
        tokens = _count_tokens(text, env)
        if tokens >= target_tokens:
            return text, tokens
    raise RuntimeError("canary_target_unreachable")


def _post_canary(text: str, timeout_sec: float = 180.0) -> bool:
    body = json.dumps({
        "model": "deepseek-v4.1-flash",
        "messages": [{"role": "user", "content": text}],
        "temperature": 0,
        "max_tokens": 4,
        "think": False,
        "stream": False,
    }).encode("utf-8")
    request = Request(
        f"http://127.0.0.1:{JUDGE_PORT}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_sec) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return False
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return False
    return isinstance(content, str)


def run_canaries(env: dict[str, str]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for label, target in (("small", 180), ("normal", 240), ("upper", 300), ("small_after", 180)):
        text, tokens = _calibrate_canary(target, env)
        ok = _post_canary(text)
        results.append({"label": label, "tokens": tokens, "ok": ok})
        if not ok:
            break
    return results


def _rollback(backup: Path, old_target: Path) -> None:
    _atomic_link(JUDGE_CURRENT, old_target)
    env_backup = backup / "bottazzi-motor-judge.env"
    unit_backup = backup / "bottazzi-motor-judge.service"
    if env_backup.is_file():
        shutil.copy2(env_backup, ENV_FILE)
    if unit_backup.is_file():
        shutil.copy2(unit_backup, UNIT_FILE)
    _systemctl("daemon-reload")
    _systemctl("restart", SERVICE)
    if not _wait_service_active():
        raise RuntimeError("rollback_judge_not_active")


def apply_release(release: Path) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise RuntimeError("rollout_requires_root")
    resolved = release.resolve()
    checks = validate_release(resolved)
    if not checks["allowed"] or not candidate_preflight(resolved):
        return {"applied": False, "reason": "candidate_preflight_failed", "checks": checks}
    production_pid = _listener_pid(PRODUCTION_DS4_PORT)
    if not production_pid:
        return {"applied": False, "reason": "production_19194_listener_missing"}

    old_target = (
        JUDGE_CURRENT.resolve()
        if JUDGE_CURRENT.exists() or JUDGE_CURRENT.is_symlink()
        else PRODUCTION_CURRENT.resolve()
    )
    JUDGE_ROOT.mkdir(parents=True, exist_ok=True)
    lock_path = JUDGE_ROOT / "rollout.lock"
    with lock_path.open("w", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        backup = _backup_state(old_target)
        try:
            _install_candidate_config(resolved)
            _atomic_link(JUDGE_PREVIOUS, old_target)
            _atomic_link(JUDGE_CURRENT, resolved)
            _systemctl("daemon-reload")
            _systemctl("restart", SERVICE)
            if not _wait_service_active():
                raise RuntimeError("candidate_judge_not_active")
            if _listener_pid(PRODUCTION_DS4_PORT) != production_pid:
                raise RuntimeError("production_19194_pid_changed")
            canaries = run_canaries(_candidate_env(resolved))
            if len(canaries) != 4 or not all(row["ok"] for row in canaries):
                raise RuntimeError("judge_canary_failed")
            if _listener_pid(PRODUCTION_DS4_PORT) != production_pid:
                raise RuntimeError("production_19194_pid_changed_after_canary")
            return {
                "applied": True,
                "candidate": str(resolved),
                "previous": str(old_target),
                "backup": str(backup),
                "production_19194_pid": production_pid,
                "judge_19196_pid": _listener_pid(JUDGE_PORT),
                "canaries": canaries,
            }
        except Exception as exc:
            _rollback(backup, old_target)
            return {
                "applied": False,
                "rolled_back": True,
                "reason": f"{type(exc).__name__}:{exc}",
                "backup": str(backup),
                "production_19194_unchanged": _listener_pid(PRODUCTION_DS4_PORT) == production_pid,
            }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Isolated Bot-tazzi Motor Judge release rollout"
    )
    parser.add_argument("release", type=Path)
    parser.add_argument(
        "--apply", action="store_true",
        help="switch and restart only bottazzi-motor-judge.service",
    )
    args = parser.parse_args(argv)
    try:
        if args.apply:
            result = apply_release(args.release)
            print(json.dumps(result, sort_keys=True, indent=2))
            return 0 if result.get("applied") else 3
        result = rollout_plan(args.release)
        print(json.dumps(result, sort_keys=True, indent=2))
        allowed = (
            result["release_checks"]["allowed"]
            and result["candidate_preflight"]
            and result["production_19194_pid"] is not None
        )
        return 0 if allowed else 3
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}:{exc}"}))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
