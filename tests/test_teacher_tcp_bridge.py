from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = (
    "/home/sibilla-cumana/"
    "ralfloop_agent_scaffold/.venv/bin/python"
)

BRIDGE_SCRIPT = ROOT / "scripts/ralf_teacher_tcp_bridge.py"
CLIENT_SCRIPT = ROOT / "scripts/ralf_teacher_tcp_client.py"

BRIDGE_UNIT = (
    ROOT
    / "deploy/systemd/ralf-teacher-tcp-bridge.service"
).read_text()

FIREWALL_UNIT = (
    ROOT
    / "deploy/systemd/ralf-teacher-vpn-firewall.service"
).read_text()

FIREWALL_SCRIPT = (
    ROOT
    / "scripts/ralf_teacher_vpn_firewall.sh"
).read_text()


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_tcp(port: int, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 4

    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                f"bridge exited early: {process.returncode}"
            )

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return

        time.sleep(0.02)

    raise AssertionError("bridge did not start")


def _start_echo_unix(path: Path) -> threading.Thread:
    ready = threading.Event()

    def run() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            server.listen(1)
            ready.set()

            conn, _ = server.accept()

            with conn:
                while True:
                    data = conn.recv(65536)

                    if not data:
                        break

                    conn.sendall(data)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    assert ready.wait(2)
    return thread


def _auth_line(token: str) -> bytes:
    return (
        json.dumps(
            {
                "protocol": "ralf-teacher-mcp-bridge-v1",
                "auth": token,
            },
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def test_teacher_tcp_bridge_relays_only_after_auth(tmp_path):
    unix_path = tmp_path / "teacher.sock"
    token_path = tmp_path / "token"
    token = "a" * 64

    token_path.write_text(token)
    _start_echo_unix(unix_path)

    port = _free_tcp_port()

    bridge = subprocess.Popen(
        [
            PYTHON,
            str(BRIDGE_SCRIPT),
            "--bind",
            "127.0.0.1",
            "--port",
            str(port),
            "--unix-socket",
            str(unix_path),
            "--token-file",
            str(token_path),
            "--allow-cidr",
            "127.0.0.0/8",
            "--max-clients",
            "2",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        _wait_tcp(port, bridge)

        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            payload = (
                b'{"jsonrpc":"2.0","id":1,'
                b'"method":"tools/list","params":{}}\n'
            )

            client.sendall(_auth_line(token) + payload)

            auth_reply = b""

            while not auth_reply.endswith(b"\n"):
                auth_reply += client.recv(1)

            parsed = json.loads(auth_reply)

            assert parsed == {
                "ok": True,
                "protocol": "ralf-teacher-mcp-bridge-v1",
            }

            echoed = b""

            while len(echoed) < len(payload):
                echoed += client.recv(65536)

            assert echoed == payload

    finally:
        bridge.terminate()
        bridge.wait(timeout=5)


def test_teacher_tcp_bridge_rejects_wrong_token_without_upstream(tmp_path):
    unix_path = tmp_path / "must-not-be-opened.sock"
    token_path = tmp_path / "token"
    token_path.write_text("b" * 64)

    port = _free_tcp_port()

    bridge = subprocess.Popen(
        [
            PYTHON,
            str(BRIDGE_SCRIPT),
            "--bind",
            "127.0.0.1",
            "--port",
            str(port),
            "--unix-socket",
            str(unix_path),
            "--token-file",
            str(token_path),
            "--allow-cidr",
            "127.0.0.0/8",
        ]
    )

    try:
        _wait_tcp(port, bridge)

        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            client.sendall(_auth_line("c" * 64))

            reply = b""

            while not reply.endswith(b"\n"):
                chunk = client.recv(1)
                assert chunk
                reply += chunk

            assert json.loads(reply) == {
                "ok": False,
                "error": "unauthorized",
            }

        assert not unix_path.exists()

    finally:
        bridge.terminate()
        bridge.wait(timeout=5)


def test_teacher_tcp_bridge_systemd_boundary():
    assert "--bind 10.252.14.138" in BRIDGE_UNIT
    assert "--port 19138" in BRIDGE_UNIT
    assert (
        "--unix-socket /run/ralf-teacher-mcp/mcp.sock"
        in BRIDGE_UNIT
    )
    assert "--allow-cidr 10.252.14.0/24" in BRIDGE_UNIT
    assert "--max-clients 6" in BRIDGE_UNIT

    assert "RestrictAddressFamilies=AF_UNIX AF_INET" in BRIDGE_UNIT
    assert "IPAddressDeny=any" in BRIDGE_UNIT
    assert "IPAddressAllow=10.252.14.0/24" in BRIDGE_UNIT

    assert "teacher-inference" not in BRIDGE_UNIT
    assert "inference.sock" not in BRIDGE_UNIT

    for forbidden in (
        "mailchimp",
        "runts",
        "pec_",
        "google_workspace",
        "whatsapp",
        "home_assistant",
        "bottazzi",
    ):
        assert forbidden not in BRIDGE_UNIT.lower()


def test_teacher_tcp_firewall_is_vpn_only():
    assert 'VPN_IF="sibilla"' in FIREWALL_SCRIPT
    assert 'VPN_NET="10.252.14.0/24"' in FIREWALL_SCRIPT
    assert 'DEST_IP="10.252.14.138"' in FIREWALL_SCRIPT
    assert 'PORT="19138"' in FIREWALL_SCRIPT
    assert 'COMMENT="wg-teacher-mcp-19138"' in FIREWALL_SCRIPT

    assert "vpn-service-ips.service" in FIREWALL_UNIT
    assert "ralf-teacher-tcp-bridge.service" in FIREWALL_UNIT


def test_student_tcp_client_contains_no_privileged_routing():
    text = CLIENT_SCRIPT.read_text().lower()

    for forbidden in (
        "teacher-inference",
        "inference.sock",
        "mailchimp",
        "runts",
        "pec_",
        "google_workspace",
        "whatsapp",
        "home_assistant",
        "bottazzi",
    ):
        assert forbidden not in text


INSTALLER = (
    ROOT
    / "tools/install_teacher_remote_bridge.sh"
).read_text()


def test_teacher_remote_installer_is_release_bound_and_fail_closed():
    assert (
        'PROD="/home/sibilla-cumana/ralfloop-production"'
        in INSTALLER
    )
    assert 'CURRENT="$PROD/current"' in INSTALLER
    assert '"$PROD"/releases/*' in INSTALLER

    assert "teacher-bridge.token" in INSTALLER
    assert "chmod 0640" in INSTALLER
    assert "chown root:bandi" in INSTALLER

    assert "ralf-teacher-mcp-broker.service" in INSTALLER
    assert "ralf-teacher-inference.service" in INSTALLER

    assert "10.252.14.138:19138" in INSTALLER
    assert "systemctl daemon-reload" in INSTALLER

    assert (
        'systemctl restart "$FIREWALL_UNIT"'
        in INSTALLER
    )
    assert (
        'systemctl restart "$BRIDGE_UNIT"'
        in INSTALLER
    )

    assert "/run/ralf-teacher-inference/inference.sock" not in INSTALLER


def test_teacher_firewall_matches_real_host_policy():
    assert "nft insert rule inet filter input" in FIREWALL_SCRIPT
    assert 'iifname "$VPN_IF"' in FIREWALL_SCRIPT
    assert 'ip saddr "$VPN_NET"' in FIREWALL_SCRIPT
    assert 'ip daddr "$DEST_IP"' in FIREWALL_SCRIPT
    assert 'tcp dport "$PORT"' in FIREWALL_SCRIPT

    assert "wg-quick-sibilla preraw" not in FIREWALL_SCRIPT
    assert "wg-quick-sibilla premangle" not in FIREWALL_SCRIPT

    assert "ralf_teacher_vpn_firewall.sh up" in FIREWALL_UNIT
    assert "ralf_teacher_vpn_firewall.sh down" in FIREWALL_UNIT


def test_teacher_remote_installer_waits_for_listener_readiness():
    root = Path(__file__).resolve().parents[1]
    installer = (
        root / "tools" / "install_teacher_remote_bridge.sh"
    ).read_text(encoding="utf-8")

    assert "LISTENER_READY=0" in installer
    assert "for _ in $(seq 1 50); do" in installer
    assert "sleep 0.1" in installer
    assert '[ "$LISTENER_READY" -eq 1 ]' in installer
    assert "Teacher bridge listener did not become ready" in installer

    restart = installer.index('systemctl restart "$BRIDGE_UNIT"')
    wait = installer.index("LISTENER_READY=0")
    firewall_check = installer.index(
        "nft list chain inet filter input", wait
    )

    assert restart < wait < firewall_check
