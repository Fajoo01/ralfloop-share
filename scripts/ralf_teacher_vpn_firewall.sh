#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-}"
VPN_IF="sibilla"
VPN_NET="10.252.14.0/24"
DEST_IP="10.252.14.138"
PORT="19138"
COMMENT="wg-teacher-mcp-19138"

handles() {
    nft -a list chain inet filter input 2>/dev/null |
    awk -v c="$COMMENT" '$0 ~ c {print $NF}'
}

add_rule() {
    nft list chain inet filter input >/dev/null 2>&1

    # Idempotenza: elimina eventuali duplicati lasciandone al massimo uno.
    first=1
    for h in $(handles); do
        if [ "$first" = 1 ]; then
            first=0
        else
            nft delete rule inet filter input handle "$h" 2>/dev/null || true
        fi
    done

    if [ "$first" = 1 ]; then
        nft insert rule inet filter input \
            iifname "$VPN_IF" \
            ip saddr "$VPN_NET" \
            ip daddr "$DEST_IP" \
            tcp dport "$PORT" \
            accept \
            comment "$COMMENT"
    fi
}

del_rule() {
    for h in $(handles); do
        nft delete rule inet filter input handle "$h" 2>/dev/null || true
    done
}

case "$ACTION" in
    up)
        add_rule
        ;;
    down)
        del_rule
        ;;
    *)
        echo "Usage: $0 up|down" >&2
        exit 2
        ;;
esac
