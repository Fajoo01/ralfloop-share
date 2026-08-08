#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON="${RALF_PYTHON:-$ROOT/.venv/bin/python}"

if [[ ! -x "$PYTHON" ]]; then
  echo "ralf_python_unavailable" >&2
  exit 1
fi

if [[ $# -lt 1 ]]; then
  echo "usage: $0 {start|stop|status|health} [--dry-run]" >&2
  exit 2
fi

case "$1" in
  start|stop|status|health) ;;
  *)
    echo "unsupported_engine_action" >&2
    exit 2
    ;;
esac

cd -- "$ROOT"
exec "$PYTHON" -m ralfloop_agent.providers.llama_cpp_server "$@"
