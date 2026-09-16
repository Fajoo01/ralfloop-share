from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path


class GardenModelContractError(RuntimeError):
    pass


@dataclass(frozen=True)
class GardenModelSpec:
    model_id: str
    revision: str
    filename: str
    sha256: str
    detector_name: str
    path: Path


_REQUIRED_KEYS = {
    "schema_version",
    "model_id",
    "revision",
    "filename",
    "sha256",
    "detector_name",
    "runtime",
    "task",
    "target_class_id",
    "target_class_name",
    "local_path",
    "auto_download",
    "license",
    "source_url",
    "weight_format",
}


def load_model_spec(manifest_path: Path) -> GardenModelSpec:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GardenModelContractError("garden_model_manifest_unreadable") from exc
    if not isinstance(payload, dict) or set(payload) != _REQUIRED_KEYS:
        raise GardenModelContractError("garden_model_manifest_schema_invalid")
    if payload["schema_version"] != 1:
        raise GardenModelContractError("garden_model_manifest_version_unsupported")
    if payload["runtime"] != "ultralytics" or payload["task"] != "object-detection":
        raise GardenModelContractError("garden_model_runtime_contract_invalid")
    if payload["target_class_id"] != 0 or payload["target_class_name"] != "person":
        raise GardenModelContractError("garden_model_person_class_invalid")
    if payload["auto_download"] is not False:
        raise GardenModelContractError("garden_model_auto_download_forbidden")
    sha256 = str(payload["sha256"]).lower()
    if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
        raise GardenModelContractError("garden_model_sha256_invalid")
    root = manifest_path.resolve().parent
    path = (root / str(payload["local_path"])).resolve()
    if not path.is_relative_to(root) or path.name != payload["filename"]:
        raise GardenModelContractError("garden_model_path_invalid")
    return GardenModelSpec(
        model_id=str(payload["model_id"]),
        revision=str(payload["revision"]),
        filename=str(payload["filename"]),
        sha256=sha256,
        detector_name=str(payload["detector_name"]),
        path=path,
    )


def verify_local_model(path: Path, expected_sha256: str) -> Path:
    if not path.is_absolute() or path.suffix != ".pt":
        raise GardenModelContractError("garden_model_path_must_be_absolute_local_pt")
    if not path.is_file():
        raise GardenModelContractError("garden_model_snapshot_missing")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(8 * 1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise GardenModelContractError("garden_model_snapshot_unreadable") from exc
    if digest.hexdigest() != expected_sha256.lower():
        raise GardenModelContractError("garden_model_sha256_mismatch")
    return path
