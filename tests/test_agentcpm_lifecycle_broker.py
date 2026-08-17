from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.ralf_agentcpm_lifecycle_broker import (
    AgentCpmController, BrokerError, DEFAULT_MODEL_PATH, MAX_REQUEST_BYTES,
    MODEL, PORT, SYSTEMCTL_ENV, UNIT, parse_request, peer_uid_allowed,
)


class HttpResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def good_http(*_args, **_kwargs):
    return HttpResponse(json.dumps({"data": [{"id": MODEL}]}).encode())


def proc_fixture(root: Path, *, uid=1001, port=PORT, alias=MODEL, model=DEFAULT_MODEL_PATH):
    process = root / "42"
    process.mkdir(parents=True)
    (process / "status").write_text(f"Name:\tllama-server\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
    argv = ["llama-server", "--port", str(port), "--alias", alias, "--model", str(model)]
    (process / "cmdline").write_bytes(b"\0".join(item.encode() for item in argv) + b"\0")


class FakeSystemctl:
    def __init__(self, active=True):
        self.active = active
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        assert command[0:2] == ["systemctl", "--user"]
        assert command[2] in {"show", "start", "stop"}
        assert command[3] == UNIT
        assert kwargs["shell"] is False
        assert kwargs["env"] == SYSTEMCTL_ENV
        if command[2] == "start":
            self.active = True
        if command[2] == "stop":
            self.active = False
        stdout = f"ActiveState={'active' if self.active else 'inactive'}\nSubState={'running' if self.active else 'dead'}\nMainPID={42 if self.active else 0}\n"
        return SimpleNamespace(stdout=stdout)


@pytest.mark.parametrize("raw,error", [
    (b"not-json", "malformed_json"),
    (b"[]", "payload_not_object"),
    (b'{"action":"status","unit":"x"}', "invalid_fields"),
    (b'{"action":"restart"}', "unsupported_action"),
    (b"x" * (MAX_REQUEST_BYTES + 1), "request_too_large"),
])
def test_request_rejections(raw, error):
    with pytest.raises(BrokerError, match=error):
        parse_request(raw)


def test_peer_uid_policy_is_exact():
    assert peer_uid_allowed(1000)
    assert peer_uid_allowed(1001)
    assert not peer_uid_allowed(2000)


def test_fixed_commands_status_and_idempotent_transitions(tmp_path):
    proc_fixture(tmp_path)
    systemctl = FakeSystemctl(active=True)
    def state_http(*args, **kwargs):
        if not systemctl.active:
            raise OSError("closed")
        return good_http(*args, **kwargs)
    controller = AgentCpmController(run=systemctl, proc_root=tmp_path, http_get=state_http,
        port_probe=lambda _port: systemctl.active, sleep=lambda _delay: None)
    assert controller.dispatch("status")["active"] is True
    assert controller.dispatch("start")["active"] is True
    assert [call[0][2] for call in systemctl.calls].count("start") == 0
    assert controller.dispatch("stop")["active"] is False
    stop_count = [call[0][2] for call in systemctl.calls].count("stop")
    assert controller.dispatch("stop")["active"] is False
    assert [call[0][2] for call in systemctl.calls].count("stop") == stop_count
    systemctl.active = False
    assert controller.dispatch("start")["active"] is True


@pytest.mark.parametrize(("uid", "port", "alias", "model", "error"), [
    (999, PORT, MODEL, DEFAULT_MODEL_PATH, "wrong_process_uid"),
    (1001, 19999, MODEL, DEFAULT_MODEL_PATH, "wrong_process_port"),
    (1001, PORT, "wrong", DEFAULT_MODEL_PATH, "wrong_process_alias"),
    (1001, PORT, MODEL, Path("/wrong.gguf"), "wrong_process_model_path"),
])
def test_active_provenance_rejections(tmp_path, uid, port, alias, model, error):
    proc_fixture(tmp_path, uid=uid, port=port, alias=alias, model=model)
    controller = AgentCpmController(run=FakeSystemctl(), proc_root=tmp_path,
        http_get=good_http, port_probe=lambda _port: True)
    state = controller.status()
    assert state["ok"] is False
    assert state["error"] == error


def test_wrong_health_model_rejected(tmp_path):
    proc_fixture(tmp_path)
    controller = AgentCpmController(run=FakeSystemctl(), proc_root=tmp_path,
        http_get=lambda *_a, **_k: HttpResponse(b'{"data":[{"id":"wrong"}]}'),
        port_probe=lambda _port: True)
    state = controller.status()
    assert state["ok"] is False
    assert state["model"] is None
