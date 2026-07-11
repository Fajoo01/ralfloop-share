from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socketserver
import sys
import time
from typing import Any

from ralfloop_agent.integration.recursive_mas_host_client import (
    DEFAULT_MAX_BYTES,
    DEFAULT_SOCKET,
    PROTOCOL_VERSION,
    validate_host_request,
)
from ralfloop_agent.integration.recursive_mas_runtime import RecursiveMASRuntimeController


class RecursiveMASUnixServer(socketserver.UnixStreamServer):
    allow_reuse_address = False

    def __init__(self, socket_path: str, controller: Any | None = None, max_request_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.socket_path = socket_path
        self.controller = controller or RecursiveMASRuntimeController.from_env()
        self.max_request_bytes = max_request_bytes
        Path(socket_path).parent.mkdir(parents=True, exist_ok=True)
        if Path(socket_path).exists():
            Path(socket_path).unlink()
        super().__init__(socket_path, RecursiveMASRequestHandler)
        os.chmod(socket_path, 0o660)

    def server_close(self) -> None:
        super().server_close()
        try:
            Path(self.socket_path).unlink()
        except FileNotFoundError:
            pass


class RecursiveMASRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        started = time.monotonic()
        raw = self.rfile.readline(self.server.max_request_bytes + 1)
        if len(raw) > self.server.max_request_bytes:
            self._write({"ok": False, "status": "invalid_request", "error_type": "payload_too_large", "duration_ms": _elapsed_ms(started)})
            return
        try:
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request must be object")
        except Exception as exc:
            self._write({"ok": False, "status": "invalid_request", "error_type": "invalid_json", "error": str(exc), "duration_ms": _elapsed_ms(started)})
            return
        validation = validate_host_request(request, self.server.max_request_bytes)
        if not validation.get("ok"):
            self._write({**validation, "duration_ms": _elapsed_ms(started), "request_id": str(request.get("request_id") or "")})
            return
        if request.get("requires_human_confirmation"):
            self._write({"protocol_version": PROTOCOL_VERSION, "ok": False, "status": "human_confirmation_required", "request_id": str(request.get("request_id") or ""), "fallback_used": False, "duration_ms": _elapsed_ms(started)})
            return
        result = self.server.controller.execute({
            "goal": request["goal"],
            "style": request.get("style") or "sequential_light",
            "rounds": int(request.get("rounds") or 1),
            "domain_context": request.get("domain_context") if isinstance(request.get("domain_context"), dict) else {},
            "task_id": request.get("request_id") or "",
        })
        self._write(normalize_service_response(request, result, _elapsed_ms(started)))

    def _write(self, response: dict[str, Any]) -> None:
        response.setdefault("protocol_version", PROTOCOL_VERSION)
        self.wfile.write(json.dumps(response, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n")


def normalize_service_response(request: dict[str, Any], result: dict[str, Any], duration_ms: int) -> dict[str, Any]:
    status = str(result.get("status") or "worker_error")
    memory = result.get("memory") if isinstance(result.get("memory"), dict) else {}
    return {
        "protocol_version": PROTOCOL_VERSION,
        "ok": bool(result.get("ok")),
        "status": status,
        "request_id": str(request.get("request_id") or result.get("task_id") or ""),
        "selected_backend": result.get("selected_backend") or "recursive_mas_native",
        "native_latent_verified": bool(result.get("native_latent_verified")),
        "closed_loop_verified": bool(result.get("closed_loop_verified")),
        "answer": result.get("answer") or "",
        "duration_ms": int(result.get("duration_ms") or duration_ms),
        "fallback_used": bool(result.get("fallback_used")),
        "cleanup_completed": bool(result.get("cleanup_completed")),
        "worker_pid": result.get("worker_pid"),
        "worker_exit_code": result.get("worker_exit_code"),
        "memory": memory,
        "vram_peak_bytes": memory.get("vram_peak_bytes"),
        "rss_peak_bytes": memory.get("rss_peak_bytes"),
        "error_type": result.get("error_type"),
        "error": result.get("error"),
        "retryable": bool(result.get("retryable")),
    }


def serve(socket_path: str, controller: Any | None = None, max_request_bytes: int = DEFAULT_MAX_BYTES) -> None:
    server = RecursiveMASUnixServer(socket_path, controller=controller, max_request_bytes=max_request_bytes)

    def _stop(_signum: int, _frame: Any) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve", "health"])
    parser.add_argument("--socket", default=os.getenv("RALFLOOP_RECURSIVE_MAS_SOCKET", DEFAULT_SOCKET))
    parser.add_argument("--max-request-bytes", type=int, default=int(os.getenv("RALFLOOP_RECURSIVE_MAS_MAX_REQUEST_BYTES", str(DEFAULT_MAX_BYTES))))
    args = parser.parse_args(argv)
    if args.command == "health":
        path = Path(args.socket)
        print(json.dumps({"ok": path.exists(), "socket": str(path), "protocol_version": PROTOCOL_VERSION}, sort_keys=True))
        return 0 if path.exists() else 1
    serve(args.socket, max_request_bytes=args.max_request_bytes)
    return 0


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
