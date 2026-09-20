#!/bin/sh
set -eu
PYTHON_BIN="${RALFLOOP_RUNTIME_PYTHON:-/home/sibilla-cumana/ralfloop_agent_scaffold/.venv/bin/python}"
ROOT="${RALFLOOP_MD_GOODIFY_ROOT:-/opt/ralfloop}"
export PYTHONPATH="${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
exec "${PYTHON_BIN}" "${ROOT}/scripts/ralf_md_goodify_mcp_server.py"
