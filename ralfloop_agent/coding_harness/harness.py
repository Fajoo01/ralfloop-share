from __future__ import annotations

from dataclasses import asdict, dataclass
import difflib
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess
from typing import Any, Mapping

from ralfloop_agent.shell_judge import (
    RalfShellJudge, ShellDecision, ShellPolicy, safe_execution_environment,
)

from .judge import ResidentDs4CodingJudge


PI = os.environ.get("RALF_PI_BIN", "/home/sibilla-cumana/.local/node_modules/.bin/pi")
MAX_TEXT_BYTES = 512 * 1024
MAX_PACKET_DIFF = 24_000
MAX_OUTPUT = 16_000


@dataclass(frozen=True)
class SnapshotEntry:
    sha256: str
    size: int
    text: str | None


@dataclass(frozen=True)
class CodingRiskAssessment:
    level: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class HarnessConfig:
    workdir: Path
    task: str
    validator_command: str
    worker_timeout_sec: int = 120
    validator_timeout_sec: int = 180
    worker_user: str = "sibilla-cumana"
    provider: str = "agentcpm-local"
    model: str = "AgentCPM-Explore"
    fallback_provider: str | None = None
    fallback_model: str | None = None
    allow_test_changes: bool = False
    protected_globs: tuple[str, ...] = ()
    ds4_base_url: str = "http://127.0.0.1:19194"
    ds4_timeout_sec: float = 900.0


def _trim(value: str, limit: int = MAX_OUTPUT) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[truncated]"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _candidate_paths(root: Path) -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        raw = [item for item in result.stdout.split(b"\0") if item]
        return sorted(root / os.fsdecode(item) for item in raw)
    except (OSError, subprocess.CalledProcessError):
        return sorted(
            p for p in root.rglob("*")
            if p.is_file() and ".git" not in p.parts
        )


def snapshot(root: Path) -> dict[str, SnapshotEntry]:
    result: dict[str, SnapshotEntry] = {}
    for path in _candidate_paths(root):
        try:
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            size = path.stat().st_size
            text = None
            if size <= MAX_TEXT_BYTES:
                try:
                    text = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    text = None
            result[rel] = SnapshotEntry(
                sha256=_sha256(path),
                size=size,
                text=text,
            )
        except (FileNotFoundError, PermissionError, OSError):
            continue
    return result


def changed_paths(
    before: Mapping[str, SnapshotEntry],
    after: Mapping[str, SnapshotEntry],
) -> list[str]:
    names = sorted(set(before) | set(after))
    return [
        name for name in names
        if before.get(name) != after.get(name)
    ]


def build_diff(
    before: Mapping[str, SnapshotEntry],
    after: Mapping[str, SnapshotEntry],
    paths: list[str],
) -> tuple[str, int, bool]:
    chunks: list[str] = []
    changed_lines = 0

    for name in paths:
        old = before.get(name)
        new = after.get(name)

        old_text = old.text if old else ""
        new_text = new.text if new else ""

        if (old and old.text is None) or (new and new.text is None):
            chunks.append(
                f"--- a/{name}\n+++ b/{name}\n"
                f"@@ binary-or-large-file old={old.sha256 if old else None} "
                f"new={new.sha256 if new else None} @@\n"
            )
            changed_lines += 1
            continue

        diff = list(difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
            lineterm="",
            n=3,
        ))
        changed_lines += sum(
            line.startswith("+") or line.startswith("-")
            for line in diff
            if not line.startswith("+++") and not line.startswith("---")
        )
        if diff:
            chunks.append("\n".join(diff) + "\n")

    value = "\n".join(chunks)
    truncated = len(value) > MAX_PACKET_DIFF
    if truncated:
        value = value[:MAX_PACKET_DIFF] + "\n...[diff truncated]"
    return value, changed_lines, truncated


def _is_test_path(path: str) -> bool:
    p = Path(path)
    return (
        "tests" in p.parts
        or p.name.startswith("test_")
        or p.name.endswith("_test.py")
    )


def protected_changes(config: HarnessConfig, paths: list[str]) -> list[str]:
    result: list[str] = []

    for path in paths:
        if not config.allow_test_changes and _is_test_path(path):
            result.append(path)
            continue
        if any(fnmatch.fnmatch(path, pattern) for pattern in config.protected_globs):
            result.append(path)

    return sorted(set(result))


def assess_coding_risk(
    *,
    worker_rc: int,
    validator_rc: int,
    changed_files: list[str],
    changed_lines: int,
    protected: list[str],
    diff_truncated: bool,
) -> CodingRiskAssessment:
    high: list[str] = []
    normal: list[str] = []

    if worker_rc != 0:
        high.append(f"worker_nonzero:{worker_rc}")
    if validator_rc != 0:
        high.append(f"validator_nonzero:{validator_rc}")
    if protected:
        high.append("protected_files_changed")
    if diff_truncated:
        high.append("diff_truncated")
    if len(changed_files) > 8:
        high.append("broad_file_scope")
    if changed_lines > 400:
        high.append("broad_diff")

    if high:
        return CodingRiskAssessment("high", tuple(high))

    if not changed_files:
        normal.append("no_files_changed")
    if len(changed_files) > 4:
        normal.append("moderate_file_scope")
    if changed_lines > 200:
        normal.append("moderate_diff")

    if normal:
        return CodingRiskAssessment("normal", tuple(normal))

    return CodingRiskAssessment("low", ("deterministic_green_small_scope",))


def _run_shell(command: str, root: Path, timeout_sec: int) -> tuple[int, str]:
    environment = safe_execution_environment(os.environ)
    review = RalfShellJudge(ShellPolicy.for_sandbox(root)).review(
        command, str(root), environment,
    )
    if review.decision not in {ShellDecision.ALLOW, ShellDecision.ALLOW_READONLY}:
        return 126, f"SHELL_JUDGE_{review.decision.value}:{review.deterministic_reason}"
    try:
        result = subprocess.run(
            ["/bin/bash", "--noprofile", "--norc", "-c", command],
            cwd=root,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_sec,
            env=environment,
        )
        return result.returncode, _trim(result.stdout or "")
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return 124, _trim(output + "\nVALIDATOR_TIMEOUT")


def _pi_command(
    config: HarnessConfig,
    prompt: str,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> list[str]:
    current = pwd.getpwuid(os.geteuid()).pw_name
    selected_provider = provider or config.provider
    selected_model = model or config.model

    command: list[str] = [
        "/usr/bin/timeout",
        "--signal=INT",
        "--kill-after=10s",
        str(config.worker_timeout_sec),
    ]

    if current != config.worker_user:
        command += ["sudo", "-u", config.worker_user, "-H"]

    command += [
        PI,
        "--no-session",
        "--provider", selected_provider,
        "--model", selected_model,
        "--tools", "read,edit,write,bash",
        "--mode", "json",
        "-p", prompt,
    ]
    return command


def _run_worker_once(
    config: HarnessConfig,
    prompt: str,
    *,
    provider: str,
    model: str,
) -> tuple[int, str]:
    try:
        result = subprocess.run(
            _pi_command(config, prompt, provider=provider, model=model),
            cwd=config.workdir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=config.worker_timeout_sec + 20,
        )
        return result.returncode, _trim(result.stdout or "")
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return 124, _trim(output + "\nWORKER_OUTER_TIMEOUT")


def _run_worker(config: HarnessConfig, prompt: str) -> tuple[int, str]:
    primary_rc, primary_output = _run_worker_once(
        config, prompt, provider=config.provider, model=config.model
    )
    fallback_provider = (config.fallback_provider or "").strip()
    fallback_model = (config.fallback_model or "").strip()
    if primary_rc == 0 or not fallback_provider or not fallback_model:
        return primary_rc, primary_output
    if (fallback_provider, fallback_model) == (config.provider, config.model):
        return primary_rc, primary_output

    fallback_rc, fallback_output = _run_worker_once(
        config, prompt, provider=fallback_provider, model=fallback_model
    )
    combined = _trim(
        "PRIMARY_WORKER_FAILED "
        f"provider={config.provider} model={config.model} rc={primary_rc}\n"
        f"{primary_output}\n"
        "FALLBACK_WORKER "
        f"provider={fallback_provider} model={fallback_model} rc={fallback_rc}\n"
        f"{fallback_output}"
    )
    return fallback_rc, combined


def _worker_prompt(config: HarnessConfig) -> str:
    test_rule = (
        "Do not modify test files."
        if not config.allow_test_changes
        else "Test files may be changed only when the task genuinely requires it."
    )

    return "\n".join((
        "You are the coding worker.",
        f"TASK: {config.task}",
        test_rule,
        "Inspect the repository before changing it.",
        "Make the smallest correct implementation change.",
        f"Validation command: {config.validator_command}",
        "Run the validation command after the change.",
        "Do not broaden scope unnecessarily.",
        "Stop after the task is implemented and validation has been attempted.",
    ))


def _repair_prompt(
    config: HarnessConfig,
    review: Mapping[str, Any],
    validator_output: str,
) -> str:
    return "\n".join((
        "You are performing exactly one bounded repair pass.",
        f"ORIGINAL TASK: {config.task}",
        "The final judge found the following issue:",
        json.dumps(review, ensure_ascii=False),
        "Deterministic validator output:",
        _trim(validator_output, 6000),
        (
            "Do not modify test files."
            if not config.allow_test_changes
            else "Do not change tests unless essential to the original task."
        ),
        "Apply only the minimal repair required.",
        f"Run validation once: {config.validator_command}",
        "Stop after that. Do not perform unrelated cleanup.",
    ))


def _packet(
    *,
    config: HarnessConfig,
    worker_rc: int,
    validator_rc: int,
    validator_output: str,
    changed: list[str],
    diff: str,
    risk: CodingRiskAssessment,
    protected: list[str],
) -> dict[str, Any]:
    return {
        "task": config.task,
        "changed_files": changed,
        "diff": diff,
        "validator": {
            "command": config.validator_command,
            "return_code": validator_rc,
            "status": "green" if validator_rc == 0 else "red",
            "output": _trim(validator_output, 8000),
        },
        "worker_rc": worker_rc,
        "risk": asdict(risk),
        "protected_files_changed": protected,
    }


def run_harness(
    config: HarnessConfig,
    *,
    judge: ResidentDs4CodingJudge | None = None,
) -> dict[str, Any]:
    root = config.workdir.resolve()
    if not root.is_dir():
        raise ValueError("coding_workdir_missing")
    if not config.task.strip():
        raise ValueError("coding_task_empty")
    if not config.validator_command.strip():
        raise ValueError("coding_validator_empty")

    before = snapshot(root)

    worker_rc, worker_output = _run_worker(config, _worker_prompt(config))

    after_worker = snapshot(root)
    changed = changed_paths(before, after_worker)
    diff, lines, truncated = build_diff(before, after_worker, changed)
    protected = protected_changes(config, changed)

    validator_rc, validator_output = _run_shell(
        config.validator_command,
        root,
        config.validator_timeout_sec,
    )

    risk = assess_coding_risk(
        worker_rc=worker_rc,
        validator_rc=validator_rc,
        changed_files=changed,
        changed_lines=lines,
        protected=protected,
        diff_truncated=truncated,
    )

    report: dict[str, Any] = {
        "schema_version": "pi_coding_harness_v1",
        "task": config.task,
        "workdir": str(root),
        "worker": {
            "return_code": worker_rc,
            "output": worker_output,
        },
        "initial_validation": {
            "return_code": validator_rc,
            "output": validator_output,
        },
        "changed_files": changed,
        "changed_lines": lines,
        "protected_files_changed": protected,
        "risk": asdict(risk),
        "ds4_invoked": False,
        "repair_invoked": False,
    }

    deterministic_green = validator_rc == 0 and not protected

    # Fast path: clean deterministic success, small scope, no DS4.
    if risk.level == "low" and deterministic_green:
        report["final_status"] = "pass"
        report["decision"] = "deterministic_fast_path"
        return report

    packet = _packet(
        config=config,
        worker_rc=worker_rc,
        validator_rc=validator_rc,
        validator_output=validator_output,
        changed=changed,
        diff=diff,
        risk=risk,
        protected=protected,
    )

    judge = judge or ResidentDs4CodingJudge(
        base_url=config.ds4_base_url,
        timeout_sec=config.ds4_timeout_sec,
        max_output_tokens=256,
    )

    report["ds4_invoked"] = True

    try:
        review, metadata = judge.review(packet)
        report["ds4_review"] = review.model_dump(mode="json")
        report["ds4_metadata"] = {
            key: value for key, value in metadata.items()
            if key != "raw"
        }
    except Exception as exc:
        report["final_status"] = "fail_closed"
        report["decision"] = "ds4_error"
        report["ds4_error"] = f"{type(exc).__name__}:{exc}"
        return report

    # DS4 cannot override a deterministic red/protected state.
    if review.verdict == "pass":
        if deterministic_green:
            report["final_status"] = "pass"
            report["decision"] = "ds4_pass"
        else:
            report["final_status"] = "fail_closed"
            report["decision"] = "ds4_pass_but_deterministic_red"
        return report

    # Exactly one repair pass.
    report["repair_invoked"] = True
    repair_rc, repair_output = _run_worker(
        config,
        _repair_prompt(
            config,
            review.model_dump(mode="json"),
            validator_output,
        ),
    )

    after_repair = snapshot(root)
    final_changed = changed_paths(before, after_repair)
    final_diff, final_lines, final_truncated = build_diff(
        before,
        after_repair,
        final_changed,
    )
    final_protected = protected_changes(config, final_changed)

    final_validator_rc, final_validator_output = _run_shell(
        config.validator_command,
        root,
        config.validator_timeout_sec,
    )

    report["repair"] = {
        "return_code": repair_rc,
        "output": repair_output,
    }
    report["final_validation"] = {
        "return_code": final_validator_rc,
        "output": final_validator_output,
    }
    report["final_changed_files"] = final_changed
    report["final_changed_lines"] = final_lines
    report["final_diff_truncated"] = final_truncated
    report["final_protected_files_changed"] = final_protected

    # Worker timeout/nonzero is only a risk signal. The filesystem +
    # deterministic validator decide whether the repair actually succeeded.
    if final_validator_rc == 0 and not final_protected:
        report["final_status"] = "pass"
        report["decision"] = "repair_then_deterministic_pass"
    else:
        report["final_status"] = "fail_closed"
        report["decision"] = "repair_failed_deterministic_validation"

    return report


__all__ = [
    "CodingRiskAssessment",
    "HarnessConfig",
    "assess_coding_risk",
    "build_diff",
    "changed_paths",
    "protected_changes",
    "run_harness",
    "snapshot",
]
