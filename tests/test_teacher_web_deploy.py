import hashlib
import json
from pathlib import Path
import sys

import pytest

from tools import install_teacher_web as installer


def test_web_unit_is_private_non_root_user_service():
    unit=Path("deploy/systemd/ralf-teacher-web.service").read_text()
    assert "WorkingDirectory=%h/.local/share/ralf-teacher-web/current" in unit
    assert "User=root" not in unit
    assert "Restart=on-failure" in unit and "UMask=0077" in unit
    assert "PrivateTmp=yes" not in unit
    assert "ralf-teacher-inference" not in unit
    assert "0.0.0.0" not in unit


def test_preparation_verifies_manifest_without_publishing(tmp_path,monkeypatch,capsys):
    commit="a"*40
    release=tmp_path / ".local/share/ralf-teacher-web/releases" / commit
    release.mkdir(parents=True)
    payload=b"verified content"
    (release / "file").write_bytes(payload)
    (release / "MANIFEST.sha256").write_text(hashlib.sha256(payload).hexdigest()+"  file\n")
    monkeypatch.setattr(Path,"home",classmethod(lambda cls:tmp_path))
    monkeypatch.setattr(sys,"argv",["install_teacher_web.py","--rollback",commit])
    installer.main()
    result=json.loads(capsys.readouterr().out)
    assert result["status"] == "prepared"
    assert not (release.parent.parent / "current").exists()
    (release / "file").write_bytes(b"tampered")
    with pytest.raises(SystemExit,match="integrity_failed"): installer.main()


def test_invalid_rollback_path_rejected(monkeypatch):
    monkeypatch.setattr(sys,"argv",["install_teacher_web.py","--rollback","../../etc"])
    with pytest.raises(SystemExit,match="invalid_release_commit"): installer.main()


def test_failed_health_restores_only_previous_web_release(tmp_path,monkeypatch):
    root=tmp_path / ".local/share/ralf-teacher-web"
    old=root / "releases" / ("b"*40)
    release=root / "releases" / ("a"*40)
    old.mkdir(parents=True)
    (release / "deploy/systemd").mkdir(parents=True)
    unit=release / "deploy/systemd/ralf-teacher-web.service"
    unit.write_text("[Service]\nExecStart=python scripts/ralf_teacher_web.py\n")
    (release / "MANIFEST.sha256").write_text(hashlib.sha256(unit.read_bytes()).hexdigest()+"  deploy/systemd/ralf-teacher-web.service\n")
    (root / "current").symlink_to(old)
    installed_unit=tmp_path / ".config/systemd/user/ralf-teacher-web.service"
    installed_unit.parent.mkdir(parents=True)
    installed_unit.write_text("previous healthy unit")
    config=tmp_path / ".config/ralf-teacher-web.env"
    db=tmp_path / ".local/state/ralf-teacher-web/student.sqlite3"
    config.write_text(f"TEACHER_WEB_DB={db}\nTEACHER_WEB_ORIGIN=https://teacher.example\n")
    monkeypatch.setattr(Path,"home",classmethod(lambda cls:tmp_path))
    monkeypatch.setattr(sys,"argv",["install_teacher_web.py","--install","--rollback","a"*40])
    commands=[]
    monkeypatch.setattr(installer.subprocess,"run",lambda cmd,**kwargs:commands.append(cmd))
    monkeypatch.setattr(installer.time,"sleep",lambda duration:None)
    requests=[]
    def unavailable(request,*args,**kwargs):
        requests.append(request)
        raise ConnectionError("unavailable")
    monkeypatch.setattr(installer.urllib.request,"urlopen",unavailable)
    with pytest.raises(ConnectionError): installer.main()
    assert requests and requests[0].get_header("Host") == "teacher.example"
    assert (root / "current").resolve() == old
    assert installed_unit.read_text() == "previous healthy unit"
    assert all(cmd[:2]==["systemctl","--user"] for cmd in commands)
    assert all("ralf-teacher-inference.service" not in cmd for cmd in commands)
    assert (tmp_path / ".config/ralf-teacher-web.env").stat().st_mode & 0o777 == 0o600
