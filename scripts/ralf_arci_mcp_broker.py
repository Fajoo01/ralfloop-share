#!/usr/bin/env python3
from __future__ import annotations

"""One-client, same-UID ARCI MCP stdio-to-AF_UNIX relay."""

import argparse
import os
from pathlib import Path
import selectors
import signal
import socket
import struct
import subprocess
import sys
import time


MAX_LINE = 8 * 1024 * 1024


def _peer_uid(conn: socket.socket) -> int:
    raw = conn.getsockopt(
        socket.SOL_SOCKET,
        socket.SO_PEERCRED,
        struct.calcsize("3i"),
    )
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


def _relay(conn: socket.socket, command: str, idle_timeout: float) -> None:
    process = subprocess.Popen(
        [sys.executable, command],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        shell=False,
        start_new_session=True,
    )
    assert process.stdin is not None and process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(conn, selectors.EVENT_READ, "client")
    selector.register(process.stdout, selectors.EVENT_READ, "server")
    buffers = {"client": bytearray(), "server": bytearray()}
    last_activity = time.monotonic()
    try:
        while process.poll() is None:
            events = selector.select(1.0)
            if not events and time.monotonic() - last_activity >= idle_timeout:
                return
            for key, _mask in events:
                source = key.data
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    return
                last_activity = time.monotonic()
                buffer = buffers[source]
                buffer.extend(chunk)
                if len(buffer) > MAX_LINE:
                    return
                while b"\n" in buffer:
                    line, _, rest = buffer.partition(b"\n")
                    buffers[source] = buffer = bytearray(rest)
                    if not line.strip():
                        continue
                    payload = line + b"\n"
                    if source == "client":
                        process.stdin.write(payload)
                        process.stdin.flush()
                    else:
                        conn.sendall(payload)
    finally:
        selector.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--allow-uid", type=int, default=os.getuid())
    parser.add_argument("--command", required=True)
    parser.add_argument("--idle-timeout", type=float, default=60.0)
    args = parser.parse_args()
    path = Path(args.socket)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        os.chmod(path, 0o600)
        server.listen(4)
        signal.signal(signal.SIGTERM, lambda *_: server.close())
        while True:
            conn, _ = server.accept()
            with conn:
                if _peer_uid(conn) != args.allow_uid:
                    continue
                _relay(conn, args.command, max(1.0, args.idle_timeout))
    except (KeyboardInterrupt, OSError):
        return 0
    finally:
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
