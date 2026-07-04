from __future__ import annotations

from pathlib import Path
from collections.abc import Sequence
import logging
import os
import re
import shlex
import shutil
import subprocess
import time

from src.models import Evidence

logger = logging.getLogger(__name__)
SANDBOX_PATH = Path("/tmp/ralf_sandbox")


class ShellExecutor:
    def __init__(
        self,
        base_dir: str | Path | None = None,
        timeout_sec: int = 30,
        task_id: str | None = None,
    ) -> None:
        configured_base = base_dir or os.getenv("RALF_SANDBOX_PATH") or SANDBOX_PATH
        self.root_dir = Path(configured_base).resolve()
        self.task_id = self._sanitize_task_id(task_id)
        self.base_dir = (self.root_dir / self.task_id).resolve() if self.task_id else self.root_dir
        try:
            self.base_dir.relative_to(self.root_dir)
        except ValueError as exc:
            raise ValueError(f"task sandbox escapes base sandbox: {task_id}") from exc
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.timeout_sec = timeout_sec

    def run(self, command: str | Sequence[str], cwd: str | None = None) -> Evidence:
        argv = self._command_to_argv(command)
        return self.run_in_sandbox(argv, cwd=cwd)

    def run_in_sandbox(self, command: Sequence[str], cwd: str | None = None) -> Evidence:
        safe_cwd = self._safe_cwd(cwd)
        argv = [str(part) for part in command]
        command_text = shlex.join(argv)
        logger.info("shell_run command=%r cwd=%s task_id=%s", command_text, safe_cwd, self.task_id)
        try:
            if not argv:
                return Evidence(command="", path=str(safe_cwd), exit_code=2, stderr="empty command")
            completed = subprocess.run(
                argv,
                cwd=safe_cwd,
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
                shell=False,
                check=False,
            )
            return Evidence(
                command=command_text,
                path=str(safe_cwd),
                exit_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        except subprocess.TimeoutExpired as exc:
            return Evidence(
                command=command_text,
                path=str(safe_cwd),
                exit_code=-1,
                stdout=exc.stdout if isinstance(exc.stdout, str) else None,
                stderr=f"Timeout after {self.timeout_sec}s: {exc.stderr or ''}",
            )
        except Exception as exc:
            return Evidence(command=command_text, path=str(safe_cwd), exit_code=1, stderr=str(exc))

    def _safe_cwd(self, cwd: str | None) -> Path:
        if cwd is None:
            return self.base_dir
        raw = Path(cwd)
        candidate = (self.base_dir / raw).resolve() if not raw.is_absolute() else raw.resolve()
        try:
            candidate.relative_to(self.base_dir)
        except ValueError as exc:
            raise ValueError(f"cwd escapes sandbox: {cwd}") from exc
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate

    @staticmethod
    def _sanitize_task_id(task_id: str | None) -> str | None:
        if task_id is None:
            return None
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(task_id).strip()).strip("._-")
        if not safe:
            raise ValueError("task_id is empty after sanitization")
        return safe[:96]

    @staticmethod
    def _command_to_argv(command: str | Sequence[str]) -> list[str]:
        if isinstance(command, str):
            return shlex.split(command)
        return [str(part) for part in command]


def cleanup_old_sandboxes(max_age_hours: float, base_dir: str | Path | None = None) -> list[Path]:
    root = Path(base_dir or os.getenv("RALF_SANDBOX_PATH") or SANDBOX_PATH).resolve()
    if not root.exists():
        return []
    cutoff = time.time() - (max_age_hours * 3600)
    removed: list[Path] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            child.relative_to(root)
        except ValueError:
            continue
        if child.stat().st_mtime < cutoff:
            shutil.rmtree(child)
            removed.append(child)
    return removed
