#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import select
import socket
import sys
from pathlib import Path


PROTOCOL = "ralf-teacher-mcp-bridge-v1"
MAX_REPLY = 4096


def _load_token(path: str) -> str:
    token = Path(path).read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise ValueError("invalid Teacher bridge token")
    return token


def _readline(sock: socket.socket) -> bytes:
    data = bytearray()

    while True:
        if len(data) > MAX_REPLY:
            raise RuntimeError("Teacher bridge reply too large")

        chunk = sock.recv(1)

        if not chunk:
            raise RuntimeError("Teacher bridge closed during authentication")

        if chunk == b"\n":
            return bytes(data)

        data.extend(chunk)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="stdio client for the remote Teacher MCP bridge"
    )
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=19138)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    args = parser.parse_args()

    token = _load_token(args.token_file)

    sock = socket.create_connection(
        (args.host, args.port),
        timeout=args.connect_timeout,
    )

    try:
        auth = {
            "protocol": PROTOCOL,
            "auth": token,
        }

        sock.sendall(
            (
                json.dumps(auth, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
        )

        reply = json.loads(_readline(sock).decode("utf-8"))

        if (
            not isinstance(reply, dict)
            or reply.get("ok") is not True
            or reply.get("protocol") != PROTOCOL
        ):
            raise RuntimeError("Teacher bridge authentication failed")

        sock.settimeout(None)

        stdin_fd = sys.stdin.buffer.fileno()
        stdout_fd = sys.stdout.buffer.fileno()
        socket_fd = sock.fileno()
        stdin_open = True

        while True:
            readers = [socket_fd]

            if stdin_open:
                readers.append(stdin_fd)

            ready, _, _ = select.select(readers, [], [])

            if stdin_open and stdin_fd in ready:
                chunk = os.read(stdin_fd, 65536)

                if chunk:
                    sock.sendall(chunk)
                else:
                    stdin_open = False
                    try:
                        sock.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass

            if socket_fd in ready:
                chunk = sock.recv(65536)

                if not chunk:
                    break

                view = memoryview(chunk)

                while view:
                    written = os.write(stdout_fd, view)
                    view = view[written:]

    finally:
        sock.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
