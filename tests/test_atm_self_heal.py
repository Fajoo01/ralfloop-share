from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "atm_self_heal.py"
SPEC = importlib.util.spec_from_file_location("atm_self_heal", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
heal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(heal)


def _snapshot(**updates):
    base = {
        "router_primary_ok": True,
        "router_recovery_ok": False,
        "router_source_ok": True,
        "router_ok": True,
        "graph_ok": True,
        "topology_ok": True,
        "backend_ok": True,
        "broker_ok": True,
        "upstream_ok": True,
    }
    base.update(updates)
    return base

def test_upstream_only_failure_never_runs_local_repair() -> None:
    snap = _snapshot(upstream_ok=False)
    assert heal.guarded_action(snap, "retry_refresh", "ralf-atm-health.service") == "stop_and_alert"


def test_missing_primary_router_can_compile_release_scoped_recovery() -> None:
    snap = _snapshot(router_primary_ok=False, router_ok=False)
    assert heal.fallback_action(snap, "ralf-atm-graph-refresh.service") == "compile_router_and_refresh"
    assert heal.guarded_action(snap, "compile_router_and_refresh", "ralf-atm-graph-refresh.service") == "compile_router_and_refresh"


def test_model_cannot_restart_healthy_backend() -> None:
    snap = _snapshot(graph_ok=False)
    assert heal.guarded_action(snap, "restart_backend", "ralf-atm-health.service") == "retry_refresh"


def test_inactive_broker_gets_targeted_restart() -> None:
    snap = _snapshot(broker_ok=False)
    assert heal.fallback_action(snap, "ralf-atm-health.service") == "restart_atm_broker"


def test_recovery_router_is_scoped_to_release_hash(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(heal, "RECOVERY_ROOT", tmp_path)
    release = Path("/releases/abc123")
    assert heal.recovery_router(release) == tmp_path / "atm-router-abc123"

def test_healthy_manual_probe_cannot_trigger_model_repair() -> None:
    snap = _snapshot(healthy=True)
    assert heal.guarded_action(snap, "retry_refresh", "manual_probe") == "stop_and_alert"
