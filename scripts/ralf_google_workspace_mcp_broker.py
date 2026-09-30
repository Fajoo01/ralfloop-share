#!/usr/bin/env python3
from __future__ import annotations

"""Least-privilege, one-client MCP stdio-to-AF_UNIX relay.

Run this service as ``bandi``. It reads no Google credentials itself: the
official wrapper resolves its own account/config under bandi's home. Read calls
may come from the shared MCP group, but Gmail writes are forwarded only for the
explicit backend write UID. This keeps raw shell/direct-MCP clients read-only.
"""

import argparse
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import subprocess
import time

from ralf_mcp_peer_auth import assign_socket_group, peer_allowed, peer_uid


MAX_LINE = 8 * 1024 * 1024
GMAIL_WRITE_OPERATIONS = frozenset({"send", "reply", "replyAll", "forward"})


def _is_outbound_email_call(name: str, arguments: object) -> bool:
    args = arguments if isinstance(arguments, dict) else {}
    if name == "manage_email":
        return str(args.get("operation") or "") in GMAIL_WRITE_OPERATIONS
    if name == "manage_scratchpad":
        return str(args.get("operation") or "") == "send" and str(args.get("target") or "") == "email"
    if name not in {"bulk_operations", "queue_operations"}:
        return False
    if str(args.get("mode") or "queue") == "batch":
        return _is_outbound_email_call(str(args.get("tool") or ""), {"operation": args.get("operation")})
    for item in args.get("operations") or ():
        if isinstance(item, dict) and _is_outbound_email_call(
            str(item.get("tool") or ""), item.get("args")
        ):
            return True
    return False


def _guard_client_line(line: bytes, *, caller_uid: int, write_uid: int | None) -> bytes | None:
    try:
        request = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(request, dict) or request.get("method") != "tools/call":
        return None
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    if not _is_outbound_email_call(str(params.get("name") or ""), params.get("arguments")):
        return None
    if write_uid is not None and caller_uid == write_uid:
        return None
    response = {
        "jsonrpc": "2.0",
        "id": request.get("id"),
        "error": {
            "code": -32003,
            "message": "outbound_email_write_blocked",
            "data": {"reason": "bot_tazzi_approval_path_required"},
        },
    }
    return json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n"


def _relay(conn: socket.socket, command: str, idle_timeout: float, *, write_uid: int | None) -> None:
    process = subprocess.Popen(
        [command], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, shell=False, start_new_session=True,
    )
    assert process.stdin is not None and process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(conn, selectors.EVENT_READ, "client")
    selector.register(process.stdout, selectors.EVENT_READ, "server")
    buffers = {"client": bytearray(), "server": bytearray()}
    caller_uid = peer_uid(conn)
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
                        blocked = _guard_client_line(
                            line, caller_uid=caller_uid, write_uid=write_uid
                        )
                        if blocked is not None:
                            conn.sendall(blocked)
                            continue
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
    parser.add_argument("--allow-group", default="ralf-mcp")
    parser.add_argument("--allow-uid", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--write-uid", type=int, default=None)
    parser.add_argument("--command", default="/home/bandi/bin/google-workspace-mcp")
    parser.add_argument("--idle-timeout", type=float, default=60.0)
    args = parser.parse_args()
    path = Path(args.socket)
    path.parent.mkdir(mode=0o2770, parents=True, exist_ok=True)
    try:
        path.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        if args.allow_uid is not None:
            os.chmod(path, 0o660)
        else:
            assign_socket_group(path, args.allow_group)
        server.listen(4)
        signal.signal(signal.SIGTERM, lambda *_: server.close())
        while True:
            conn, _ = server.accept()
            with conn:
                if not peer_allowed(conn, args.allow_group, args.allow_uid):
                    continue
                _relay(
                    conn, args.command, max(1.0, args.idle_timeout),
                    write_uid=args.write_uid,
                )
    except (KeyboardInterrupt, OSError):
        return 0
    finally:
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
