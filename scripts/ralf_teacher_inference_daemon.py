#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import struct
import sys
import time
from typing import Any

from ralfloop_agent.teacher.inference import (
    SharedTeacherInferenceEngine,
)


MAX_REQUEST_BYTES = 2 * 1024 * 1024


def _peer_uid(conn: socket.socket) -> int:
    raw = conn.getsockopt(
        socket.SOL_SOCKET,
        socket.SO_PEERCRED,
        struct.calcsize("3i"),
    )
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


def _read_request(conn: socket.socket) -> dict[str, str]:
    conn.settimeout(10.0)
    raw = bytearray()

    while len(raw) <= MAX_REQUEST_BYTES:
        chunk = conn.recv(
            min(
                65536,
                MAX_REQUEST_BYTES + 1 - len(raw),
            )
        )

        if not chunk:
            break

        raw.extend(chunk)

        if b"\n" in chunk:
            break

    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("request_too_large")

    if b"\n" not in raw:
        raise ValueError("request_not_terminated")

    line, remainder = bytes(raw).split(b"\n", 1)

    if remainder:
        raise ValueError("multiple_requests_denied")

    payload = json.loads(line)

    if (
        not isinstance(payload, dict)
        or set(payload) != {
            "system_prompt",
            "user_prompt",
        }
        or not isinstance(payload["system_prompt"], str)
        or not isinstance(payload["user_prompt"], str)
    ):
        raise ValueError("invalid_request")

    return payload


def _send(
    conn: socket.socket,
    payload: dict[str, Any],
) -> bool:
    try:
        conn.sendall(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
    except OSError:
        # Il client può essersi disconnesso mentre Qwen stava
        # generando. Questo non deve terminare il daemon condiviso.
        return False

    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument(
        "--allow-uid",
        required=True,
        type=int,
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=300.0,
    )
    args = parser.parse_args()

    socket_path = Path(args.socket)
    socket_path.parent.mkdir(
        mode=0o750,
        parents=True,
        exist_ok=True,
    )

    socket_path.unlink(missing_ok=True)

    engine = SharedTeacherInferenceEngine()
    stopping = False

    server = socket.socket(
        socket.AF_UNIX,
        socket.SOCK_STREAM,
    )

    def stop_handler(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)

    try:
        server.bind(str(socket_path))
        os.chmod(socket_path, 0o660)
        server.listen(16)
        server.settimeout(1.0)

        while not stopping:
            try:
                conn, _ = server.accept()
            except socket.timeout:
                engine.release_if_idle(
                    max(1.0, args.idle_timeout)
                )
                continue

            with conn:
                try:
                    if _peer_uid(conn) != args.allow_uid:
                        raise PermissionError("peer_uid_denied")

                    request = _read_request(conn)

                    result = engine.infer(
                        request["system_prompt"],
                        request["user_prompt"],
                    )

                    _send(
                        conn,
                        {
                            "ok": True,
                            "result": result,
                        },
                    )

                except Exception as exc:
                    _send(
                        conn,
                        {
                            "ok": False,
                            "error": type(exc).__name__,
                        },
                    )

    finally:
        server.close()
        engine.close()
        socket_path.unlink(missing_ok=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
