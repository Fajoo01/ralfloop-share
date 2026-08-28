from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
from typing import Mapping

from .capabilities import ShellParserError, extract_capabilities, parse_bash
from .contracts import DestructiveLevel, ShellCapability, ShellDecision, ShellReviewResult


SAFE_GIT_READ = {"status", "diff", "log", "show"}
SAFE_READ = {"cat", "grep", "rg", "head", "tail", "wc", "test", "stat", "ls", "find"}
PURE_TRANSFORMS = {"dirname"}
SAFE_WRITE = {"echo", "printf", "mkdir", "touch"}
FORBIDDEN_EXECUTABLES = {
    "chroot", "dd", "mkfs", "mount", "poweroff", "reboot", "shutdown",
    "sudo", "systemctl", "umount",
}
DANGEROUS_ENV = {
    "BASH_ENV", "ENV", "GIT_EXTERNAL_DIFF", "GIT_PAGER", "LD_LIBRARY_PATH",
    "LD_PRELOAD", "PAGER", "PATH", "PYTHONPATH", "SHELLOPTS",
}


def safe_execution_environment(environment: Mapping[str, str], *, path_prefix: str = "") -> dict[str, str]:
    env = dict(environment)
    for name in ("GIT_EXTERNAL_DIFF", "GIT_DIFF_OPTS", "LD_PRELOAD", "PYTHONPATH"):
        env.pop(name, None)
    env.update({
        "GIT_PAGER": "cat", "PAGER": "cat", "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
    })
    if path_prefix:
        env["PATH"] = path_prefix + os.pathsep + env.get("PATH", "")
    return env


@dataclass(frozen=True)
class ShellPolicy:
    readable_roots: tuple[Path, ...]
    writable_roots: tuple[Path, ...]
    protected_roots: tuple[Path, ...]
    secret_roots: tuple[Path, ...]

    @classmethod
    def for_sandbox(cls, root: str | Path) -> "ShellPolicy":
        sandbox = Path(root).resolve(strict=True)
        protected = tuple(Path(value).resolve(strict=False) for value in (
            "/boot", "/etc", "/home/sibilla-cumana/ralfloop-production/current",
            "/home/sibilla-cumana/ralfloop-production/releases", "/usr", "/var/lib",
        ))
        secrets = tuple(Path(value).resolve(strict=False) for value in (
            "/home/bandi/.config", "/home/sibilla-cumana/.config", "/run/credentials",
        ))
        return cls((sandbox,), (sandbox,), protected, secrets)


def _within(path: str, roots: tuple[Path, ...]) -> bool:
    target = Path(path)
    return any(target == root or root in target.parents for root in roots)


def _empty_capability(cwd: str) -> ShellCapability:
    return ShellCapability(executable="", argv=(), cwd=str(Path(cwd).resolve(strict=False)))


def _git_indirect_effects(cwd: str) -> bool:
    root = Path(cwd)
    candidates = (root / ".git/config", root / ".gitattributes")
    for candidate in candidates:
        try:
            content = candidate.read_text(encoding="utf-8", errors="replace").casefold()
        except (OSError, UnicodeError):
            continue
        if any(marker in content for marker in ("diff.external", "textconv", "filter=")):
            return True
    return False


class RalfShellJudge:
    def __init__(self, policy: ShellPolicy, *, parser: Path | None = None) -> None:
        self.policy = policy
        self.parser = parser

    def review(self, command: str, cwd: str, environment: Mapping[str, str] | None = None) -> ShellReviewResult:
        try:
            ast = parse_bash(command, binary=self.parser)
            capability = extract_capabilities(ast, cwd=cwd)
        except (ShellParserError, OSError, ValueError):
            return ShellReviewResult(
                ShellDecision.DENY, _empty_capability(cwd), "parser_failure",
                reviewer_required=False, risk="too_destructive", correctness="invalid",
            )
        return self._decide(capability, environment or {})

    def _decide(self, cap: ShellCapability, environment: Mapping[str, str]) -> ShellReviewResult:
        all_targets = cap.resolved_paths_read + cap.resolved_paths_write + cap.resolved_paths_delete
        if any(_within(path, self.policy.secret_roots) for path in all_targets):
            return self._result(ShellDecision.DENY, cap, "secret_path", "too_destructive")
        if any(_within(path, self.policy.protected_roots) for path in cap.resolved_paths_write + cap.resolved_paths_delete):
            return self._result(ShellDecision.DENY, cap, "protected_path_write", "too_destructive")
        if cap.resolved_paths_delete and any(path in {"/", "/home", "/home/sibilla-cumana"} or "ralfloop-production" in path for path in cap.resolved_paths_delete):
            return self._result(
                ShellDecision.DENY,
                replace(cap, destructive_level=DestructiveLevel.TOO_DESTRUCTIVE),
                "too_destructive_scope", "too_destructive",
            )
        if "dynamic_code_execution" in cap.unresolved_elements:
            return self._result(ShellDecision.DENY, cap, "dynamic_code_execution", "too_destructive")
        if any(Path(argv[0]).name in FORBIDDEN_EXECUTABLES for argv in cap.commands):
            return self._result(ShellDecision.DENY, cap, "forbidden_executable", "too_destructive")
        if any(
            Path(argv[0]).name == "find"
            and any(arg in {"-delete", "-exec", "-execdir", "-ok", "-okdir"} for arg in argv[1:])
            for argv in cap.commands
        ):
            return self._result(ShellDecision.DENY, cap, "find_mutation_forbidden", "too_destructive")
        if cap.background_execution:
            return self._result(ShellDecision.REVIEW, cap, "background_execution", "high")
        if set(cap.env_assignments) & DANGEROUS_ENV:
            return self._result(ShellDecision.REVIEW, cap, "dangerous_environment_assignment", "high")
        if cap.unresolved_elements:
            return self._result(ShellDecision.REVIEW, cap, "unresolved_capability", "high")
        if cap.network_or_external_effects:
            return self._result(ShellDecision.REVIEW, cap, "external_effect", "high")
        if cap.resolved_paths_delete:
            return self._result(ShellDecision.REVIEW, cap, "filesystem_delete", "high")
        if any(not _within(path, self.policy.readable_roots) for path in cap.resolved_paths_read):
            return self._result(ShellDecision.REVIEW, cap, "read_outside_allowlist", "high")
        if any(not _within(path, self.policy.writable_roots) for path in cap.resolved_paths_write):
            return self._result(ShellDecision.DENY, cap, "write_outside_allowlist", "too_destructive")
        commands = cap.commands
        if not commands:
            return self._result(ShellDecision.REVIEW, cap, "no_executable", "high")
        if any(Path(argv[0]).name in {"bash", "sh", "zsh"} for argv in commands):
            return self._result(ShellDecision.REVIEW, cap, "nested_shell", "high")
        for argv in commands:
            executable = Path(argv[0]).name
            if executable == "git":
                if len(argv) < 2 or argv[1] not in SAFE_GIT_READ:
                    return self._result(ShellDecision.REVIEW, cap, "git_operation_not_allowlisted", "high")
                if _git_indirect_effects(cap.cwd):
                    return self._result(ShellDecision.REVIEW, cap, "git_indirect_effect_configured", "high")
            elif executable not in SAFE_READ | SAFE_WRITE | PURE_TRANSFORMS:
                return self._result(ShellDecision.REVIEW, cap, "unknown_command", "high")
        if cap.resolved_paths_write:
            if all(Path(argv[0]).name in SAFE_WRITE | SAFE_READ | PURE_TRANSFORMS for argv in commands):
                return self._result(ShellDecision.ALLOW, cap, "allowlisted_sandbox_write", "low")
            return self._result(ShellDecision.REVIEW, cap, "write_command_not_allowlisted", "high")
        return self._result(ShellDecision.ALLOW_READONLY, cap, "allowlisted_read_only", "low")

    @staticmethod
    def _result(decision: ShellDecision, cap: ShellCapability, reason: str, risk: str) -> ShellReviewResult:
        return ShellReviewResult(
            decision, cap, reason, reviewer_required=decision is ShellDecision.REVIEW,
            risk=risk, authorization="neutral", correctness="unknown",
        )

    @staticmethod
    def command_digest(command: str) -> str:
        return hashlib.sha256(command.encode()).hexdigest()
