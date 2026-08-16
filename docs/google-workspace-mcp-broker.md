# Google Workspace MCP broker

Ralf (UID 1000, `sibilla-cumana`) must not read bandi's OAuth files. The broker
runs as UID 1001 (`bandi`), starts the already configured official wrapper, and
exposes the standard newline-delimited MCP JSON-RPC stream on an AF_UNIX socket.
Linux `SO_PEERCRED` additionally rejects every peer UID except 1000.

Administrative installation (adjust `/opt/ralfloop` to the immutable installed
source path; do not point production at this worktree):

```sh
sudo groupadd --system --force ralf-mcp
sudo usermod -aG ralf-mcp sibilla-cumana
sudo install -d -o root -g root -m 0755 /opt/ralfloop/scripts
sudo install -o root -g root -m 0755 scripts/ralf_google_workspace_mcp_broker.py /opt/ralfloop/scripts/
sudo install -o root -g root -m 0644 deploy/systemd/ralf-google-workspace-mcp-broker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ralf-google-workspace-mcp-broker.service
```

The service does not copy tokens, expose secrets in environment variables, or
grant sudo. Keep bandi's OAuth paths at their existing restrictive permissions.
