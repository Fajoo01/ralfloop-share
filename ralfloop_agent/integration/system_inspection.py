from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import subprocess
from uuid import uuid4

from src.models import Evidence


READ_ONLY_EXECUTABLES = frozenset(
    {
        "df",
        "du",
        "find",
        "stat",
        "ls",
        "journalctl",
        "docker",
        "ps",
        "pgrep",
        "free",
        "uptime",
        "nvidia-smi",
        "sort",
        "wc",
        "head",
        "tail",
        "grep",
    }
)
FILTER_EXECUTABLES = frozenset({"sort", "wc", "head", "tail", "grep"})
SHELL_META_TOKENS = frozenset({"|", "||", "&", "&&", ";", ">", ">>", "<", "<<", "\x60"})
SHELL_META_CHARACTERS = frozenset("|;&><\x60$")
FIND_WRITE_ACTIONS = frozenset(
    {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}
)
MAX_OUTPUT_CHARS = 64_000


@dataclass(frozen=True)
class CommandValidation:
    allowed: bool
    reason: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class InspectionResult:
    result_id: str
    evidence: Evidence


def _argv(command: str | Sequence[str]) -> tuple[str, ...]:
    if isinstance(command, str):
        if any(character in command for character in ("\n", "\r", "\x00")):
            return ()
        try:
            return tuple(shlex.split(command))
        except ValueError:
            return ()
    return tuple(str(part) for part in command)


def validate_read_only_command(command: str | Sequence[str]) -> CommandValidation:
    if isinstance(command, str) and any(character in SHELL_META_CHARACTERS for character in command):
        return CommandValidation(False, "shell_control_operator_forbidden", ())
    argv = _argv(command)
    if not argv:
        return CommandValidation(False, "empty_or_invalid_command", argv)
    if any(token in SHELL_META_TOKENS for token in argv):
        return CommandValidation(False, "shell_control_operator_forbidden", argv)
    if any(
        any(marker in token for marker in ("$(", "${", "`", "\x00"))
        for token in argv
    ):
        return CommandValidation(False, "command_substitution_forbidden", argv)
    executable = argv[0]
    if "/" in executable or executable not in READ_ONLY_EXECUTABLES:
        return CommandValidation(False, "executable_not_allowlisted", argv)

    arguments = argv[1:]
    lowered = tuple(argument.casefold() for argument in arguments)
    if executable == "find":
        if any(
            argument == action or argument.startswith(f"{action}=")
            for argument in lowered
            for action in FIND_WRITE_ACTIONS
        ):
            return CommandValidation(False, "find_write_action_forbidden", argv)
    elif executable == "journalctl":
        allowed = {"--disk-usage", "--no-pager", "--quiet"}
        if "--disk-usage" not in lowered or any(argument not in allowed for argument in lowered):
            return CommandValidation(False, "journalctl_only_disk_usage_allowed", argv)
    elif executable == "docker":
        if len(lowered) < 2 or lowered[:2] != ("system", "df"):
            return CommandValidation(False, "docker_only_system_df_allowed", argv)
        allowed_tail = {"-v", "--verbose"}
        if any(
            argument not in allowed_tail and not argument.startswith("--format=")
            for argument in lowered[2:]
        ):
            return CommandValidation(False, "docker_system_df_option_forbidden", argv)
    elif executable == "nvidia-smi":
        allow_value = False
        for argument in arguments:
            if allow_value:
                allow_value = False
                continue
            if argument in {"-l", "--loop", "-lms", "--loop-ms"}:
                return CommandValidation(False, "nvidia_smi_loop_forbidden", argv)
            if argument in {"-i", "--id"}:
                allow_value = True
                continue
            if argument in {"-L", "--list-gpus", "-q", "--query", "-x", "--xml-format", "-h", "--help"}:
                continue
            if argument.casefold().startswith(("--id=", "--query-", "--format=")):
                continue
            return CommandValidation(False, "nvidia_smi_option_forbidden", argv)
        if allow_value:
            return CommandValidation(False, "nvidia_smi_missing_id", argv)
    elif executable == "sort":
        if any(not argument.startswith("-") for argument in arguments):
            return CommandValidation(False, "filter_file_operand_forbidden", argv)
        if any(argument in {"-o", "--output"} or argument.startswith("--output=") for argument in lowered):
            return CommandValidation(False, "filter_output_file_forbidden", argv)
    elif executable == "wc":
        if any(not argument.startswith("-") for argument in arguments):
            return CommandValidation(False, "filter_file_operand_forbidden", argv)
    elif executable in {"head", "tail"}:
        if any(not argument.startswith("-") and not argument.isdigit() for argument in arguments):
            return CommandValidation(False, "filter_file_operand_forbidden", argv)
    elif executable == "grep":
        positional = [argument for argument in arguments if not argument.startswith("-")]
        if len(positional) > 1:
            return CommandValidation(False, "filter_file_operand_forbidden", argv)
    return CommandValidation(True, "read_only_allowlist", argv)


class ReadOnlySystemExecutor:
    def __init__(self, root: str | Path, *, timeout_sec: int = 60) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError("inspection_root_not_directory")
        self.timeout_sec = timeout_sec

    def run(self, command: str | Sequence[str]) -> Evidence:
        validation = validate_read_only_command(command)
        command_text = shlex.join(validation.argv)
        if not validation.allowed:
            return Evidence(
                command=command_text,
                path=str(self.root),
                exit_code=126,
                stdout="",
                stderr=validation.reason,
            )
        try:
            completed = subprocess.run(
                list(validation.argv),
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
                shell=False,
                check=False,
                env={**os.environ, "LC_ALL": "C"},
            )
            return Evidence(
                command=command_text,
                path=str(self.root),
                exit_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        except subprocess.TimeoutExpired as exc:
            return Evidence(
                command=command_text,
                path=str(self.root),
                exit_code=-1,
                stdout=exc.stdout if isinstance(exc.stdout, str) else "",
                stderr=f"timeout_after_{self.timeout_sec}s",
            )
        except OSError as exc:
            return Evidence(
                command=command_text,
                path=str(self.root),
                exit_code=127,
                stdout="",
                stderr=f"{type(exc).__name__}:{exc}",
            )


def resolve_inspection_root(
    extra_context: dict | None,
    *,
    allowed_root: str | Path | None = None,
) -> Path:
    safe_root = Path(
        allowed_root
        or os.getenv("RALF_READONLY_INSPECTION_ROOT")
        or Path.home()
    ).resolve()
    terminal = dict((extra_context or {}).get("terminal_client") or {})
    requested = Path(str(terminal.get("cwd") or safe_root)).resolve()
    try:
        requested.relative_to(safe_root)
    except ValueError:
        return safe_root
    return requested if requested.is_dir() else safe_root


def default_inspection_plan(root: str | Path) -> tuple[tuple[str, ...], ...]:
    target = str(Path(root).resolve())
    return (
        ("df", "-h", target),
        ("du", "-x", "-h", "--max-depth=1", target),
        ("find", target, "-xdev", "-type", "f", "-size", "+1G", "-printf", "%s\t%p\n"),
    )


def execute_inspection_plan(
    executor: ReadOnlySystemExecutor,
    commands: Sequence[Sequence[str]],
) -> list[InspectionResult]:
    results: list[InspectionResult] = []
    for command in commands:
        validation = validate_read_only_command(command)
        if not validation.allowed:
            evidence = Evidence(
                command=shlex.join(validation.argv),
                path=str(executor.root),
                exit_code=126,
                stdout="",
                stderr=validation.reason,
            )
        else:
            evidence = executor.run(validation.argv)
        results.append(InspectionResult(result_id=uuid4().hex, evidence=evidence))
    return results


def format_inspection_answer(results: Sequence[InspectionResult]) -> str:
    rows = ["Read-only system inspection: real command evidence follows."]
    for result in results:
        evidence = result.evidence
        rows.extend(
            (
                "",
                f"result_id={result.result_id}",
                f"command={evidence.command}",
                f"path={evidence.path}",
                f"exit_code={evidence.exit_code}",
                "stdout:",
                (evidence.stdout or "")[:MAX_OUTPUT_CHARS],
                "stderr:",
                (evidence.stderr or "")[:MAX_OUTPUT_CHARS],
            )
        )
    rows.extend(("", "risk=inspection_only", "recoverable_space=not_estimated_without_cleanup_approval"))
    return "\n".join(rows)
