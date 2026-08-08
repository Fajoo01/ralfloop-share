# Sandboxed remote code

`sandboxed_remote_code` never loads remote Python into the Ralf process.

Pipeline:

1. HTTPS-only streaming download; public DNS and connected peer must match.
2. Size, timeout, redirect and content-type gates; SHA256 provenance.
3. Static scan. Blocked findings never execute. Suspicious findings require a
   one-shot, owner-only, hash-bound approval file.
4. `systemd-run --user --scope` cgroup limits CPU, RAM, PIDs and I/O.
5. Bubblewrap creates user, PID, mount, network, IPC, UTS and cgroup
   namespaces. Only read-only runtime libraries and the ephemeral workdir are
   mounted. Host data, Docker socket and host home are absent.
6. Capabilities are empty; `no_new_privs` and two seccomp filters block network,
   privileged syscalls, nested namespaces and secondary executables.
7. Output and produced files are bounded and hashed. Temporary root is removed
   in `finally`.

Host prerequisite: install and load `deploy/apparmor/ralf-bwrap`. It grants
user-namespace creation only to `/usr/bin/bwrap`; the binary is not setuid.

Approval files live in `$RALF_REMOTE_CODE_APPROVAL_DIR` or
`~/.local/state/ralf/remote-code-approvals`. Required fields:

```json
{
  "approved": true,
  "sha256": "<downloaded-source-sha256>",
  "expires_at": "2026-08-01T12:00:00+00:00"
}
```

Filename is `<approval_id>.json`, mode `0600`, owner runtime UID. Approval is
atomically consumed. URL query strings and source content never enter audit.

Rollback host prerequisite:

```sh
vpnpc sudo --non-interactive sibilla -- /usr/sbin/apparmor_parser -R /etc/apparmor.d/ralf-bwrap
vpnpc sudo --non-interactive sibilla -- /usr/bin/rm /etc/apparmor.d/ralf-bwrap
```
