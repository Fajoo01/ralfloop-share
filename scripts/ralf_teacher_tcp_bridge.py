#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import socket
import threading
from pathlib import Path


PROTOCOL = "ralf-teacher-mcp-bridge-v1"
DEFAULT_SOCKET = "/run/ralf-teacher-mcp/mcp.sock"
MAX_AUTH_BYTES = 4096


class BridgeAuthError(RuntimeError):
    pass


def _load_token(path: str) -> str:
    token = Path(path).read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise ValueError("teacher bridge token must contain at least 32 characters")
    return token


def _peer_allowed(peer_ip: str, allowed_cidr: str) -> bool:
    address = ipaddress.ip_address(peer_ip)
    network = ipaddress.ip_network(allowed_cidr, strict=True)
    return address in network


def _recv_auth(
    client: socket.socket,
    *,
    timeout: float,
    max_bytes: int = MAX_AUTH_BYTES,
) -> tuple[dict[str, object], bytes]:
    client.settimeout(timeout)
    buffer = bytearray()

    while True:
        if len(buffer) > max_bytes:
            raise BridgeAuthError("authentication frame too large")

        chunk = client.recv(min(4096, max_bytes + 1 - len(buffer)))

        if not chunk:
            raise BridgeAuthError("connection closed before authentication")

        buffer.extend(chunk)
        newline = buffer.find(b"\n")

        if newline >= 0:
            raw = bytes(buffer[:newline])
            remainder = bytes(buffer[newline + 1 :])
            break

    if len(raw) > max_bytes:
        raise BridgeAuthError("authentication frame too large")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeAuthError("invalid authentication frame") from exc

    if not isinstance(payload, dict):
        raise BridgeAuthError("invalid authentication frame")

    return payload, remainder


def _authenticate(
    client: socket.socket,
    *,
    expected_token: str,
    timeout: float,
) -> bytes:
    payload, remainder = _recv_auth(client, timeout=timeout)

    if set(payload) != {"protocol", "auth"}:
        raise BridgeAuthError("invalid authentication fields")

    if payload.get("protocol") != PROTOCOL:
        raise BridgeAuthError("unsupported protocol")

    supplied = payload.get("auth")

    if not isinstance(supplied, str):
        raise BridgeAuthError("invalid authentication token")

    if not hmac.compare_digest(supplied, expected_token):
        raise BridgeAuthError("unauthorized")

    client.sendall(
        (
            json.dumps(
                {"ok": True, "protocol": PROTOCOL},
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    )

    client.settimeout(None)
    return remainder


def _safe_error(client: socket.socket, error: str) -> None:
    try:
        client.sendall(
            (
                json.dumps(
                    {"ok": False, "error": error},
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        )
    except OSError:
        pass


def _pump(source: socket.socket, destination: socket.socket) -> None:
    try:
        while True:
            chunk = source.recv(65536)
            if not chunk:
                break
            destination.sendall(chunk)
    except OSError:
        pass
    finally:
        try:
            destination.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _relay(client: socket.socket, upstream: socket.socket) -> None:
    a = threading.Thread(
        target=_pump,
        args=(client, upstream),
        daemon=True,
    )
    b = threading.Thread(
        target=_pump,
        args=(upstream, client),
        daemon=True,
    )

    a.start()
    b.start()
    a.join()
    b.join()


def _serve_client(
    client: socket.socket,
    peer: tuple[str, int],
    *,
    unix_socket: str,
    allowed_cidr: str,
    expected_token: str,
    auth_timeout: float,
    slots: threading.BoundedSemaphore,
) -> None:
    try:
        if not _peer_allowed(peer[0], allowed_cidr):
            _safe_error(client, "peer_not_allowed")
            return

        try:
            remainder = _authenticate(
                client,
                expected_token=expected_token,
                timeout=auth_timeout,
            )
        except BridgeAuthError:
            _safe_error(client, "unauthorized")
            return

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
            upstream.connect(unix_socket)

            if remainder:
                upstream.sendall(remainder)

            _relay(client, upstream)

    finally:
        try:
            client.close()
        finally:
            slots.release()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Authenticated VPN TCP relay for the isolated Teacher MCP socket."
        )
    )
    parser.add_argument("--bind", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument(
        "--unix-socket",
        default=DEFAULT_SOCKET,
    )
    parser.add_argument("--token-file", required=True)
    parser.add_argument(
        "--allow-cidr",
        default="10.252.14.0/24",
    )
    parser.add_argument(
        "--max-clients",
        type=int,
        default=6,
    )
    parser.add_argument(
        "--auth-timeout",
        type=float,
        default=10.0,
    )
    args = parser.parse_args()

    if not 1 <= args.max_clients <= 16:
        parser.error("--max-clients must be between 1 and 16")

    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    ipaddress.ip_address(args.bind)
    ipaddress.ip_network(args.allow_cidr, strict=True)

    expected_token = _load_token(args.token_file)
    slots = threading.BoundedSemaphore(args.max_clients)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((args.bind, args.port))
        server.listen(max(8, args.max_clients))

        while True:
            client, peer = server.accept()

            if not slots.acquire(blocking=False):
                _safe_error(client, "busy")
                client.close()
                continue

            thread = threading.Thread(
                target=_serve_client,
                args=(client, peer),
                kwargs={
                    "unix_socket": args.unix_socket,
                    "allowed_cidr": args.allow_cidr,
                    "expected_token": expected_token,
                    "auth_timeout": args.auth_timeout,
                    "slots": slots,
                },
                daemon=True,
            )
            thread.start()


if __name__ == "__main__":
    raise SystemExit(main())
