#!/bin/sh
set -eu

vpn_source="10.252.14.7/32"
vpn_network="10.252.0.0/16"
vpn_table="51820"

require_free_or_owned_priority() {
    priority="$1"
    pattern="$2"
    current="$(ip -4 rule show | awk -v key="${priority}:" '$1 == key { print }')"
    if [ -n "$current" ] && ! printf '%s\n' "$current" | grep -Fq "$pattern"; then
        echo "split-routing: priority ${priority} already used: ${current}" >&2
        exit 1
    fi
}

add_rule() {
    priority="$1"
    pattern="$2"
    shift 2
    if ! ip -4 rule show | awk -v key="${priority}:" '$1 == key { print }' | grep -Fq "$pattern"; then
        ip -4 rule add priority "$priority" "$@"
    fi
}

delete_owned_rule() {
    priority="$1"
    pattern="$2"
    if ip -4 rule show | awk -v key="${priority}:" '$1 == key { print }' | grep -Fq "$pattern"; then
        ip -4 rule del priority "$priority"
    fi
}

case "${1:-}" in
    up)
        ip -4 route show table "$vpn_table" | awk '
            $1 == "default" {
                for (i = 1; i < NF; i++)
                    if ($i == "dev" && $(i + 1) == "sibilla") found = 1
            }
            END { exit !found }
        '
        require_free_or_owned_priority 10000 "from 10.252.14.7 lookup 51820"
        require_free_or_owned_priority 10005 "to 10.252.0.0/16 lookup 51820"
        require_free_or_owned_priority 10010 "from all lookup main"
        add_rule 10000 "from 10.252.14.7 lookup 51820" from "$vpn_source" table "$vpn_table"
        add_rule 10005 "to 10.252.0.0/16 lookup 51820" to "$vpn_network" table "$vpn_table"
        add_rule 10010 "from all lookup main" table main
        ;;
    down)
        delete_owned_rule 10010 "from all lookup main"
        delete_owned_rule 10005 "to 10.252.0.0/16 lookup 51820"
        delete_owned_rule 10000 "from 10.252.14.7 lookup 51820"
        ;;
    status)
        ip -4 rule show
        ip -4 route get 15.236.85.177
        ip -4 route get 15.236.85.177 from 10.252.14.7
        ip -4 route get 10.252.14.10
        ;;
    *)
        echo "usage: $0 {up|down|status}" >&2
        exit 2
        ;;
esac
