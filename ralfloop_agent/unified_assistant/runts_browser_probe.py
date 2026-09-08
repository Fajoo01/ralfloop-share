"""Narrow, socket-activated read probe run only as the RUNTS browser user."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import sys

ROOT = Path(os.environ.get("BOTTAZZI_RUNTS_UPLOAD_ROOT", "/home/bandi/.local/share/bottazzi/runts")).resolve()
MAX_FILE_SIZE = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 16 * 1024


def _result(**values):
    return json.dumps(values, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def probe_runts_upload_file(path_value: object, expected_sha256: object) -> dict[str, object]:
    if not isinstance(path_value, str) or not isinstance(expected_sha256, str):
        return {"readable": False, "error_code": "runts_browser_path_denied"}
    if len(expected_sha256) != 64 or any(char not in "0123456789abcdef" for char in expected_sha256):
        return {"readable": False, "error_code": "runts_browser_file_hash_mismatch"}
    path = Path(path_value)
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(ROOT)
        # Refuse every symlink below the allowlisted root; resolving alone is not
        # enough because a symlink could leave then re-enter the tree.
        current = ROOT
        for part in resolved.relative_to(ROOT).parts:
            current /= part
            if current.is_symlink():
                return {"readable": False, "error_code": "runts_browser_path_denied"}
        details = resolved.stat()
        if not stat.S_ISREG(details.st_mode) or details.st_size <= 0 or details.st_size > MAX_FILE_SIZE:
            return {"readable": False, "error_code": "runts_browser_path_denied"}
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    except ValueError:
        return {"readable": False, "error_code": "runts_browser_path_denied"}
    except OSError:
        return {"readable": False, "error_code": "runts_browser_file_unreadable"}
    if digest != expected_sha256:
        return {"readable": False, "sha256": digest, "size": details.st_size,
                "error_code": "runts_browser_file_hash_mismatch"}
    return {"readable": True, "sha256": digest, "size": details.st_size, "error_code": None}


def serve_socket_activated() -> None:
    listener = socket.fromfd(3, socket.AF_UNIX, socket.SOCK_STREAM)
    while True:
        connection, _ = listener.accept()
        with connection:
            connection.settimeout(5)
            try:
                raw = connection.recv(MAX_REQUEST_BYTES + 1)
                if not raw or len(raw) > MAX_REQUEST_BYTES:
                    response = {"readable": False, "error_code": "runts_browser_path_denied"}
                else:
                    request = json.loads(raw.decode())
                    if not isinstance(request, dict) or set(request) != {"path", "expected_sha256"}:
                        response = {"readable": False, "error_code": "runts_browser_path_denied"}
                    else:
                        response = probe_runts_upload_file(request["path"], request["expected_sha256"])
            except (UnicodeDecodeError, json.JSONDecodeError, OSError):
                response = {"readable": False, "error_code": "runts_browser_file_unreadable"}
            connection.sendall(_result(**response))


if __name__ == "__main__":
    if "--socket-activation" not in sys.argv or os.getuid() == 0:
        raise SystemExit("runts_browser_probe_socket_activation_required")
    serve_socket_activated()
