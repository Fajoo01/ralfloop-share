from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import re
import subprocess


FORBIDDEN_PARTS = frozenset({".git", ".ralf_run", "checkpoints", "models", "credentials", "secrets"})
FORBIDDEN_NAMES = frozenset(
    {
        "main_plugin.py",
        "domain_approval_executor.py",
        "telegram_approval_api.py",
        ".env",
        "id_rsa",
        "id_ed25519",
    }
)
ALLOWED_SUFFIXES = frozenset({".py", ".json", ".toml", ".yaml", ".yml", ".md", ".txt"})
SECRET_RE = re.compile(r"(?i)(?:password|api[_-]?key|private[_-]?key|secret|token)\s*[:=]\s*['\"][^'\"]+")
BYPASS_MARKERS = (
    "human_confirmed=true",
    "auto_execute_protected_actions=true",
    "bypass_approval",
    "disable_approval",
    "skip_confirmation",
)


@dataclass(frozen=True)
class PatchValidation:
    ok: bool
    files: tuple[str, ...] = ()
    changed_lines: int = 0
    errors: tuple[str, ...] = ()


@dataclass
class PatchValidator:
    max_files: int = 5
    max_changed_lines: int = 500
    allowed_paths: tuple[str, ...] = ()
    allowed_suffixes: frozenset[str] = field(default_factory=lambda: ALLOWED_SUFFIXES)

    def _paths(self, patch: str) -> tuple[str, ...]:
        paths = []
        for match in re.finditer(r"^diff --git a/(.+?) b/(.+?)$", patch, flags=re.MULTILINE):
            before, after = match.groups()
            if before != after:
                paths.extend((before, after))
            else:
                paths.append(after)
        return tuple(dict.fromkeys(paths))

    def validate(self, patch: str, worktree: str | Path) -> PatchValidation:
        root = Path(worktree).resolve()
        errors: list[str] = []
        if not patch.strip().startswith("diff --git "):
            errors.append("unified_diff_required")
        if "GIT binary patch" in patch or "Binary files " in patch:
            errors.append("binary_patch_forbidden")
        if "new file mode 120000" in patch or "new mode 120000" in patch:
            errors.append("symlink_patch_forbidden")
        paths = self._paths(patch)
        if not paths:
            errors.append("no_patch_paths")
        if len(paths) > self.max_files:
            errors.append("max_files_exceeded")
        for raw in paths:
            path = PurePosixPath(raw)
            if path.is_absolute() or ".." in path.parts:
                errors.append(f"path_outside_worktree:{raw}")
                continue
            if any(part.casefold() in FORBIDDEN_PARTS for part in path.parts):
                errors.append(f"forbidden_path:{raw}")
            if path.name.casefold() in FORBIDDEN_NAMES:
                errors.append(f"forbidden_file:{raw}")
            lowered = raw.casefold()
            if any(marker in lowered for marker in ("telegram", "recursive_mas", "h1b", "h2", "checkpoint")):
                errors.append(f"protected_component:{raw}")
            if path.suffix.casefold() not in self.allowed_suffixes:
                errors.append(f"extension_forbidden:{raw}")
            if self.allowed_paths and not any(
                raw == allowed or raw.startswith(f"{allowed.rstrip('/')}/")
                for allowed in self.allowed_paths
            ):
                errors.append(f"path_not_allowlisted:{raw}")
            candidate = root / path
            if candidate.is_symlink():
                errors.append(f"symlink_target_forbidden:{raw}")
            target = candidate.resolve()
            try:
                target.relative_to(root)
            except ValueError:
                errors.append(f"path_outside_worktree:{raw}")
        changed_lines = sum(
            1
            for line in patch.splitlines()
            if (line.startswith("+") and not line.startswith("+++"))
            or (line.startswith("-") and not line.startswith("---"))
        )
        if changed_lines > self.max_changed_lines:
            errors.append("max_changed_lines_exceeded")
        if SECRET_RE.search(patch):
            errors.append("possible_secret_in_patch")
        folded = patch.casefold().replace(" ", "")
        if any(marker in folded for marker in BYPASS_MARKERS):
            errors.append("approval_bypass_forbidden")
        if not errors:
            completed = subprocess.run(
                ["git", "apply", "--check", "-"],
                input=patch,
                capture_output=True,
                text=True,
                cwd=root,
                shell=False,
                check=False,
                timeout=20,
            )
            if completed.returncode != 0:
                errors.append(f"patch_not_applicable:{completed.stderr[:200]}")
        return PatchValidation(not errors, paths, changed_lines, tuple(errors))


__all__ = ["PatchValidation", "PatchValidator"]
