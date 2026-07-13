#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="${HOME}/.local/bin"
TARGET="${TARGET_DIR}/ralf"
PYTHON="${ROOT}/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
  PYTHON="python3"
fi

mkdir -p "$TARGET_DIR"

cat > "$TARGET" <<EOF
#!/usr/bin/env bash
set -euo pipefail
ROOT="$ROOT"
PYTHON="$PYTHON"
export PYTHONPATH="\$ROOT\${PYTHONPATH:+:\$PYTHONPATH}"
exec "\$PYTHON" -m ralfloop_agent.cli.terminal_chat "\$@"
EOF

chmod 0755 "$TARGET"

printf 'installed: %s\n' "$TARGET"
case ":$PATH:" in
  *":$TARGET_DIR:"*) ;;
  *) printf 'warning: %s not in PATH\n' "$TARGET_DIR" >&2 ;;
esac
