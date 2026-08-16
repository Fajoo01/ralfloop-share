from __future__ import annotations

from dataclasses import asdict, dataclass
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import shutil
import statistics
import struct
import subprocess
import tempfile
import time
from typing import Any, Iterable


@dataclass(frozen=True)
class SandboxLimits:
    wall_seconds: float = 3.0
    cpu_seconds: int = 2
    memory_mb: int = 256
    output_bytes: int = 65536
    file_bytes: int = 16 * 1024 * 1024
    tasks: int = 32


@dataclass(frozen=True)
class TestVector:
    stdin: str
    stdout: str
    name: str


TestVector.__test__ = False


@dataclass(frozen=True)
class EvaluationResult:
    correct: bool
    compilation_success: bool
    tests_passed: int
    tests_total: int
    wall_ms: float | None
    cpu_ms: float | None
    rss_mb: float | None
    binary_kb: float | None
    timeout: bool
    crash: bool
    sanitizer_errors: bool
    deterministic_output: bool
    compiler: str
    compiler_version: str
    flags: tuple[str, ...]
    source_hash: str
    binary_hash: str | None
    dataset_hash: str
    evaluator_version: str
    cpu_affinity: tuple[int, ...]
    random_seed: int
    run_count: int
    warmup_count: int
    median_ms: float | None
    p95_ms: float | None
    noise: float | None
    failure: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class BubblewrapSandbox:
    """Runs candidate compiler and binary only inside a networkless namespace."""

    evaluator_version = "c-evaluator-v1"
    fixed_flags = ("-std=c11", "-O2", "-pipe", "-fno-ident")
    allowed_flags = (
        ("-std=c11", "-O2", "-pipe", "-fno-ident"),
        ("-std=c11", "-O3", "-pipe", "-fno-ident"),
    )

    def __init__(self, *, bwrap: str = "/usr/bin/bwrap", compiler: str = "/usr/bin/gcc", limits: SandboxLimits | None = None):
        self.bwrap = bwrap
        self.compiler = compiler
        self.limits = limits or SandboxLimits()
        self._user_systemd_available: bool | None = None

    @property
    def available(self) -> bool:
        return Path(self.bwrap).is_file() and Path(self.compiler).is_file()

    def command(self, executable: str, *args: str, seccomp_fd: int | None = None) -> list[str]:
        command = [
            self.bwrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--cap-drop",
            "ALL",
            "--ro-bind",
            "/",
            "/",
            "--tmpfs",
            "/home",
            "--tmpfs",
            "/tmp",
            "--dir",
            "/tmp/work",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--bind",
            "WORKSPACE",
            "/tmp/work",
            "--chdir",
            "/tmp/work",
            "--clearenv",
            "--setenv",
            "PATH",
            "/usr/bin:/bin",
            "--setenv",
            "LC_ALL",
            "C",
            "--setenv",
            "SOURCE_DATE_EPOCH",
            "0",
        ]
        if seccomp_fd is not None:
            command.extend(("--seccomp", str(seccomp_fd)))
        command.extend(("--", executable, *args))
        return command

    def evaluate(
        self,
        source: str,
        tests: Iterable[TestVector],
        *,
        random_seed: int = 1,
        warmups: int = 1,
        runs: int = 5,
        flags: tuple[str, ...] | None = None,
    ) -> EvaluationResult:
        if not self.available:
            raise RuntimeError("sandbox_unavailable")
        if flags is not None and flags not in self.allowed_flags:
            raise ValueError("compiler_flags_not_allowlisted")
        flags = flags or self.fixed_flags
        vectors = tuple(tests)
        if not vectors:
            raise ValueError("acceptance_tests_required")
        dataset_hash = hashlib.sha256(
            json.dumps([asdict(item) for item in vectors], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        source_hash = hashlib.sha256(source.encode()).hexdigest()
        compiler_version = subprocess.run(
            [self.compiler, "--version"], check=True, text=True, capture_output=True, timeout=2
        ).stdout.splitlines()[0]
        with tempfile.TemporaryDirectory(prefix="ralf-eval-") as raw_workspace:
            workspace = Path(raw_workspace)
            source_path = workspace / "candidate.c"
            source_path.write_text(source, encoding="utf-8")
            source_path.chmod(0o400)
            compile_run = self._run(
                workspace,
                self.compiler,
                *flags,
                "candidate.c",
                "-o",
                "candidate",
                timeout=max(10.0, self.limits.wall_seconds),
            )
            compile_log = (compile_run[1] + compile_run[2])[: self.limits.output_bytes]
            if compile_run[0] != 0 or not (workspace / "candidate").exists():
                return self._failed(source_hash, dataset_hash, compiler_version, flags, len(vectors), "compile", compile_log)
            binary = (workspace / "candidate").read_bytes()
            binary_hash = hashlib.sha256(binary).hexdigest()
            passed = 0
            crash = False
            timed_out = False
            deterministic = True
            cpu_total = 0.0
            rss_peak = 0.0
            for vector in vectors:
                first = self._run(workspace, "./candidate", stdin=vector.stdin.encode())
                second = self._run(workspace, "./candidate", stdin=vector.stdin.encode())
                timed_out |= first[4] or second[4]
                crash |= first[0] < 0 or second[0] < 0
                deterministic &= first[1] == second[1]
                cpu_total += first[5]
                rss_peak = max(rss_peak, first[6])
                if first[0] == 0 and first[1].decode("utf-8", "replace") == vector.stdout:
                    passed += 1
            samples: list[float] = []
            benchmark = vectors[0]
            for _ in range(max(0, warmups)):
                self._run(workspace, "./candidate", stdin=benchmark.stdin.encode())
            for _ in range(max(1, runs)):
                item = self._run(workspace, "./candidate", stdin=benchmark.stdin.encode())
                if item[0] == 0 and not item[4]:
                    samples.append(item[3])
            median = statistics.median(samples) if samples else None
            p95 = _percentile(samples, 0.95) if samples else None
            noise = statistics.pstdev(samples) / median if len(samples) > 1 and median else 0.0 if samples else None
            correct = passed == len(vectors) and deterministic and not crash and not timed_out
            return EvaluationResult(
                correct=correct,
                compilation_success=True,
                tests_passed=passed,
                tests_total=len(vectors),
                wall_ms=median,
                cpu_ms=cpu_total,
                rss_mb=rss_peak,
                binary_kb=len(binary) / 1024,
                timeout=timed_out,
                crash=crash,
                sanitizer_errors=False,
                deterministic_output=deterministic,
                compiler=self.compiler,
                compiler_version=compiler_version,
                flags=flags,
                source_hash=source_hash,
                binary_hash=binary_hash,
                dataset_hash=dataset_hash,
                evaluator_version=self.evaluator_version,
                cpu_affinity=tuple(sorted(os.sched_getaffinity(0))),
                random_seed=random_seed,
                run_count=max(1, runs),
                warmup_count=max(0, warmups),
                median_ms=median,
                p95_ms=p95,
                noise=noise,
                failure=None if correct else "acceptance_test_failure",
            )

    def _run(
        self,
        workspace: Path,
        executable: str,
        *args: str,
        stdin: bytes = b"",
        timeout: float | None = None,
    ) -> tuple[int, bytes, bytes, float, bool, float, float]:
        seccomp_fd = self._seccomp_filter(deny_processes=executable == "./candidate")
        sandbox_command = self.command(executable, *args, seccomp_fd=seccomp_fd)
        command = [str(workspace) if item == "WORKSPACE" else item for item in sandbox_command]
        if self._systemd_user_scope_available():
            command = [
                "/usr/bin/systemd-run", "--user", "--scope", "--quiet",
                f"--property=MemoryMax={self.limits.memory_mb}M",
                "--property=CPUQuota=100%",
                f"--property=TasksMax={self.limits.tasks}",
                "--", *command,
            ]
        stdout_path = workspace / ".stdout"
        stderr_path = workspace / ".stderr"
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        started = time.perf_counter_ns()
        timed_out = False
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=stdout,
                stderr=stderr,
                preexec_fn=self._limit_child,
                pass_fds=(seccomp_fd,),
            )
            try:
                process.communicate(stdin, timeout=timeout or self.limits.wall_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                process.communicate()
        wall_ms = (time.perf_counter_ns() - started) / 1_000_000
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        out = stdout_path.read_bytes()[: self.limits.output_bytes + 1]
        err = stderr_path.read_bytes()[: self.limits.output_bytes + 1]
        stdout_path.unlink(missing_ok=True)
        stderr_path.unlink(missing_ok=True)
        if len(out) > self.limits.output_bytes or len(err) > self.limits.output_bytes:
            os.close(seccomp_fd)
            return 120, out[: self.limits.output_bytes], err[: self.limits.output_bytes], wall_ms, timed_out, 0.0, 0.0
        os.close(seccomp_fd)
        cpu_ms = ((after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime)) * 1000
        rss_mb = after.ru_maxrss / 1024
        return process.returncode, out, err, wall_ms, timed_out, cpu_ms, rss_mb

    def _systemd_user_scope_available(self) -> bool:
        """Use the optional cgroup envelope only when its user bus is usable.

        Bubblewrap, seccomp and hard rlimits remain mandatory below.  Some
        non-login runners expose a stale or unusable user bus socket; invoking
        systemd-run there prevents bwrap from starting at all.
        """
        if self._user_systemd_available is None:
            try:
                probe = subprocess.run(
                    ["/usr/bin/systemctl", "--user", "show-environment"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                self._user_systemd_available = False
            else:
                self._user_systemd_available = probe.returncode == 0
        return self._user_systemd_available

    def _limit_child(self) -> None:
        limits = self.limits
        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS, (limits.memory_mb * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_FSIZE, (limits.file_bytes,) * 2)
        process_budget = self._current_user_tasks() + limits.tasks + 8
        resource.setrlimit(resource.RLIMIT_NPROC, (process_budget,) * 2)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        libc = ctypes.CDLL(None)
        if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
            os._exit(126)

    @staticmethod
    def _seccomp_filter(*, deny_processes: bool) -> int:
        if os.uname().machine != "x86_64":
            raise RuntimeError("seccomp_arch_unsupported")
        # Classic BPF over struct seccomp_data. Default allow; explicit EPERM
        # for network, privilege, kernel attack surface and (at runtime) fork.
        denied = [41, 42, 43, 44, 45, 46, 47, 49, 50, 101, 155, 165, 166, 246, 248, 249, 250, 298, 304, 321, 323]
        if deny_processes:
            denied.extend((56, 57, 58, 435))  # clone, fork, vfork, clone3
        instructions = [
            (0x20, 0, 0, 4),                 # LD arch
            (0x15, 1, 0, 0xC000003E),        # x86_64 or kill
            (0x06, 0, 0, 0x80000000),        # KILL_PROCESS
            (0x20, 0, 0, 0),                 # LD syscall nr
        ]
        for syscall in sorted(set(denied)):
            instructions.extend(((0x15, 0, 1, syscall), (0x06, 0, 0, 0x00050001)))
        instructions.append((0x06, 0, 0, 0x7FFF0000))  # ALLOW
        payload = b"".join(struct.pack("HBBI", *instruction) for instruction in instructions)
        fd = os.memfd_create("ralf-seccomp", os.MFD_CLOEXEC)
        os.write(fd, payload)
        os.lseek(fd, 0, os.SEEK_SET)
        return fd

    @staticmethod
    def _current_user_tasks() -> int:
        uid = os.getuid()
        count = 0
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid == uid:
                    count += sum(1 for task in (entry / "task").iterdir() if task.name.isdigit())
            except (FileNotFoundError, PermissionError):
                continue
        return count

    def _failed(
        self,
        source_hash: str,
        dataset_hash: str,
        compiler_version: str,
        flags: tuple[str, ...],
        tests_total: int,
        failure: str,
        detail: bytes,
    ) -> EvaluationResult:
        return EvaluationResult(
            False, False, 0, tests_total, None, None, None, None, False, False, False, False,
            self.compiler, compiler_version, flags, source_hash, None, dataset_hash, self.evaluator_version,
            tuple(sorted(os.sched_getaffinity(0))), 1, 0, 0, None, None, None,
            f"{failure}:{detail.decode('utf-8', 'replace')[:256]}",
        )


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
