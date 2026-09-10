#!/usr/bin/env python3
from __future__ import annotations

"""Multi-client Teacher MCP relay; inference is owned by one shared daemon."""

import argparse
import os
from pathlib import Path
import selectors
import signal
import socket
import struct
import subprocess
import sys
import threading
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

        # Prima consegna EOF al server Teacher: il suo finally chiude
        # la lease Qwen e lascia allo scheduler il tempo di ripristinare
        # AgentCPM.
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass

        if process.poll() is None:
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--allow-uid", required=True, type=int)
    parser.add_argument("--command", required=True)
    parser.add_argument("--idle-timeout", type=float, default=60.0)
    parser.add_argument("--max-clients", type=int, default=1)
    args = parser.parse_args()

    if args.max_clients < 1 or args.max_clients > 16:
        parser.error("--max-clients must be between 1 and 16")

    if (
        args.max_clients > 1
        and not os.getenv("RALF_TEACHER_INFERENCE_SOCKET", "").strip()
    ):
        parser.error(
            "multi-client mode requires RALF_TEACHER_INFERENCE_SOCKET"
        )
    path = Path(args.socket)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        os.chmod(path, 0o600)
        server.listen(max(4, args.max_clients))
        signal.signal(signal.SIGTERM, lambda *_: server.close())

        slots = threading.BoundedSemaphore(args.max_clients)

        def handle(conn: socket.socket) -> None:
            try:
                with conn:
                    if _peer_uid(conn) != args.allow_uid:
                        return

                    _relay(
                        conn,
                        args.command,
                        max(1.0, args.idle_timeout),
                    )
            finally:
                slots.release()

        while True:
            conn, _ = server.accept()

            if not slots.acquire(blocking=False):
                conn.close()
                continue

            threading.Thread(
                target=handle,
                args=(conn,),
                daemon=True,
                name="teacher-mcp-client",
            ).start()
    except (KeyboardInterrupt, OSError):
        return 0
    finally:
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
