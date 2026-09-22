"""Shared group-based peer policy for local Ralfloop MCP sockets."""
from __future__ import annotations

import grp
import os
from pathlib import Path
import pwd
import socket
import struct


def group_gid(group_name: str = "ralf-mcp") -> int:
    try:
        return grp.getgrnam(group_name).gr_gid
    except KeyError as exc:
        raise RuntimeError(f"mcp_peer_group_missing:{group_name}") from exc


def peer_uid(conn: socket.socket) -> int:
    raw = conn.getsockopt(
        socket.SOL_SOCKET,
        socket.SO_PEERCRED,
        struct.calcsize("3i"),
    )
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


def peer_in_group(conn: socket.socket, group_name: str = "ralf-mcp") -> bool:
    uid = peer_uid(conn)
    try:
        account = pwd.getpwuid(uid)
        gids = os.getgrouplist(account.pw_name, account.pw_gid)
    except (KeyError, OSError):
        return False
    return group_gid(group_name) in gids


def peer_allowed(
    conn: socket.socket,
    group_name: str = "ralf-mcp",
    legacy_uid: int | None = None,
) -> bool:
    uid = peer_uid(conn)
    return uid == legacy_uid if legacy_uid is not None else peer_in_group(conn, group_name)


def assign_socket_group(path: str | Path, group_name: str = "ralf-mcp") -> None:
    socket_path = Path(path)
    os.chown(socket_path, -1, group_gid(group_name))
    os.chmod(socket_path, 0o660)
