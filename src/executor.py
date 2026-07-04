from __future__ import annotations

from pathlib import Path
import logging
import shlex
import subprocess
import tempfile

from src.models import Evidence

logger = logging.getLogger(__name__)


class ShellExecutor:
    def __init__(self, base_dir: str | Path | None = None, timeout_sec: int = 20) -> None:
        self.base_dir = Path(base_dir or tempfile.mkdtemp(prefix="ralf_capability_")).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.timeout_sec = timeout_sec

    def run(self, command: str, cwd: str | None = None) -> Evidence:
        safe_cwd = self._safe_cwd(cwd)
        logger.info("shell_run command=%r cwd=%s", command, safe_cwd)
        try:
            argv = shlex.split(command)
            if not argv:
                return Evidence(command=command, path=str(safe_cwd), exit_code=2, stderr="empty command")
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
                command=command,
                path=str(safe_cwd),
                exit_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        except subprocess.TimeoutExpired as exc:
            return Evidence(
                command=command,
                path=str(safe_cwd),
                exit_code=124,
                stdout=exc.stdout if isinstance(exc.stdout, str) else None,
                stderr=f"timeout after {self.timeout_sec}s",
            )
        except Exception as exc:
            return Evidence(command=command, path=str(safe_cwd), exit_code=1, stderr=str(exc))

    def _safe_cwd(self, cwd: str | None) -> Path:
        if cwd is None:
            return self.base_dir
        candidate = (self.base_dir / cwd).resolve() if not Path(cwd).is_absolute() else Path(cwd).resolve()
        try:
            candidate.relative_to(self.base_dir)
        except ValueError as exc:
            raise ValueError(f"cwd escapes sandbox: {cwd}") from exc
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate
