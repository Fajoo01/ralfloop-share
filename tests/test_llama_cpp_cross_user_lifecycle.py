from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import signal

import pytest

from ralfloop_agent.providers.llama_cpp_server import (
    LlamaCppServerConfig,
    LlamaCppServerManager,
    LlamaCppServerOwnershipError,
    ProcessIdentity,
)
from scripts.ralf_llama_cpp_lifecycle_broker import (
    BrokerError,
    LlamaCppController,
    build_controller,
    parse_request,
    peer_uid_allowed,
)


def config(tmp_path: Path, **overrides) -> LlamaCppServerConfig:
    values = {
        "model_path": tmp_path / "model.gguf",
        "model_hash": "a" * 64,
        "server_bin": tmp_path / "llama-server",
        "state_dir": tmp_path / "state",
    }
    values.update(overrides)
    return LlamaCppServerConfig(**values)


def record(cfg: LlamaCppServerConfig, *, pid: int = 321, uid: int = 1001, ticks: int = 77) -> dict:
    return {
        "pid": pid,
        "owner_pid": None,
        "owner_uid": uid,
        "start_ticks": ticks,
        "process_start_ticks": ticks,
        "provider": "llama_cpp",
        "mode": "chat",
        "server_bin": str(cfg.server_bin.resolve()),
        "model_path": str(cfg.model_path),
        "model_hash": cfg.model_hash,
        "port": cfg.port,
    }


def identity(cfg: LlamaCppServerConfig, *, pid: int = 321, uid: int = 1001, ticks: int = 77) -> ProcessIdentity:
    return ProcessIdentity(
        pid=pid,
        uid=uid,
        start_ticks=ticks,
        executable=str(cfg.server_bin.resolve()),
        argv=(
            str(cfg.server_bin),
            "--model", str(cfg.model_path),
            "--alias", cfg.model,
            "--host", "127.0.0.1",
            "--port", str(cfg.port),
        ),
    )


def test_expected_launcher_uid_allows_exact_cross_user_provenance(tmp_path: Path) -> None:
    cfg = config(tmp_path, expected_launcher_uid=1001)
    expected = identity(cfg)
    manager = LlamaCppServerManager(cfg, identity_reader=lambda pid: expected)

    assert manager._validated_identity(record(cfg)).pid == 321
    assert os.getuid() != 1001 or expected.uid == os.getuid()


def test_backend_uses_only_broker_attested_cross_user_executable(tmp_path: Path) -> None:
    cfg = config(tmp_path, expected_launcher_uid=1001)
    expected = identity(cfg)

    class Client:
        def status(self):
            return {
                "ok": True,
                "action": "status",
                "active": True,
                "managed": True,
                "healthy": True,
                "pid": expected.pid,
                "provenance": {
                    "pid": expected.pid,
                    "uid": expected.uid,
                    "start_ticks": expected.start_ticks,
                    "executable": expected.executable,
                    "argv": list(expected.argv),
                },
            }

    manager = LlamaCppServerManager(
        cfg,
        identity_reader=lambda pid: None,
        lifecycle_client=Client(),
    )
    assert manager._validated_identity(record(cfg)).pid == expected.pid


def test_backend_rejects_broker_attestation_with_wrong_executable(tmp_path: Path) -> None:
    cfg = config(tmp_path, expected_launcher_uid=1001)
    expected = identity(cfg)

    class Client:
        def status(self):
            return {
                "ok": True,
                "action": "status",
                "active": True,
                "managed": True,
                "healthy": True,
                "pid": expected.pid,
                "provenance": {
                    "pid": expected.pid,
                    "uid": expected.uid,
                    "start_ticks": expected.start_ticks,
                    "executable": "/tmp/fake-server",
                    "argv": list(expected.argv),
                },
            }

    manager = LlamaCppServerManager(
        cfg,
        identity_reader=lambda pid: None,
        lifecycle_client=Client(),
    )
    with pytest.raises(LlamaCppServerOwnershipError):
        manager._validated_identity(record(cfg))


@pytest.mark.parametrize(
    "mutation",
    (
        "record_uid",
        "process_uid",
        "start_ticks",
        "process_start_ticks",
        "executable",
        "argv_model",
        "argv_alias",
        "argv_port",
        "argv_host",
        "duplicate_port",
        "record_model",
        "record_server",
        "record_hash",
        "record_port",
        "record_provider",
        "record_mode",
    ),
)
def test_cross_user_provenance_rejects_adversarial_mismatch(tmp_path: Path, mutation: str) -> None:
    cfg = config(tmp_path, expected_launcher_uid=1001)
    rec = record(cfg)
    proc = identity(cfg)
    if mutation == "record_uid":
        rec["owner_uid"] = 1000
    elif mutation == "process_uid":
        proc = replace(proc, uid=1000)
    elif mutation == "start_ticks":
        rec["start_ticks"] = 78
    elif mutation == "process_start_ticks":
        rec["process_start_ticks"] = 78
    elif mutation == "executable":
        proc = replace(proc, executable="/tmp/llama-server")
    elif mutation.startswith("argv_") or mutation == "duplicate_port":
        argv = list(proc.argv)
        flag = {
            "argv_model": "--model",
            "argv_alias": "--alias",
            "argv_port": "--port",
            "argv_host": "--host",
        }.get(mutation)
        if mutation == "duplicate_port":
            argv.extend(("--port", str(cfg.port)))
        else:
            assert flag is not None
            argv[argv.index(flag) + 1] = "wrong"
        proc = replace(proc, argv=tuple(argv))
    elif mutation == "record_model":
        rec["model_path"] = "/tmp/wrong.gguf"
    elif mutation == "record_server":
        rec["server_bin"] = "/tmp/wrong-server"
    elif mutation == "record_hash":
        rec["model_hash"] = "b" * 64
    elif mutation == "record_port":
        rec["port"] = 19093
    elif mutation == "record_provider":
        rec["provider"] = "other"
    elif mutation == "record_mode":
        rec["mode"] = "agent"
    manager = LlamaCppServerManager(cfg, identity_reader=lambda pid: proc)

    with pytest.raises(LlamaCppServerOwnershipError):
        manager._validated_identity(rec)


def test_agentcpm_19093_never_matches_chat_engine(tmp_path: Path) -> None:
    cfg = config(tmp_path, expected_launcher_uid=1001)
    argv = list(identity(cfg).argv)
    argv[argv.index("--port") + 1] = "19093"
    proc = replace(identity(cfg), argv=tuple(argv))
    assert LlamaCppServerManager(cfg)._identity_matches_target(proc) is False


def test_pid_record_must_be_owner_controlled_regular_file(tmp_path: Path) -> None:
    cfg = config(tmp_path, expected_launcher_uid=os.getuid())
    cfg.state_dir.mkdir()
    cfg.pid_path.write_text(json.dumps(record(cfg, uid=os.getuid())), encoding="utf-8")
    cfg.pid_path.chmod(0o660)
    manager = LlamaCppServerManager(cfg)
    with pytest.raises(LlamaCppServerOwnershipError, match="untrusted"):
        manager._read_pid_record()

    cfg.pid_path.chmod(0o640)
    target = tmp_path / "record-target"
    cfg.pid_path.replace(target)
    cfg.pid_path.symlink_to(target)
    with pytest.raises(LlamaCppServerOwnershipError, match="untrusted"):
        manager._read_pid_record()


def test_healthy_process_without_trusted_record_remains_unmanaged(tmp_path: Path) -> None:
    manager = LlamaCppServerManager(config(tmp_path, allow_healthy_reuse=False))
    manager.health = lambda: True
    with pytest.raises(LlamaCppServerOwnershipError, match="unmanaged_process_on_port"):
        manager.ensure_available()


def test_cross_user_stop_is_brokered_after_local_provenance_validation(tmp_path: Path) -> None:
    cfg = config(tmp_path, expected_launcher_uid=os.getuid())
    cfg.state_dir.mkdir()
    rec = record(cfg, uid=os.getuid())
    cfg.pid_path.write_text(json.dumps(rec), encoding="utf-8")
    cfg.pid_path.chmod(0o640)
    alive = {"value": True}
    proc = identity(cfg, uid=os.getuid())

    class Client:
        calls = 0

        def stop(self):
            self.calls += 1
            alive["value"] = False
            return {
                "ok": True, "action": "stop", "active": False,
                "managed": False, "healthy": False, "pid": None, "changed": True,
            }

    client = Client()
    killed = []
    manager = LlamaCppServerManager(
        cfg,
        lifecycle_client=client,
        identity_reader=lambda pid: proc if alive["value"] else None,
        kill_fn=lambda pid, sig: killed.append((pid, sig)),
    )
    manager.health = lambda: alive["value"]
    manager._port_in_use = lambda: alive["value"]

    assert manager.stop()["brokered"] is True
    assert client.calls == 1
    assert killed == []


def test_lifecycle_broker_protocol_is_fixed_and_peer_bounded() -> None:
    assert parse_request(b'{"action":"status"}') == "status"
    for raw in (
        b'{"action":"delete"}',
        b'{"action":"stop","pid":1}',
        b'{"selector":"button"}',
    ):
        with pytest.raises(BrokerError):
            parse_request(raw)
    assert peer_uid_allowed(1000) is True
    assert peer_uid_allowed(1001) is True
    assert peer_uid_allowed(0) is False
    assert peer_uid_allowed(1002) is False


def test_broker_never_stops_unmanaged_healthy_port() -> None:
    class Manager:
        class Config:
            startup_timeout_sec = 1.0

        config = Config()
        stop_calls = 0

        def status(self):
            return {"managed": False, "healthy": True, "pid": None}

        def _port_in_use(self):
            return True

        def stop(self):
            self.stop_calls += 1

    manager = Manager()
    controller = LlamaCppController(manager)
    with pytest.raises(BrokerError, match="unmanaged"):
        controller.dispatch("stop")
    assert manager.stop_calls == 0


def test_broker_keeps_launcher_primary_gid_and_shared_socket_group() -> None:
    root = Path(__file__).resolve().parents[1]
    unit = (root / "deploy/systemd/ralf-llama-cpp-lifecycle-broker.service").read_text()
    tmpfiles = (root / "deploy/tmpfiles.d/ralf-llama-cpp-lifecycle.conf").read_text()
    assert "User=bandi" in unit
    assert "Group=bandi" in unit
    assert "SupplementaryGroups=ralf-gpu" in unit
    assert "Environment=PYTHONPATH=/home/sibilla-cumana/ralfloop-production/current" in unit
    assert "ReadWritePaths=/home/bandi/.local/state/ralf /var/lib/ralf-llama-cpp" in unit
    assert "RuntimeDirectory=" not in unit
    assert "2750 bandi ralf-gpu" in tmpfiles


def test_broker_never_recurses_into_its_own_lifecycle_socket(monkeypatch) -> None:
    monkeypatch.setenv("RALF_LLAMA_CPP_LIFECYCLE_SOCKET", "/run/recursive.sock")
    assert build_controller().manager.config.lifecycle_socket is None
