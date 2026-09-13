# Sibilla split tunnel — 2026-09-13

## Problema

IPv4 Internet was selected by the WireGuard full-tunnel policy:

```text
15.236.85.177 dev sibilla table 51820 src 10.252.14.7
```

The physical path is `eno1`, address `192.168.0.26`, gateway `192.168.0.1`.
HuggingFace range throughput before the change was 4,800,851 B/s through the
tunnel and 36,320,688 B/s when bound to `eno1`.

## Causa

`wg-quick@sibilla` is active with `AllowedIPs = 0.0.0.0/0`. It installs table
`51820` plus these rules:

```text
32764: from all lookup main suppress_prefixlength 0
32765: not from all fwmark 0xca6c lookup 51820
```

The main table already had the correct physical default route. The WireGuard
policy selected table `51820` first for every unmarked IPv4 Internet flow.

## Modifica

Installed `/usr/local/sbin/sibilla-split-routing` and added matching `PostUp`
and `PreDown` entries to `/etc/wireguard/sibilla.conf`. No interface or
networking service was restarted. `AllowedIPs` remains unchanged.

```text
10000: from 10.252.14.7 lookup 51820
10005: from all to 10.252.0.0/16 lookup 51820
10010: from all lookup main
```

Rule `10000` preserved established Xet and service flows whose source was
already the WireGuard address. Rule `10005` keeps vpnpc peers and VPN DNS in
the tunnel. Rule `10010` sends newly created Internet flows through the
physical default route. The WireGuard endpoint remains reachable through the
main table because its transport packets carry fwmark `0xca6c`.

Pre-change state and both WireGuard configs are root-only in:

```text
/var/backups/ralfloop/sibilla-split-routing-20260913T054741Z
```

Two transient five-minute rollback timers were armed before the live and
persistent changes. They were cancelled only after external reachability,
route, configuration, and download checks passed. The retained rollback is:

```sh
vpnpc sudo --non-interactive sibilla -- \
  /var/backups/ralfloop/sibilla-split-routing-20260913T054741Z/rollback-permanent.sh
```

It removes only the three owned rules and restores the original WireGuard
configuration. It does not stop WireGuard or restart networking.

## Verifica

```text
vpnpc check sibilla: OK before and after persistence
15.236.85.177 via 192.168.0.1 dev eno1 src 192.168.0.26
8.8.8.8 via 192.168.0.1 dev eno1 src 192.168.0.26
10.252.14.4 dev sibilla table 51820 src 10.252.14.7
10.252.14.10 dev sibilla table 51820 src 10.252.14.7
10.252.14.12 dev sibilla table 51820 src 10.252.14.7
wg-quick strip sibilla: PASS
```

The existing `hf_xet` process kept PID `1014635`. Immediately after the
change it retained six old tunnel sockets and opened nineteen physical
sockets. Later all 39 IPv4 sockets used `192.168.0.26`; the process was never
stopped. A post-change range request received 2,306,892 B/s of residual
bandwidth while Xet was active. A concurrent ten-second counter sample showed
123,102,064 B/s on `eno1` and 10,758,144 B/s of process disk writes. The
partial grew to 39,185,733,452 bytes and remains resumable in the HuggingFace
cache.
