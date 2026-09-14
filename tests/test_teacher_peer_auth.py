from __future__ import annotations

import importlib.util
from pathlib import Path
import struct
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "ralf_mcp_peer_auth.py"


def _module():
    spec = importlib.util.spec_from_file_location("ralf_mcp_peer_auth_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


class FakeConn:
    def __init__(self, uid: int):
        self.uid = uid

    def getsockopt(self, *_args):
        return struct.pack("3i", 4242, self.uid, 999)


def test_peer_allowed_by_shared_group(monkeypatch):
    mod = _module()
    monkeypatch.setattr(mod.pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_name="bandi", pw_gid=1001))
    monkeypatch.setattr(mod.os, "getgrouplist", lambda _name, _gid: [1001, 986, 987])
    monkeypatch.setattr(mod.grp, "getgrnam", lambda _name: SimpleNamespace(gr_gid=987))

    assert mod.peer_allowed(FakeConn(1001), "ralf-mcp") is True


def test_peer_allowed_legacy_uid_override(monkeypatch):
    mod = _module()
    monkeypatch.setattr(mod.pwd, "getpwuid", lambda _uid: (_ for _ in ()).throw(AssertionError()))

    assert mod.peer_allowed(FakeConn(1001), "ralf-mcp", 1001) is True
    assert mod.peer_allowed(FakeConn(1002), "ralf-mcp", 1001) is False


def test_assign_socket_group_sets_shared_mode(monkeypatch, tmp_path):
    mod = _module()
    target = tmp_path / "mcp.sock"
    target.touch()
    calls = []
    monkeypatch.setattr(mod.grp, "getgrnam", lambda _name: SimpleNamespace(gr_gid=987))
    monkeypatch.setattr(mod.os, "chown", lambda path, uid, gid: calls.append(("chown", Path(path), uid, gid)))
    monkeypatch.setattr(mod.os, "chmod", lambda path, mode: calls.append(("chmod", Path(path), mode)))

    mod.assign_socket_group(target, "ralf-mcp")

    assert calls == [
        ("chown", target, -1, 987),
        ("chmod", target, 0o660),
    ]


def test_repository_service_uses_group_authenticated_broker():
    unit = (ROOT / "deploy/systemd/ralf-teacher-mcp-broker.service").read_text()
    assert "Group=ralf-mcp" in unit
    assert "SupplementaryGroups=bandi" in unit
    assert "RuntimeDirectoryMode=2770" in unit
    assert "--allow-group ralf-mcp" in unit
    assert "UMask=0007" in unit
