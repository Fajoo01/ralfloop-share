from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import shutil
import subprocess


ENGINE_NAMES = ("ollama", "llama-server", "llama-cli", "vllm", "sglang", "tabby")


@dataclass(frozen=True)
class EngineProbe:
    name: str
    path: str | None
    version: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


def probe_engine(name: str, *, extra_paths: tuple[str, ...] = ()) -> EngineProbe:
    path = shutil.which(name)
    if path is None:
        for candidate in extra_paths:
            candidate_path = Path(candidate)
            if candidate_path.is_file() and candidate_path.name == name:
                path = str(candidate_path)
                break
    if path is None:
        return EngineProbe(name=name, path=None)
    version = None
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5, check=False)
        version = ((result.stdout or result.stderr).strip().splitlines() or [None])[0]
    except (OSError, subprocess.SubprocessError):
        pass
    return EngineProbe(name=name, path=path, version=version)


def probe_engines(*, extra_paths: tuple[str, ...] = ()) -> list[dict[str, str | None]]:
    return [probe_engine(name, extra_paths=extra_paths).to_dict() for name in ENGINE_NAMES]
