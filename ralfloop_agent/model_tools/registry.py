from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY = PROJECT_ROOT / "config" / "model_tools.json"
DEFAULT_HF_CACHE = Path.home() / ".cache" / "huggingface" / "hub"


class ModelToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_id: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    revision: str | None = None
    backend: str = Field(min_length=1)
    artifact_kind: Literal["model", "builtin"] = "model"
    python_executable: str = Field(min_length=1)
    device: Literal["cpu", "cuda", "auto"] = "cpu"
    timeout_sec: int = Field(default=30, ge=1, le=600)
    max_ram_mb: int = Field(default=0, ge=0)
    max_vram_mb: int = Field(default=0, ge=0)
    local_files_only: bool = True
    trust_remote_code: bool = False
    enabled: bool = False
    status: str = "candidate"
    model_card_status: str = "unverified"
    license: str | None = None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]

    @model_validator(mode="after")
    def enabled_contract(self) -> "ModelToolSpec":
        if self.enabled and not self.revision:
            raise ValueError("enabled_model_tool_requires_revision")
        if not self.local_files_only:
            raise ValueError("model_tool_must_be_local_files_only")
        return self


@dataclass(frozen=True)
class SnapshotStatus:
    ok: bool
    status: str
    path: Path | None
    safetensors: bool


def _cache_name(model_id: str) -> str:
    return "models--" + model_id.replace("/", "--")


def validate_schema_value(value: Any, schema: dict[str, Any]) -> tuple[bool, str | None]:
    expected = schema.get("type")
    checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "boolean": lambda item: isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
    }
    if expected in checks and not checks[expected](value):
        return False, f"expected_{expected}"
    if expected == "object" and isinstance(value, dict):
        required = set(schema.get("required") or [])
        missing = sorted(required - set(value))
        if missing:
            return False, f"missing:{','.join(missing)}"
        properties = dict(schema.get("properties") or {})
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                return False, f"extra:{','.join(extra)}"
        for key, child in properties.items():
            if key in value:
                ok, error = validate_schema_value(value[key], child)
                if not ok:
                    return False, f"{key}:{error}"
    if expected == "array" and isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            ok, error = validate_schema_value(item, schema["items"])
            if not ok:
                return False, f"{index}:{error}"
    return True, None


class ModelToolRegistry:
    def __init__(
        self,
        specs: list[ModelToolSpec],
        *,
        cache_root: str | Path = DEFAULT_HF_CACHE,
        schema_version: str = "1.0",
    ) -> None:
        ids = [spec.tool_id for spec in specs]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate_model_tool_id")
        self.specs = {spec.tool_id: spec for spec in specs}
        self.cache_root = Path(cache_root)
        self.schema_version = schema_version

    @classmethod
    def load(
        cls,
        path: str | Path = DEFAULT_REGISTRY,
        *,
        cache_root: str | Path | None = None,
    ) -> "ModelToolRegistry":
        if cache_root is None:
            configured = (
                os.getenv("RALF_MODEL_TOOL_HF_CACHE", "").strip()
                or os.getenv("HF_HUB_CACHE", "").strip()
            )
            if configured:
                cache_root = Path(configured).expanduser()
            else:
                hf_home = os.getenv("HF_HOME", "").strip()
                cache_root = Path(hf_home).expanduser() / "hub" if hf_home else DEFAULT_HF_CACHE
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("schema_version") != "1.0" or not isinstance(payload.get("tools"), list):
            raise ValueError("invalid_model_tool_registry")
        return cls(
            [ModelToolSpec.model_validate(item) for item in payload["tools"]],
            cache_root=cache_root,
            schema_version=payload["schema_version"],
        )

    def get(self, tool_id: str) -> ModelToolSpec:
        try:
            return self.specs[tool_id]
        except KeyError as exc:
            raise KeyError("unknown_model_tool") from exc

    def for_capability(self, capability: str) -> ModelToolSpec | None:
        return next((spec for spec in self.specs.values() if spec.capability == capability), None)

    def snapshot_status(self, spec: ModelToolSpec) -> SnapshotStatus:
        root = self.cache_root / _cache_name(spec.model_id)
        if not root.is_dir():
            return SnapshotStatus(False, "snapshot_missing", None, False)
        if not spec.revision:
            return SnapshotStatus(False, "revision_missing", None, False)
        candidate = root / "snapshots" / spec.revision
        if not candidate.is_dir():
            ref = root / "refs" / spec.revision
            if ref.is_file():
                try:
                    candidate = root / "snapshots" / ref.read_text(encoding="utf-8").strip()
                except OSError:
                    pass
        if not candidate.is_dir():
            return SnapshotStatus(False, "snapshot_missing", None, False)
        safetensors = any(candidate.glob("*.safetensors"))
        return SnapshotStatus(True, "ready", candidate.resolve(), safetensors)

    def availability(self, spec: ModelToolSpec) -> SnapshotStatus:
        if spec.trust_remote_code or spec.status in {
            "model_rejected_remote_code_required",
            "sandboxed_remote_code_required",
        }:
            return SnapshotStatus(False, "sandboxed_remote_code_required", None, False)
        if spec.artifact_kind == "builtin":
            if not spec.enabled:
                return SnapshotStatus(False, "tool_disabled", PROJECT_ROOT, False)
            executable = Path(spec.python_executable)
            if not executable.is_file() or not executable.stat().st_mode & 0o111:
                return SnapshotStatus(False, "python_executable_unavailable", PROJECT_ROOT, False)
            if spec.backend == "bubblewrap_systemd" and not all(
                Path(path).is_file() for path in ("/usr/bin/bwrap", "/usr/bin/systemd-run")
            ):
                return SnapshotStatus(False, "sandbox_runtime_unavailable", PROJECT_ROOT, False)
            return SnapshotStatus(True, "ready", PROJECT_ROOT, False)
        snapshot = self.snapshot_status(spec)
        if not snapshot.ok:
            return snapshot
        if spec.status == "dependency_missing":
            return SnapshotStatus(False, "dependency_missing", snapshot.path, snapshot.safetensors)
        if not spec.enabled:
            return SnapshotStatus(False, "tool_disabled", snapshot.path, snapshot.safetensors)
        executable = Path(spec.python_executable)
        if not executable.is_file() or not executable.stat().st_mode & 0o111:
            return SnapshotStatus(False, "python_executable_unavailable", snapshot.path, snapshot.safetensors)
        return snapshot

    def download_command(self, spec: ModelToolSpec) -> list[str]:
        if spec.artifact_kind == "builtin":
            return []
        return [
            "huggingface-cli",
            "download",
            spec.model_id,
            "--revision",
            spec.revision or "<PINNED_REVISION>",
            "--local-dir-use-symlinks",
            "False",
        ]

    def health(self) -> dict[str, Any]:
        tools = []
        for spec in self.specs.values():
            status = self.availability(spec)
            tools.append(
                {
                    "tool_id": spec.tool_id,
                    "capability": spec.capability,
                    "model_id": spec.model_id,
                    "revision": spec.revision,
                    "enabled": spec.enabled,
                    "status": status.status,
                    "snapshot": str(status.path) if status.path else None,
                    "safetensors": status.safetensors,
                    "model_card_status": spec.model_card_status,
                    "license": spec.license,
                    "download_command": self.download_command(spec),
                }
            )
        return {"schema_version": self.schema_version, "tools": tools}


__all__ = [
    "DEFAULT_HF_CACHE",
    "DEFAULT_REGISTRY",
    "ModelToolRegistry",
    "ModelToolSpec",
    "SnapshotStatus",
    "validate_schema_value",
]
