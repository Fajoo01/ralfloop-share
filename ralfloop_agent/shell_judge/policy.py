from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
from typing import Mapping

from .capabilities import ShellParserError, extract_capabilities, parse_bash
from .contracts import DestructiveLevel, ShellCapability, ShellDecision, ShellReviewResult


SAFE_GIT_ARGV = {
    ("git", "status"), ("git", "diff"), ("git", "diff", "--check"),
    ("git", "log"), ("git", "show"),
}
SAFE_READ = {"cat", "grep", "rg", "head", "tail", "wc", "test", "stat", "ls", "find"}
PURE_TRANSFORMS = {"dirname"}
SAFE_WRITE = {"echo", "printf", "mkdir", "touch"}
FORBIDDEN_EXECUTABLES = {
    "chroot", "dd", "mkfs", "mount", "poweroff", "reboot", "shutdown",
    "sudo", "systemctl", "umount",
}
DANGEROUS_ENV = {
    "BASH_ENV", "CDPATH", "ENV", "GIT_EXTERNAL_DIFF", "GIT_PAGER", "LD_LIBRARY_PATH",
    "LD_PRELOAD", "NODE_OPTIONS", "PAGER", "PATH", "PERL5OPT", "PERL5LIB",
    "PYTHONHOME", "PYTHONPATH", "RIPGREP_CONFIG_PATH", "RUBYLIB", "RUBYOPT", "SHELLOPTS",
}

_DANGEROUS_GIT_KEYS = {
    "core.attributesfile", "core.fsmonitor", "core.hookspath", "core.pager",
    "diff.external", "interactive.difffilter",
}
_DANGEROUS_GIT_PREFIXES = (
    "alias.", "include.", "includeif.", "pager.",
)


def safe_execution_environment(environment: Mapping[str, str], *, path_prefix: str = "") -> dict[str, str]:
    env = dict(environment)
    for name in tuple(env):
        if name in DANGEROUS_ENV or name.startswith("GIT_"):
            env.pop(name, None)
    env.update({
        "GIT_PAGER": "cat", "PAGER": "cat", "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    })
    if path_prefix:
        try:
            trusted_prefix = Path(path_prefix).resolve(strict=True)
        except OSError:
            trusted_prefix = None
        if trusted_prefix is not None and trusted_prefix.is_dir():
            env["PATH"] = str(trusted_prefix) + os.pathsep + env["PATH"]
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


def _trusted_command_token(token: str) -> bool:
    if "/" not in token:
        return True
    try:
        resolved = Path(token).resolve(strict=True)
    except OSError:
        return False
    return resolved.parent in {Path("/usr/bin"), Path("/bin")}


def _empty_capability(cwd: str) -> ShellCapability:
    return ShellCapability(executable="", argv=(), cwd=str(Path(cwd).resolve(strict=False)))


def _dangerous_env(name: str) -> bool:
    return name in DANGEROUS_ENV or name.startswith("GIT_")


def _trusted_git() -> str | None:
    candidate = shutil.which("git", path="/usr/bin:/bin")
    if not candidate:
        return None
    resolved = str(Path(candidate).resolve(strict=True))
    return resolved if resolved in {"/usr/bin/git", "/bin/git"} else None


def _git_config_keys(cwd: str) -> tuple[str, ...] | None:
    """Read canonical local key names only; values never enter logs or policy output."""
    git = _trusted_git()
    if git is None:
        return None
    root = Path(cwd).resolve(strict=False)
    if not any((parent / ".git").exists() for parent in (root, *root.parents)):
        return ()
    env = safe_execution_environment({"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    try:
        proc = subprocess.run(
            [git, "-C", cwd, "config", "--null", "--name-only", "--local", "--no-includes", "--list"],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=1.0, check=False, env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or len(proc.stdout) > 1_000_000:
        return None
    try:
        return tuple(part.decode("utf-8", "strict").casefold() for part in proc.stdout.split(b"\0") if part)
    except UnicodeError:
        return None


def _git_indirect_effects(cwd: str) -> bool:
    keys = _git_config_keys(cwd)
    if keys is None:
        return True
    if any(
        key in _DANGEROUS_GIT_KEYS
        or key.startswith(_DANGEROUS_GIT_PREFIXES)
        or (key.startswith("diff.") and key.endswith((".command", ".textconv")))
        or (key.startswith("filter.") and key.endswith((".clean", ".smudge", ".process")))
        or key == "extensions.worktreeconfig"
        for key in keys
    ):
        return True
    cwd_path = Path(cwd).resolve(strict=False)
    repository_root = next((parent for parent in (cwd_path, *cwd_path.parents)
                            if (parent / ".git").exists()), cwd_path)
    attributes_files = list(repository_root.rglob(".gitattributes"))
    if len(attributes_files) > 1024:
        return True
    git_marker = repository_root / ".git"
    if git_marker.is_dir():
        attributes_files.append(git_marker / "info/attributes")
    elif git_marker.is_file() and not git_marker.is_symlink():
        try:
            marker = git_marker.read_text(encoding="utf-8", errors="strict").strip()
            if marker.startswith("gitdir:"):
                git_dir = (repository_root / marker.split(":", 1)[1].strip()).resolve(strict=True)
                attributes_files.append(git_dir / "info/attributes")
        except (OSError, UnicodeError):
            return True
    for attributes in attributes_files:
        if attributes.is_symlink():
            return True
        try:
            if attributes.stat().st_size > 1_000_000:
                return True
            content = attributes.read_text(encoding="utf-8", errors="strict").casefold()
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError):
            return True
        if any(token.startswith(("diff", "filter")) for line in content.splitlines()
               for token in line.split()[1:] if not line.lstrip().startswith("#")):
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
        if any(_dangerous_env(name) for name in cap.env_assignments):
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
            if not _trusted_command_token(argv[0]):
                return self._result(ShellDecision.REVIEW, cap, "untrusted_executable_path", "high")
            if executable == "git":
                normalized = ("git", *argv[1:])
                if normalized not in SAFE_GIT_ARGV:
                    return self._result(ShellDecision.REVIEW, cap, "git_operation_not_allowlisted", "high")
                if _git_indirect_effects(cap.cwd):
                    return self._result(ShellDecision.REVIEW, cap, "git_indirect_effect_configured", "high")
            elif executable == "rg" and any(arg == "--pre" or arg.startswith("--pre=") for arg in argv[1:]):
                return self._result(ShellDecision.REVIEW, cap, "external_preprocessor", "high")
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
