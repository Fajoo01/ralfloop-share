#!/usr/bin/env python3
from __future__ import annotations

"""Mailchimp MCP stdio-to-AF_UNIX relay for Ralfloop."""

import argparse
import os
from pathlib import Path
import selectors
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time

try:
    from ralf_mcp_peer_auth import assign_socket_group, peer_allowed
except ModuleNotFoundError:  # importlib-based tests/loaders run from repository root
    from scripts.ralf_mcp_peer_auth import assign_socket_group, peer_allowed


MAX_LINE = 8 * 1024 * 1024


def _peer_uid(conn: socket.socket) -> int:
    raw = conn.getsockopt(
        socket.SOL_SOCKET,
        socket.SO_PEERCRED,
        struct.calcsize("3i"),
    )
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


def _start_child(
    python_executable: str,
    command: str,
    child_stderr: object,
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [python_executable, command],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=child_stderr,
        shell=False,
        start_new_session=True,
    )


def _relay(
    conn: socket.socket,
    python_executable: str,
    command: str,
    idle_timeout: float,
) -> None:
    child_stderr = tempfile.TemporaryFile()
    try:
        process = _start_child(python_executable, command, child_stderr)
    except OSError as exc:
        child_stderr.close()
        print(
            "mailchimp_mcp_child_start_failed "
            f"error_class={type(exc).__name__}",
            file=sys.stderr,
            flush=True,
        )
        raise

    assert process.stdin is not None
    assert process.stdout is not None

    selector = selectors.DefaultSelector()
    selector.register(conn, selectors.EVENT_READ, "client")
    selector.register(process.stdout, selectors.EVENT_READ, "server")

    buffers = {
        "client": bytearray(),
        "server": bytearray(),
    }

    last_activity = time.monotonic()

    try:
        while process.poll() is None:
            events = selector.select(1.0)

            if (
                not events
                and time.monotonic() - last_activity >= idle_timeout
            ):
                return

            for key, _mask in events:
                source = key.data
                chunk = os.read(
                    key.fileobj.fileno(),
                    65536,
                )

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

        if process.returncode not in (0, -signal.SIGTERM, -signal.SIGKILL):
            child_stderr.seek(0, os.SEEK_END)
            stderr_bytes = child_stderr.tell()
            print(
                "mailchimp_mcp_child_exit "
                f"returncode={process.returncode} stderr_bytes={stderr_bytes}",
                file=sys.stderr,
                flush=True,
            )
        child_stderr.close()


def _validated_executable(value: str) -> str:
    path = Path(value)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise argparse.ArgumentTypeError("must be an absolute executable file")
    return str(path)


def _validated_server_script(value: str) -> str:
    path = Path(value)
    if not path.is_absolute() or not path.is_file() or path.suffix != ".py":
        raise argparse.ArgumentTypeError("must be an absolute Python script file")
    return str(path)


def main() -> int:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--socket",
        required=True,
    )
    parser.add_argument(
        "--allow-group",
        default="ralf-mcp",
    )
    parser.add_argument(
        "--allow-uid",
        type=int,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--command",
        required=True,
        type=_validated_server_script,
    )
    parser.add_argument(
        "--python",
        type=_validated_executable,
        default=sys.executable,
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=60.0,
    )

    args = parser.parse_args()

    path = Path(args.socket)

    path.parent.mkdir(
        mode=0o700,
        parents=True,
        exist_ok=True,
    )

    try:
        path.unlink(missing_ok=True)

        server = socket.socket(
            socket.AF_UNIX,
            socket.SOCK_STREAM,
        )

        server.bind(str(path))
        if args.allow_uid is None:
            assign_socket_group(path, args.allow_group)
        else:
            os.chmod(path, 0o600)
        server.listen(4)

        signal.signal(
            signal.SIGTERM,
            lambda *_: server.close(),
        )

        while True:
            conn, _ = server.accept()

            with conn:
                if not peer_allowed(conn, args.allow_group, args.allow_uid):
                    continue

                _relay(
                    conn,
                    args.python,
                    args.command,
                    max(1.0, args.idle_timeout),
                )

    except (KeyboardInterrupt, OSError):
        return 0

    finally:
        path.unlink(missing_ok=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
