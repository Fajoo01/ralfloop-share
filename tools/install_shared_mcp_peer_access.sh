#!/usr/bin/env bash
set -euo pipefail

PY=/home/sibilla-cumana/ralfloop_agent_scaffold/.venv/bin/python
BROKER=/home/sibilla-cumana/ralf-memory-rag/current/scripts/ralf_arci_mcp_broker.py
DROPIN=99-ralf-mcp-shared-peers.conf

[[ $(id -u) -eq 0 ]] || { echo 'run as root' >&2; exit 2; }
[[ -x "$PY" && -f "$BROKER" ]] || { echo 'shared broker missing' >&2; exit 3; }
"$PY" "$BROKER" --help 2>&1 | grep -q -- '--allow-group' || { echo 'broker lacks group support' >&2; exit 4; }
getent group ralf-mcp >/dev/null
id bandi | grep -q 'ralf-mcp'
id sibilla-cumana | grep -q 'ralf-mcp'

write_dropin() {
  local unit=$1 socket=$2 command=$3 idle=${4:-60}
  local dir="/etc/systemd/system/${unit}.d"
  install -d -m 0755 "$dir"
  cat >"$dir/$DROPIN" <<EOF
[Service]
Group=ralf-mcp
RuntimeDirectoryMode=2770
ExecStart=
ExecStart=$PY $BROKER --socket $socket --allow-group ralf-mcp --command $command --idle-timeout $idle
EOF
}
write_dropin ralf-amule-mcp-broker.service /run/ralf-amule-mcp/mcp.sock /home/sibilla-cumana/ralfloop-bottazzi/current/scripts/ralf_amule_mcp_server.py 60
write_dropin ralf-arci-mcp-broker.service /run/ralf-arci-mcp/mcp.sock /home/sibilla-cumana/ralfloop-bottazzi/current/scripts/ralf_arci_rest_mcp_server.py 60
write_dropin ralf-canva-mcp-broker.service /run/ralf-canva-mcp/mcp.sock /home/sibilla-cumana/ralfloop-bottazzi/current/scripts/ralf_canva_mcp_server.py 300
write_dropin ralf-meta-social-mcp-broker.service /run/ralf-meta-social-mcp/mcp.sock /home/sibilla-cumana/ralfloop-bottazzi/current/scripts/ralf_meta_social_mcp_server.py 60
write_dropin ralf-md-goodify-mcp-broker.service /run/ralf-md-goodify-mcp/mcp.sock /home/sibilla-cumana/ralf-md-goodify-mcp/current/scripts/ralf_md_goodify_mcp_server.py 90
echo "Environment=PYTHONPATH=/home/sibilla-cumana/ralf-md-goodify-mcp/current" >> /etc/systemd/system/ralf-md-goodify-mcp-broker.service.d/99-ralf-mcp-shared-peers.conf
write_dropin ralf-bandi-mcp-broker.service /run/ralf-bandi-mcp/mcp.sock /home/sibilla-cumana/ralfloop-production/current/scripts/ralf_bandi_mcp_server.py 300
write_dropin ralf-abc-relation-mcp-broker.service /run/ralf-abc-relation-mcp/mcp.sock /home/sibilla-cumana/ralf-abc-relation-mcp/current/scripts/ralf_abc_relation_mcp_server.py 120

systemctl daemon-reload
for unit in \
  ralf-amule-mcp-broker.service ralf-arci-mcp-broker.service \
  ralf-canva-mcp-broker.service ralf-meta-social-mcp-broker.service \
  ralf-md-goodify-mcp-broker.service ralf-bandi-mcp-broker.service \
  ralf-abc-relation-mcp-broker.service; do
  systemctl restart "$unit"
  systemctl is-active --quiet "$unit"
done

echo 'shared MCP peer access installed'
