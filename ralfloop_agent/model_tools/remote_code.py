from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import uuid4

import requests


MAX_SOURCE_BYTES = 256 * 1024
MAX_OUTPUT_BYTES = 16 * 1024
MAX_FILES = 64
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
ALLOWED_CONTENT_TYPES = frozenset(
    {
        "application/javascript",
        "application/x-python-code",
        "text/javascript",
        "text/plain",
        "text/x-python",
        "text/x-script.python",
    }
)
PINNED_INTERPRETERS = {"python3": (Path("/usr/bin/python3"), "Python 3.12.")}


class RemoteCodeError(RuntimeError):
    pass


class DownloadPolicyError(RemoteCodeError):
    pass


class SandboxUnavailable(RemoteCodeError):
    pass


@dataclass(frozen=True)
class RemoteCodeArtifact:
    source: bytes
    source_url: str
    final_url: str
    content_type: str
    peer_ip: str
    sha256: str
    size_bytes: int
    fetched_at: str


@dataclass(frozen=True)
class ScanFinding:
    rule: str
    severity: str
    reason: str


@dataclass(frozen=True)
class StaticScan:
    classification: str
    findings: tuple[ScanFinding, ...]


@dataclass
class _Capture:
    limit: int
    total: int = 0
    head: bytearray = field(default_factory=bytearray)
    tail: deque[bytes] = field(default_factory=deque)
    tail_size: int = 0
    digest: Any = field(default_factory=hashlib.sha256)
    exceeded: bool = False

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        self.digest.update(chunk)
        half = max(1, self.limit // 2)
        if len(self.head) < half:
            keep = min(half - len(self.head), len(chunk))
            self.head.extend(chunk[:keep])
        self.tail.append(chunk)
        self.tail_size += len(chunk)
        while self.tail and self.tail_size - len(self.tail[0]) >= half:
            self.tail_size -= len(self.tail.popleft())
        self.exceeded = self.total > self.limit

    def rendered(self) -> tuple[str, bool, str]:
        if self.total <= self.limit:
            raw = bytes(self.head)
            if self.total > len(raw):
                raw += b"".join(self.tail)[-(self.total - len(raw)) :]
        else:
            raw = bytes(self.head) + b"\n...[truncated]...\n" + b"".join(self.tail)[-(self.limit // 2) :]
        return _redact(raw.decode("utf-8", errors="replace")), self.total > len(raw), self.digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
        return urlunsplit((parts.scheme, host + port, parts.path, "", ""))
    except ValueError:
        return "invalid-url"


def _redact(text: str) -> str:
    rules = (
        (re.compile(r"(?i)(authorization\s*[:=]\s*)([^\s]+)"), r"\1[REDACTED]"),
        (re.compile(r"(?i)((?:api[_-]?key|token|password|secret)\s*[:=]\s*)['\"]?[^\s'\"]+"), r"\1[REDACTED]"),
        (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "[REDACTED PRIVATE KEY]"),
    )
    for pattern, replacement in rules:
        text = pattern.sub(replacement, text)
    return text


def _public_ips(host: str, port: int, resolver: Callable[..., Any] = socket.getaddrinfo) -> tuple[str, ...]:
    try:
        rows = resolver(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise DownloadPolicyError("dns_resolution_failed") from exc
    addresses: set[str] = set()
    for row in rows:
        raw = str(row[4][0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise DownloadPolicyError("dns_invalid_address") from exc
        if not address.is_global:
            raise DownloadPolicyError("non_public_address_forbidden")
        addresses.add(str(address))
    if not addresses:
        raise DownloadPolicyError("dns_empty")
    return tuple(sorted(addresses))


def _validate_public_url(url: str, resolver: Callable[..., Any] = socket.getaddrinfo) -> tuple[str, ...]:
    if not url or len(url.encode("utf-8")) > 2048:
        raise DownloadPolicyError("url_length_invalid")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise DownloadPolicyError("url_invalid") from exc
    if parts.scheme != "https":
        raise DownloadPolicyError("https_required")
    if parts.username or parts.password:
        raise DownloadPolicyError("url_credentials_forbidden")
    if not parts.hostname:
        raise DownloadPolicyError("url_host_missing")
    try:
        port = parts.port or 443
    except ValueError as exc:
        raise DownloadPolicyError("url_port_invalid") from exc
    return _public_ips(parts.hostname, port, resolver)


def _peer_address(response: requests.Response) -> str:
    candidates = (
        getattr(getattr(response.raw, "_connection", None), "sock", None),
        getattr(getattr(getattr(getattr(response.raw, "_fp", None), "fp", None), "raw", None), "_sock", None),
    )
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            return str(candidate.getpeername()[0]).split("%", 1)[0]
        except (AttributeError, OSError, TypeError):
            continue
    raise DownloadPolicyError("peer_address_unavailable")


class PublicCodeDownloader:
    def __init__(
        self,
        *,
        max_bytes: int = MAX_SOURCE_BYTES,
        connect_timeout: float = 3.0,
        total_timeout: float = 12.0,
        max_redirects: int = 3,
        resolver: Callable[..., Any] = socket.getaddrinfo,
        session: requests.Session | None = None,
        peer_address: Callable[[requests.Response], str] = _peer_address,
    ) -> None:
        self.max_bytes = max(1, min(int(max_bytes), MAX_SOURCE_BYTES))
        self.connect_timeout = max(0.1, float(connect_timeout))
        self.total_timeout = max(self.connect_timeout, float(total_timeout))
        self.max_redirects = max(0, min(int(max_redirects), 5))
        self.resolver = resolver
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.peer_address = peer_address

    def fetch(self, url: str) -> RemoteCodeArtifact:
        original = str(url).strip()
        current = original
        started = time.monotonic()
        for redirect in range(self.max_redirects + 1):
            resolved = _validate_public_url(current, self.resolver)
            response = self.session.get(
                current,
                allow_redirects=False,
                headers={"Accept": "text/plain, text/x-python, application/x-python-code"},
                stream=True,
                timeout=(self.connect_timeout, min(self.total_timeout, 5.0)),
            )
            if 300 <= response.status_code < 400:
                location = response.headers.get("Location", "").strip()
                response.close()
                if not location or redirect >= self.max_redirects:
                    raise DownloadPolicyError("redirect_policy_failed")
                current = urljoin(current, location)
                continue
            try:
                response.raise_for_status()
                peer = self.peer_address(response)
                if peer not in resolved or not ipaddress.ip_address(peer).is_global:
                    raise DownloadPolicyError("dns_rebinding_or_proxy_detected")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
                if content_type not in ALLOWED_CONTENT_TYPES:
                    raise DownloadPolicyError("content_type_forbidden")
                raw_length = response.headers.get("Content-Length", "").strip()
                if raw_length:
                    try:
                        if int(raw_length) > self.max_bytes:
                            raise DownloadPolicyError("source_too_large")
                    except ValueError as exc:
                        raise DownloadPolicyError("content_length_invalid") from exc
                chunks: list[bytes] = []
                size = 0
                digest = hashlib.sha256()
                for chunk in response.iter_content(8192):
                    if time.monotonic() - started > self.total_timeout:
                        raise DownloadPolicyError("download_timeout")
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise DownloadPolicyError("source_too_large")
                    digest.update(chunk)
                    chunks.append(chunk)
                source = b"".join(chunks)
            finally:
                response.close()
            if not source or b"\x00" in source:
                raise DownloadPolicyError("source_not_text")
            try:
                source.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise DownloadPolicyError("source_not_utf8") from exc
            return RemoteCodeArtifact(
                source=source,
                source_url=_safe_url(original),
                final_url=_safe_url(current),
                content_type=content_type,
                peer_ip=peer,
                sha256=digest.hexdigest(),
                size_bytes=size,
                fetched_at=_now(),
            )
        raise DownloadPolicyError("redirect_policy_failed")


_BLOCK_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("credential_access", re.compile(r"(?i)(/\.ssh/|/\.aws/|/\.config/gcloud|/etc/shadow|docker\.sock|credential|keyring|id_rsa|AWS_SECRET_ACCESS_KEY)"), "credential or privileged socket access"),
    ("persistence", re.compile(r"(?i)(crontab|systemctl\s+(?:enable|start)|/etc/(?:systemd|cron)|\.bashrc|\.profile|autostart|authorized_keys)"), "persistence attempt"),
    ("privilege_escalation", re.compile(r"(?i)(\bsudo\b|\bpkexec\b|\bsetuid\b|chmod\s+[u+]s|capset|setcap|/proc/\d+/mem)"), "privilege escalation attempt"),
    ("reverse_shell", re.compile(r"(?is)(/dev/tcp/|\bnc\s+[^\n]*\s-e\b|bash\s+-i|(?:socket\s*\.|\w+\.)connect\s*\([^\n]{0,240}(?:subprocess|pty|dup2)|pty\.spawn\s*\(\s*['\"]/(?:bin/)?(?:ba)?sh)"), "reverse shell pattern"),
    ("destructive_command", re.compile(r"(?i)(rm\s+-[^\n]*r[^\n]*f\s+/(?:\s|$)|\bmkfs(?:\.|\s)|dd\s+if=.*\s+of=/dev/|\bshred\b|:\(\)\s*\{\s*:\|:\s*&\s*\}\s*;\s*:|shutdown\s+-|\breboot\b)"), "destructive command"),
    ("sandbox_escape", re.compile(r"(?i)(\bmount\s|\bumount\s|\bunshare\b|\bsetns\b|\bptrace\b|/proc/(?:1|self)/(?:root|mem|fd)|nsenter|pivot_root|chroot|bpf\s*\()"), "sandbox escape primitive"),
    ("secondary_download", re.compile(r"(?i)(\bcurl\s+https?://|\bwget\s+https?://|requests\.(?:get|post|request)\s*\(|urllib\.request|urlopen\s*\(|httpx\.|aiohttp\.|subprocess[^\n]{0,160}(?:curl|wget))"), "secondary download attempt"),
    ("embedded_secret", re.compile(r"(?i)(BEGIN [A-Z ]*PRIVATE KEY|(?:api[_-]?key|secret|password|token)\s*=\s*['\"][^'\"]{8,})"), "embedded secret"),
)
_SUSPICIOUS_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("dynamic_execution", re.compile(r"(?i)(\beval\s*\(|\bexec\s*\(|\bcompile\s*\(|__import__\s*\()"), "dynamic execution"),
    ("encoded_payload", re.compile(r"(?i)(base64\.(?:b64decode|decodebytes)|marshal\.loads|zlib\.decompress|bytes\.fromhex|codecs\.decode[^\n]{0,80}(?:hex|rot_13))"), "encoded or compressed payload"),
    ("character_obfuscation", re.compile(r"(?i)(chr\s*\([^)]*\)\s*\+|join\s*\([^\n]{0,100}chr\s*\()"), "character-built payload"),
    ("process_spawn", re.compile(r"(?i)(subprocess\.|os\.(?:system|popen|spawn|exec))"), "child process request"),
)


def scan_remote_code(source: bytes) -> StaticScan:
    text = source.decode("utf-8", errors="strict")
    findings: list[ScanFinding] = []
    for rule, pattern, reason in _BLOCK_RULES:
        if pattern.search(text):
            findings.append(ScanFinding(rule, "blocked", reason))
    for rule, pattern, reason in _SUSPICIOUS_RULES:
        if pattern.search(text):
            findings.append(ScanFinding(rule, "suspicious", reason))
    if any(item.severity == "blocked" for item in findings):
        classification = "blocked"
    elif findings:
        classification = "suspicious"
    else:
        classification = "safe"
    return StaticScan(classification, tuple(findings))


class ApprovalStore:
    def __init__(self, root: str | Path | None = None) -> None:
        configured = root or os.getenv("RALF_REMOTE_CODE_APPROVAL_DIR", "")
        self.root = Path(configured).expanduser() if configured else Path.home() / ".local/state/ralf/remote-code-approvals"

    def consume(self, approval_id: str, artifact_hash: str) -> tuple[bool, str]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,96}", approval_id):
            return False, "approval_id_invalid"
        path = self.root / f"{approval_id}.json"
        try:
            if path.is_symlink():
                return False, "approval_symlink_forbidden"
            stat = path.stat()
            if not path.is_file() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
                return False, "approval_permissions_invalid"
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return False, "approval_unavailable"
        if payload.get("approved") is not True or payload.get("sha256") != artifact_hash:
            return False, "approval_hash_mismatch"
        try:
            expires = datetime.fromisoformat(str(payload["expires_at"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            return False, "approval_expiry_invalid"
        if expires <= datetime.now(timezone.utc):
            return False, "approval_expired"
        used = self.root / f".{approval_id}.used-{uuid4().hex}.json"
        try:
            path.replace(used)
        except OSError:
            return False, "approval_consume_failed"
        return True, "approval_valid"


_DENIED_SYSCALLS_X86_64 = frozenset(
    {
        *range(41, 56), 101, 155, 165, 166, 167, 168, 169, 170, 171, 172, 173,
        175, 176, 179, 246, 248, 249, 250, 272, 279, 298, 304, 308, 310, 311,
        312, 313, 321, 323, 425, 426, 427, 428, 429, 430, 431, 432, 433, 435,
        438, 442,
    }
)


def build_seccomp_filter() -> bytes:
    if platform.machine() != "x86_64":
        raise SandboxUnavailable("unsupported_seccomp_architecture")
    instructions: list[tuple[int, int, int, int]] = [
        (0x20, 0, 0, 4),
        (0x15, 1, 0, 0xC000003E),
        (0x06, 0, 0, 0x80000000),
        (0x20, 0, 0, 0),
    ]
    for syscall_number in sorted(_DENIED_SYSCALLS_X86_64):
        instructions.extend(((0x15, 0, 1, syscall_number), (0x06, 0, 0, 0x00050000 | 1)))
    instructions.append((0x06, 0, 0, 0x7FFF0000))
    return b"".join(struct.pack("HBBI", *instruction) for instruction in instructions)


def _launcher_bootstrap() -> str:
    root = Path(__file__).resolve().parents[2]
    return (
        "import sys;"
        f"sys.path.insert(0,{str(root)!r});"
        "from ralfloop_agent.model_tools.remote_code import launcher_main;"
        "raise SystemExit(launcher_main())"
    )


def _sandbox_python_bootstrap() -> str:
    return (
        "import ctypes,runpy,sys;"
        "F=type('F',(ctypes.Structure,),{'_fields_':[('code',ctypes.c_ushort),('jt',ctypes.c_ubyte),('jf',ctypes.c_ubyte),('k',ctypes.c_uint)]});"
        "P=type('P',(ctypes.Structure,),{'_fields_':[('len',ctypes.c_ushort),('filter',ctypes.POINTER(F))]});"
        "a=(F*5)(F(0x20,0,0,0),F(0x15,0,1,59),F(0x06,0,0,0x50001),F(0x15,0,1,322),F(0x06,0,0,0x50001));"
        "b=(F*6)(*a,F(0x06,0,0,0x7fff0000));"
        "p=P(6,b);libc=ctypes.CDLL(None,use_errno=True);"
        "assert libc.prctl(38,1,0,0,0)==0;"
        "assert libc.syscall(317,1,0,ctypes.byref(p))==0;"
        "runpy.run_path(sys.argv[1],run_name='__main__')"
    )


def launcher_main(argv: list[str] | None = None) -> int:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or args[1] != "--":
        raise SystemExit("invalid_sandbox_launcher_arguments")
    filter_path = Path(args[0]).resolve(strict=True)
    command = args[2:]
    if not command or Path(command[0]).resolve() != Path("/usr/bin/bwrap"):
        raise SystemExit("sandbox_launcher_command_forbidden")
    fd = os.open(filter_path, os.O_RDONLY | os.O_CLOEXEC)
    os.set_inheritable(fd, True)
    try:
        separator = command.index("--")
    except ValueError as exc:
        raise SystemExit("sandbox_launcher_separator_missing") from exc
    command[separator:separator] = ["--seccomp", str(fd)]
    os.execve(command[0], command, {"PATH": "/usr/bin", "LANG": "C.UTF-8"})
    return 127


class BubblewrapSandbox:
    def __init__(
        self,
        *,
        timeout_sec: float = 5.0,
        memory_mb: int = 128,
        pids_max: int = 24,
        cpu_quota: int = 50,
        output_bytes: int = MAX_OUTPUT_BYTES,
        interpreter_table: dict[str, tuple[Path, str]] | None = None,
        launcher_python: str | Path | None = None,
    ) -> None:
        self.timeout_sec = max(0.2, min(float(timeout_sec), 30.0))
        self.memory_mb = max(32, min(int(memory_mb), 512))
        self.pids_max = max(4, min(int(pids_max), 64))
        self.cpu_quota = max(5, min(int(cpu_quota), 100))
        self.output_bytes = max(1024, min(int(output_bytes), 64 * 1024))
        self.interpreters = interpreter_table or PINNED_INTERPRETERS
        self.launcher_python = Path(launcher_python or sys.executable).absolute()

    def _interpreter(self, name: str) -> Path:
        try:
            path, version_prefix = self.interpreters[name]
        except KeyError as exc:
            raise SandboxUnavailable("interpreter_not_allowlisted") from exc
        if not path.is_file() or not os.access(path, os.X_OK):
            raise SandboxUnavailable("interpreter_unavailable")
        try:
            version = subprocess.run(
                [str(path), "--version"], check=True, capture_output=True, text=True,
                timeout=2, env={"PATH": "/usr/bin", "LANG": "C"},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SandboxUnavailable("interpreter_version_probe_failed") from exc
        if not (version.stdout + version.stderr).strip().startswith(version_prefix):
            raise SandboxUnavailable("interpreter_version_mismatch")
        return path

    def _bwrap_command(self, work: Path, interpreter: Path) -> list[str]:
        command = [
            "/usr/bin/bwrap", "--unshare-user", "--uid", "65534", "--gid", "65534",
            "--unshare-pid", "--unshare-net", "--unshare-ipc", "--unshare-uts",
            "--unshare-cgroup", "--disable-userns", "--assert-userns-disabled",
            "--hostname", "ralf-remote-code", "--die-with-parent", "--new-session",
            "--clearenv", "--ro-bind", "/usr", "/usr",
        ]
        for library in ("/lib", "/lib64"):
            if Path(library).exists():
                command.extend(("--ro-bind", library, library))
        command.extend(
            (
                "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/run",
                "--dir", "/home", "--dir", "/home/sandbox", "--bind", str(work), "/work",
                "--chdir", "/work", "--setenv", "PATH", "/usr/bin", "--setenv", "HOME",
                "/home/sandbox", "--setenv", "LANG", "C.UTF-8", "--setenv",
                "PYTHONNOUSERSITE", "1", "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
                "--cap-drop", "ALL", "--", "/usr/bin/prlimit",
                f"--as={self.memory_mb * 1024 * 1024}", f"--nproc={self.pids_max}",
                f"--cpu={max(1, int(self.timeout_sec))}", "--fsize=1048576", "--nofile=64",
                "/usr/bin/setpriv", "--no-new-privs", str(interpreter), "-I", "-S", "-c",
                _sandbox_python_bootstrap(), "/work/program.py",
            )
        )
        return command

    @staticmethod
    def _reader(stream: Any, capture: _Capture, limit_event: threading.Event) -> None:
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                capture.add(chunk)
                if capture.exceeded:
                    limit_event.set()
        finally:
            stream.close()

    @staticmethod
    def _kill_group(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=0.5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def execute(self, artifact: RemoteCodeArtifact, *, interpreter_name: str = "python3") -> dict[str, Any]:
        interpreter = self._interpreter(interpreter_name)
        if not Path("/usr/bin/bwrap").is_file() or not Path("/usr/bin/systemd-run").is_file():
            raise SandboxUnavailable("sandbox_runtime_unavailable")
        sandbox_id = uuid4().hex
        root_path: Path | None = None
        try:
            with tempfile.TemporaryDirectory(prefix="ralf-remote-code-") as root:
                root_path = Path(root)
                work = root_path / "work"
                work.mkdir(mode=0o700)
                program = work / "program.py"
                program.write_bytes(artifact.source)
                program.chmod(0o400)
                filter_path = root_path / "seccomp.bpf"
                seccomp = build_seccomp_filter()
                filter_path.write_bytes(seccomp)
                filter_path.chmod(0o400)
                bwrap_command = self._bwrap_command(work, interpreter)
                unit = f"ralf-remote-code-{sandbox_id[:16]}.scope"
                command = [
                    "/usr/bin/systemd-run", "--user", "--scope", "--quiet", "--collect",
                    f"--unit={unit}", f"--property=MemoryMax={self.memory_mb}M",
                    "--property=MemorySwapMax=0", f"--property=TasksMax={self.pids_max}",
                    f"--property=CPUQuota={self.cpu_quota}%", "--property=IOAccounting=yes",
                    "--property=IOWeight=10", "--property=IOReadBandwidthMax=/ 8M",
                    "--property=IOWriteBandwidthMax=/ 2M", "--", str(self.launcher_python), "-I", "-c",
                    _launcher_bootstrap(), str(filter_path), "--", *bwrap_command,
                ]
                env = {
                    "PATH": "/usr/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent",
                    "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
                    "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus",
                    "PYTHONNOUSERSITE": "1",
                }
                process = subprocess.Popen(
                    command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=root_path, env=env, shell=False, start_new_session=True,
                )
                assert process.stdout is not None and process.stderr is not None
                stdout_capture = _Capture(self.output_bytes)
                stderr_capture = _Capture(self.output_bytes)
                limit_event = threading.Event()
                threads = [
                    threading.Thread(target=self._reader, args=(process.stdout, stdout_capture, limit_event), daemon=True),
                    threading.Thread(target=self._reader, args=(process.stderr, stderr_capture, limit_event), daemon=True),
                ]
                for thread in threads:
                    thread.start()
                started = time.monotonic()
                stop_reason = "exited"
                while process.poll() is None:
                    if limit_event.is_set():
                        stop_reason = "output_limit"
                        self._kill_group(process)
                        break
                    if time.monotonic() - started >= self.timeout_sec:
                        stop_reason = "timeout"
                        self._kill_group(process)
                        break
                    time.sleep(0.02)
                for thread in threads:
                    thread.join(timeout=1)
                if limit_event.is_set():
                    stop_reason = "output_limit"
                if process.poll() is None:
                    self._kill_group(process)
                return_code = process.wait(timeout=1)
                stdout, stdout_truncated, stdout_hash = stdout_capture.rendered()
                stderr, stderr_truncated, stderr_hash = stderr_capture.rendered()
                files: list[dict[str, Any]] = []
                artifact_bytes = 0
                artifacts_truncated = False
                for path in sorted(work.rglob("*")):
                    if path == program or not path.is_file() or path.is_symlink():
                        continue
                    size = path.stat().st_size
                    if len(files) >= MAX_FILES or artifact_bytes + size > MAX_ARTIFACT_BYTES:
                        artifacts_truncated = True
                        continue
                    digest = hashlib.sha256()
                    with path.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(65536), b""):
                            digest.update(chunk)
                    artifact_bytes += size
                    files.append({"path": path.relative_to(work).as_posix(), "size_bytes": size,
                        "sha256": digest.hexdigest()})
                result = {
                    "sandbox_id": sandbox_id, "backend": "bubblewrap+systemd-cgroup-v2",
                    "exit_code": return_code, "stop_reason": stop_reason, "stdout": stdout,
                    "stderr": stderr, "stdout_bytes": stdout_capture.total,
                    "stderr_bytes": stderr_capture.total, "stdout_sha256": stdout_hash,
                    "stderr_sha256": stderr_hash,
                    "truncated": stdout_truncated or stderr_truncated or stop_reason == "output_limit",
                    "artifacts": files, "artifact_bytes": artifact_bytes,
                    "artifacts_truncated": artifacts_truncated,
                    "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
                    "limits": {"timeout_sec": self.timeout_sec, "memory_mb": self.memory_mb,
                        "pids_max": self.pids_max, "cpu_quota_percent": self.cpu_quota,
                        "io_weight": 10, "io_read_bytes_per_sec": 8 * 1024 * 1024,
                        "io_write_bytes_per_sec": 2 * 1024 * 1024,
                        "output_bytes": self.output_bytes},
                    "isolation": {"user_namespace": True, "pid_namespace": True,
                        "mount_namespace": True, "network_namespace": True,
                        "cgroup_namespace": True, "network": "disabled",
                        "root": "read_only_minimal", "host_data_mounts": False,
                        "runtime_ro_binds": ["/usr", "/lib", "/lib64"],
                        "docker_socket": False, "home": "empty", "environment": "allowlist",
                        "uid": 65534, "capabilities": "none", "no_new_privileges": True,
                        "seccomp_sha256": hashlib.sha256(seccomp).hexdigest()},
                }
            result["cleanup"] = bool(root_path and not root_path.exists())
            return result
        finally:
            if root_path and root_path.exists():
                shutil.rmtree(root_path, ignore_errors=True)


def _base_output() -> dict[str, Any]:
    return {
        "classification": "failed", "reason": "not_started", "executed": False,
        "approval_required": False, "sha256": "", "provenance": {}, "findings": [],
        "sandbox": {}, "stdout": "", "stderr": "", "exit_code": None,
        "artifacts": [], "cleanup": True, "audit": [],
    }


class SandboxedRemoteCodeTool:
    def __init__(
        self, *, downloader: PublicCodeDownloader | None = None,
        sandbox: BubblewrapSandbox | None = None, approvals: ApprovalStore | None = None,
    ) -> None:
        self.downloader = downloader or PublicCodeDownloader()
        self.sandbox = sandbox or BubblewrapSandbox()
        self.approvals = approvals or ApprovalStore()

    def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        output = _base_output()
        audit: list[dict[str, Any]] = output["audit"]
        url = str(payload.get("url") or "")
        audit.append({"event": "remote_code_download_started", "url": _safe_url(url)[:2048], "at": _now()})
        try:
            artifact = self.downloader.fetch(url)
            output["sha256"] = artifact.sha256
            output["provenance"] = {
                "source_url": artifact.source_url, "final_url": artifact.final_url,
                "content_type": artifact.content_type, "peer_ip": artifact.peer_ip,
                "size_bytes": artifact.size_bytes, "fetched_at": artifact.fetched_at,
            }
            audit.append({"event": "remote_code_downloaded", "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes, "content_type": artifact.content_type, "at": _now()})
            scan = scan_remote_code(artifact.source)
            output["classification"] = scan.classification
            output["findings"] = [finding.__dict__ for finding in scan.findings]
            audit.append({"event": "remote_code_scanned", "classification": scan.classification,
                "finding_rules": [finding.rule for finding in scan.findings], "sha256": artifact.sha256, "at": _now()})
            if scan.classification == "blocked":
                output["reason"] = "static_policy_blocked"
                audit.append({"event": "remote_code_blocked", "sha256": artifact.sha256, "at": _now()})
                return output
            if scan.classification == "suspicious":
                output["approval_required"] = True
                approval_id = str(payload.get("approval_id") or "")
                approved, reason = self.approvals.consume(approval_id, artifact.sha256) if approval_id else (False, "explicit_approval_required")
                audit.append({"event": "remote_code_approval_checked", "approved": approved,
                    "reason": reason, "sha256": artifact.sha256, "at": _now()})
                if not approved:
                    output["reason"] = reason
                    return output
            audit.append({"event": "remote_code_sandbox_started", "sha256": artifact.sha256,
                "network": "disabled", "at": _now()})
            result = self.sandbox.execute(artifact, interpreter_name=str(payload.get("interpreter") or "python3"))
            output["executed"] = True
            output["sandbox"] = {key: value for key, value in result.items() if key not in {"stdout", "stderr", "artifacts"}}
            output["stdout"] = result["stdout"]
            output["stderr"] = result["stderr"]
            output["exit_code"] = result["exit_code"]
            output["artifacts"] = result["artifacts"]
            output["cleanup"] = bool(result["cleanup"])
            output["reason"] = "execution_completed" if result["exit_code"] == 0 and result["stop_reason"] == "exited" else result["stop_reason"]
            if output["reason"] != "execution_completed":
                output["classification"] = "failed"
            audit.append({"event": "remote_code_sandbox_finished", "sha256": artifact.sha256,
                "exit_code": result["exit_code"], "stop_reason": result["stop_reason"],
                "stdout_bytes": result["stdout_bytes"], "stderr_bytes": result["stderr_bytes"],
                "cleanup": result["cleanup"], "at": _now()})
            audit.append({"event": "remote_code_sandbox_destroyed", "sha256": artifact.sha256,
                "cleanup": result["cleanup"], "at": _now()})
            return output
        except (RemoteCodeError, requests.RequestException, OSError, subprocess.SubprocessError) as exc:
            output["classification"] = "failed"
            output["reason"] = str(exc)[:160] or type(exc).__name__
            audit.append({"event": "remote_code_failed", "error_type": type(exc).__name__,
                "reason": output["reason"], "at": _now()})
            return output


def run_sandboxed_remote_code(payload: dict[str, Any]) -> dict[str, Any]:
    return SandboxedRemoteCodeTool().run(payload)


__all__ = [
    "ApprovalStore", "BubblewrapSandbox", "DownloadPolicyError", "PublicCodeDownloader",
    "RemoteCodeArtifact", "SandboxUnavailable", "SandboxedRemoteCodeTool", "StaticScan",
    "build_seccomp_filter", "launcher_main", "run_sandboxed_remote_code", "scan_remote_code",
]
