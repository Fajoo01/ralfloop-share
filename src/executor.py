from __future__ import annotations

from pathlib import Path
from collections.abc import Sequence
import logging
import shlex
import subprocess

from src.models import Evidence

logger = logging.getLogger(__name__)
SANDBOX_PATH = Path("/tmp/ralf_sandbox")


class ShellExecutor:
    def __init__(self, base_dir: str | Path | None = None, timeout_sec: int = 30) -> None:
        self.base_dir = Path(base_dir or SANDBOX_PATH).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.timeout_sec = timeout_sec

    def run(self, command: str | Sequence[str], cwd: str | None = None) -> Evidence:
        argv = self._command_to_argv(command)
        return self.run_in_sandbox(argv, cwd=cwd)

    def run_in_sandbox(self, command: Sequence[str], cwd: str | None = None) -> Evidence:
        safe_cwd = self._safe_cwd(cwd)
        argv = [str(part) for part in command]
        command_text = shlex.join(argv)
        logger.info("shell_run command=%r cwd=%s", command_text, safe_cwd)
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
    def _command_to_argv(command: str | Sequence[str]) -> list[str]:
        if isinstance(command, str):
            return shlex.split(command)
        return [str(part) for part in command]
