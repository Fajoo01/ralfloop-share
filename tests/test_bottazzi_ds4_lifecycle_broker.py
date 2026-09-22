from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from scripts.ralf_bottazzi_ds4_lifecycle_broker import (
    BottazziDs4Controller,
    BrokerError,
    MAX_REQUEST_BYTES,
    MODEL,
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
    def __init__(self, active: bool = False):
        self.active = active
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        assert command[0] == "systemctl"
        assert command[-1] == UNIT or command[2] == UNIT
        if command[1] == "start":
            self.active = True
        elif command[1] == "stop":
            self.active = False
        stdout = (
            f"ActiveState={'active' if self.active else 'inactive'}\n"
            f"SubState={'running' if self.active else 'dead'}\n"
            f"MainPID={1439 if self.active else 0}\n"
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
