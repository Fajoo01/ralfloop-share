# AgentCPM lifecycle broker

The system service runs as `bandi` and talks to that user's systemd user bus via
`/run/user/1001/bus`. Ralf can request only `status`, `stop`, or `start` for the
compiled-in `ralfloop-agentcpm.service` unit over the dedicated Unix socket.
Socket group membership is only a filesystem gate: `SO_PEERCRED` additionally
requires UID 1000.

The empty capability sets intentionally exclude `CAP_KILL`; lifecycle changes
go through the user manager. `ProtectHome=read-only` permits read-only provenance
checks of the configured model and `/proc`, while `RuntimeDirectory` is the only
service-owned writable location. AF_INET is retained solely for the loopback
`/v1/models` provenance check; the broker does not listen on TCP.

Install from the checked worktree only after review:

```sh
sudo /home/sibilla-cumana/ralfloop_local_architecture_worktree/tools/install_agentcpm_lifecycle_broker.sh
```
