from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile


PRODUCTION = Path("/home/sibilla-cumana/ralfloop-production")
MODEL = Path("/home/sibilla-cumana/Dati/ralfloop-models/functiongemma-270m/functiongemma-270m-it-q8_0.gguf")


def preflight(release: Path) -> dict[str, object]:
    checks: dict[str, object] = {
        "release_under_root": release.resolve().parent == (PRODUCTION / "releases").resolve(),
        "release_metadata": (release / "RELEASE.json").is_file(),
        "manifest": verify_manifest(release),
        "functiongemma_model": MODEL.is_file(),
        "port_19104_free": port_free(19104),
        "glm_incompatible_idle": not process_match(("glm-run-machine", "colibri_glm")),
    }
    memory = meminfo()
    checks["ram_available_mb"] = memory["MemAvailable"]
    checks["swap_free_mb"] = memory["SwapFree"]
    checks["enough_ram"] = memory["MemAvailable"] >= 4096
    checks["enough_swap"] = memory["SwapFree"] >= 512
    checks["gate_marker"] = gate_marker(release)
    checks["allowed"] = all(value is True for key, value in checks.items() if key not in {"ram_available_mb", "swap_free_mb", "allowed"})
    return checks


def verify_manifest(release: Path) -> bool:
    manifest = release / "MANIFEST.sha256"
    if not manifest.is_file():
        return False
    for line in manifest.read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        target = release / relative
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            return False
    return True


def gate_marker(release: Path) -> bool:
    marker = release / ".ralf_run/local_arch_v1/gates.json"
    if not marker.is_file():
        return False
    data = json.loads(marker.read_text(encoding="utf-8"))
    return (
        data.get("scoped_tests") is True
        and data.get("no_regression") is True
        and data.get("sandbox") is True
        and data.get("no_host_candidate_execution") is True
        and data.get("benchmark_minimum") is True
        and data.get("full_suite_clean") is True
    )


def port_free(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def process_match(needles: tuple[str, ...]) -> bool:
    output = subprocess.run(["ps", "-eo", "args="], check=True, text=True, capture_output=True).stdout.casefold()
    for line in output.splitlines():
        if "deploy_local_arch_release" in line:
            continue
        if any(needle.casefold() in line for needle in needles):
            return True
    return False


def meminfo() -> dict[str, int]:
    values = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        values[key] = int(value.strip().split()[0]) // 1024
    return values


def atomic_link(link: Path, target: Path) -> None:
    temporary = link.with_name(f".{link.name}.tmp-{os.getpid()}")
    temporary.symlink_to(target.resolve())
    os.replace(temporary, link)


def publish(release: Path) -> dict[str, object]:
    checks = preflight(release)
    if not checks["allowed"]:
        return {"published": False, "checks": checks}
    old = (PRODUCTION / "current").resolve()
    atomic_link(PRODUCTION / "previous", old)
    atomic_link(PRODUCTION / "current", release)
    return {"published": True, "previous": str(old), "current": str(release.resolve()), "checks": checks}


def rollback_drill(release: Path) -> dict[str, object]:
    old = (PRODUCTION / "current").resolve()
    with tempfile.TemporaryDirectory(prefix="ralf-rollback-drill-") as raw:
        root = Path(raw)
        current = root / "current"
        previous = root / "previous"
        atomic_link(current, old)
        atomic_link(previous, old)
        atomic_link(current, release)
        atomic_link(current, previous.resolve())
        return {"ok": current.resolve() == old, "restored": str(current.resolve()), "target": str(old)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("release", type=Path)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--rollback-drill", action="store_true")
    args = parser.parse_args()
    result: dict[str, object] = {"preflight": preflight(args.release)}
    if args.rollback_drill:
        result["rollback_drill"] = rollback_drill(args.release)
    if args.publish:
        result["deploy"] = publish(args.release)
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["preflight"]["allowed"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
