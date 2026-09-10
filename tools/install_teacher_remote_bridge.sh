#!/usr/bin/env bash
set -euo pipefail

PROD="/home/sibilla-cumana/ralfloop-production"
CURRENT="$PROD/current"
RELEASE="$(readlink -f "$CURRENT")"

BRIDGE_UNIT="ralf-teacher-tcp-bridge.service"
FIREWALL_UNIT="ralf-teacher-vpn-firewall.service"

TOKEN_DIR="/etc/ralfloop"
TOKEN_FILE="$TOKEN_DIR/teacher-bridge.token"

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

[ "${EUID:-$(id -u)}" -eq 0 ] ||
    fail "run as root"

case "$RELEASE" in
    "$PROD"/releases/*)
        ;;
    *)
        fail "production/current is not an immutable release"
        ;;
esac

[ -f "$CURRENT/RELEASE.json" ] ||
    fail "missing RELEASE.json"

[ -f "$CURRENT/MANIFEST.sha256" ] ||
    fail "missing MANIFEST.sha256"

[ -f "$CURRENT/deploy/systemd/$BRIDGE_UNIT" ] ||
    fail "missing bridge unit"

[ -f "$CURRENT/deploy/systemd/$FIREWALL_UNIT" ] ||
    fail "missing firewall unit"

[ -x "$CURRENT/scripts/ralf_teacher_tcp_bridge.py" ] ||
    fail "missing bridge executable"

[ -x "$CURRENT/scripts/ralf_teacher_vpn_firewall.sh" ] ||
    fail "missing firewall executable"

systemctl is-active --quiet ralf-teacher-mcp-broker.service ||
    fail "Teacher MCP broker is not active"

systemctl is-active --quiet ralf-teacher-inference.service ||
    fail "Teacher inference service is not active"

ip -4 addr show dev vpnsvc0 |
    grep -qF "10.252.14.138/32" ||
    fail "10.252.14.138 is not assigned"

# Se il bridge non è già installato/attivo, la porta deve essere libera.
if ! systemctl is-active --quiet "$BRIDGE_UNIT" 2>/dev/null; then
    if ss -H -lnt |
        awk '{print $4}' |
        grep -qx '10.252.14.138:19138'
    then
        fail "10.252.14.138:19138 is already occupied"
    fi
fi

install -d -o root -g root -m 0755 "$TOKEN_DIR"

if [ ! -e "$TOKEN_FILE" ]; then
    umask 0077

    /home/sibilla-cumana/ralfloop_agent_scaffold/.venv/bin/python \
        -c 'import secrets; print(secrets.token_urlsafe(48))' \
        > "$TOKEN_FILE"
fi

chown root:bandi "$TOKEN_FILE"
chmod 0640 "$TOKEN_FILE"

TOKEN_LEN="$(
    tr -d '\r\n' < "$TOKEN_FILE" |
    wc -c
)"

[ "$TOKEN_LEN" -ge 32 ] ||
    fail "Teacher bridge token is too short"

install \
    -o root -g root -m 0644 \
    "$CURRENT/deploy/systemd/$FIREWALL_UNIT" \
    "/etc/systemd/system/$FIREWALL_UNIT"

install \
    -o root -g root -m 0644 \
    "$CURRENT/deploy/systemd/$BRIDGE_UNIT" \
    "/etc/systemd/system/$BRIDGE_UNIT"

systemctl daemon-reload

systemctl enable "$FIREWALL_UNIT" "$BRIDGE_UNIT"

cleanup_on_error() {
    systemctl stop "$BRIDGE_UNIT" 2>/dev/null || true
    systemctl stop "$FIREWALL_UNIT" 2>/dev/null || true
}
trap cleanup_on_error ERR

systemctl restart "$FIREWALL_UNIT"
systemctl restart "$BRIDGE_UNIT"

systemctl is-active --quiet "$FIREWALL_UNIT"
systemctl is-active --quiet "$BRIDGE_UNIT"

ss -H -lnt |
    awk '{print $4}' |
    grep -qx '10.252.14.138:19138'

nft list chain inet filter input |
    grep -F 'iifname "sibilla"' |
    grep -F 'ip saddr 10.252.14.0/24' |
    grep -F 'ip daddr 10.252.14.138' |
    grep -F 'tcp dport 19138' |
    grep -F 'wg-teacher-mcp-19138'

trap - ERR

echo "TEACHER_REMOTE_BRIDGE_INSTALLED"
