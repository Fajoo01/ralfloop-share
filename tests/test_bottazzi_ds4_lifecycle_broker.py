from __future__ import annotations

import io
import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts.ralf_bottazzi_ds4_lifecycle_broker import (
    BottazziDs4Controller,
    BrokerError,
    MAX_REQUEST_BYTES,
    MODEL,
    RIZZO_SHADOW_UNIT,
    UNIT,
    parse_request,
    peer_uid_allowed,
)


class HttpResponse(io.BytesIO):
    def close(self):
        super().close()


def good_http(*_args, **_kwargs):
    return HttpResponse(json.dumps({"data": [{"id": MODEL}]}).encode())


class FakeSystemctl:
    def __init__(self, active: bool = False, shadow_active: bool = False, shadow_missing: bool = False):
        self.active = active
        self.shadow_active = shadow_active
        self.shadow_missing = shadow_missing
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        assert command[0] == "systemctl"
        unit = command[2]
        assert unit in {UNIT, RIZZO_SHADOW_UNIT}
        if unit == RIZZO_SHADOW_UNIT and self.shadow_missing:
            raise subprocess.CalledProcessError(1, command)
        attr = "active" if unit == UNIT else "shadow_active"
        if command[1] == "start":
            setattr(self, attr, True)
        elif command[1] == "stop":
            setattr(self, attr, False)
        active = bool(getattr(self, attr))
        stdout = (
            f"ActiveState={'active' if active else 'inactive'}\n"
            f"SubState={'running' if active else 'dead'}\n"
            f"MainPID={(1439 if unit == UNIT else 2440) if active else 0}\n"
        )
        return SimpleNamespace(stdout=stdout)


def state_http(systemctl):
    def get(*args, **kwargs):
        if not systemctl.active:
            raise OSError("closed")
        return good_http(*args, **kwargs)
    return get


def test_fixed_unit_start_stop_are_idempotent():
    systemctl = FakeSystemctl(active=False)
    controller = BottazziDs4Controller(
        run=systemctl,
        http_get=state_http(systemctl),
        sleep=lambda _delay: None,
    )
    assert controller.dispatch("start")["active"] is True
    start_count = [call[0][1] for call in systemctl.calls].count("start")
    assert controller.dispatch("start")["active"] is True
    assert [call[0][1] for call in systemctl.calls].count("start") == start_count
    assert controller.dispatch("touch")["active"] is True
    assert controller.dispatch("stop")["active"] is False
    stop_count = [call[0][1] for call in systemctl.calls].count("stop")
    assert controller.dispatch("stop")["active"] is False
    assert [call[0][1] for call in systemctl.calls].count("stop") == stop_count


def test_ds4_start_pauses_rizzo_and_stop_restores_it(tmp_path):
    systemctl = FakeSystemctl(active=False, shadow_active=True)
    marker = tmp_path / "rizzo-paused"
    controller = BottazziDs4Controller(
        run=systemctl,
        http_get=state_http(systemctl),
        sleep=lambda _delay: None,
        shadow_marker=marker,
    )
    started = controller.dispatch("start")
    assert started["active"] is True
    assert started["rizzo_shadow_paused"] is True
    assert systemctl.shadow_active is False
    assert marker.exists()

    transitions = [(call[0][1], call[0][2]) for call in systemctl.calls if call[0][1] in {"start", "stop"}]
    assert transitions[:2] == [("stop", RIZZO_SHADOW_UNIT), ("start", UNIT)]

    stopped = controller.dispatch("stop")
    assert stopped["active"] is False
    assert stopped["rizzo_shadow_restored"] is True
    assert stopped["rizzo_shadow_paused"] is False
    assert systemctl.shadow_active is True
    assert not marker.exists()
    transitions = [(call[0][1], call[0][2]) for call in systemctl.calls if call[0][1] in {"start", "stop"}]
    assert transitions[-2:] == [("stop", UNIT), ("start", RIZZO_SHADOW_UNIT)]


def test_ds4_start_failure_restores_rizzo(tmp_path):
    class FailingSystemctl(FakeSystemctl):
        def __call__(self, command, **kwargs):
            if command[1] == "start" and command[2] == UNIT:
                self.calls.append((command, kwargs))
                raise OSError("ds4 start failed")
            return super().__call__(command, **kwargs)

    systemctl = FailingSystemctl(active=False, shadow_active=True)
    marker = tmp_path / "rizzo-paused"
    controller = BottazziDs4Controller(
        run=systemctl,
        http_get=state_http(systemctl),
        sleep=lambda _delay: None,
        shadow_marker=marker,
    )
    with pytest.raises(OSError, match="ds4 start failed"):
        controller.dispatch("start")
    assert systemctl.shadow_active is True
    assert not marker.exists()


def test_ds4_start_works_when_rizzo_service_is_not_installed(tmp_path):
    systemctl = FakeSystemctl(active=False, shadow_missing=True)
    controller = BottazziDs4Controller(
        run=systemctl,
        http_get=state_http(systemctl),
        sleep=lambda _delay: None,
        shadow_marker=tmp_path / "rizzo-paused",
    )
    result = controller.dispatch("start")
    assert result["active"] is True
    assert result["rizzo_shadow_paused"] is False


def test_peer_policy_allows_only_backend_and_bandi_users():
    assert peer_uid_allowed(1000)
    assert peer_uid_allowed(1001)
    assert not peer_uid_allowed(0)
    assert not peer_uid_allowed(2000)


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        (b"not-json", "malformed_json"),
        (b"[]", "invalid_fields"),
        (b'{"action":"restart"}', "unsupported_action"),
        (b'{"action":"start","unit":"other.service"}', "invalid_fields"),
        (b"x" * (MAX_REQUEST_BYTES + 1), "request_too_large"),
    ],
)
def test_request_protocol_rejects_arbitrary_units_and_actions(raw, error):
    with pytest.raises(BrokerError, match=error):
        parse_request(raw)


def test_active_but_wrong_model_fails_closed():
    systemctl = FakeSystemctl(active=True)
    controller = BottazziDs4Controller(
        run=systemctl,
        http_get=lambda *_a, **_k: HttpResponse(b'{"data":[{"id":"not-deepseek"}]}'),
        sleep=lambda _delay: None,
    )
    state = controller.status()
    assert state["ok"] is False
    assert state["error"] == "active_unit_not_ready"
