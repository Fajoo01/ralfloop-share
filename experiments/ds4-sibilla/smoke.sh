#!/usr/bin/env bash
set -u

DS4_SRC="${DS4_SRC:-/home/sibilla-cumana/src/ds4-cuda-stream-pr739}"
DS4_BIN="${DS4_BIN:-$DS4_SRC/ds4-server}"
DS4_SERVICE="${DS4_SERVICE:-bottazzi-ds4.service}"
DS4_WRAPPER="${DS4_WRAPPER:-/home/sibilla-cumana/.local/bin/bottazzi-ds4-server}"
DS4_MODEL="${DS4_MODEL:-/home/sibilla-cumana/Dati/ralfloop-models/deepseek-v4-flash-pr739/gguf/DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf}"
DS4_OWNER="${DS4_OWNER:-sibilla-cumana}"

section() { printf '\n=== %s ===\n' "$*"; }

section "HOST"
uname -a || true
printf 'date: '; date -Is || true

section "RAM / SWAP"
free -h || true
swapon --show || true

section "GPU"
nvidia-smi || true
nvidia-smi --query-gpu=name,compute_cap,memory.total,memory.used,memory.free,driver_version --format=csv,noheader 2>/dev/null || true

section "GPU COMPUTE PROCESSES"
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader 2>/dev/null || true

section "DS4 PROCESS"
pgrep -af 'ds4-server' || true

section "DS4 FILES"
printf 'source:  %s\n' "$DS4_SRC"
printf 'binary:  %s\n' "$DS4_BIN"
printf 'wrapper: %s\n' "$DS4_WRAPPER"
printf 'model:   %s\n' "$DS4_MODEL"
ls -ld "$DS4_SRC" 2>/dev/null || true
ls -lh "$DS4_BIN" "$DS4_WRAPPER" "$DS4_MODEL" 2>/dev/null || true

section "DS4 GIT (AS REPOSITORY OWNER)"
if [[ -d "$DS4_SRC/.git" ]]; then
  if command -v sudo >/dev/null 2>&1; then
    sudo -u "$DS4_OWNER" -H git -C "$DS4_SRC" remote -v || true
    sudo -u "$DS4_OWNER" -H git -C "$DS4_SRC" branch --show-current || true
    sudo -u "$DS4_OWNER" -H git -C "$DS4_SRC" rev-parse HEAD || true
    sudo -u "$DS4_OWNER" -H git -C "$DS4_SRC" log -1 --oneline --decorate || true
    sudo -u "$DS4_OWNER" -H git -C "$DS4_SRC" status --short --branch || true
  else
    echo "sudo unavailable; skipping owner-safe git inspection"
  fi
else
  echo "not a git checkout: $DS4_SRC"
fi

section "SYSTEMD SERVICE"
sudo systemctl --no-pager --full status "$DS4_SERVICE" || true

section "SYSTEMD UNIT + DROP-INS"
sudo systemctl cat "$DS4_SERVICE" || true

section "WRAPPER"
sudo sed -n '1,240p' "$DS4_WRAPPER" 2>/dev/null || true

section "RECENT DS4 LOG"
sudo journalctl -u "$DS4_SERVICE" -n 120 --no-pager 2>/dev/null || true

section "LISTEN SOCKET"
ss -ltnp 2>/dev/null | grep -E '(:19194|ds4)' || true

section "MODEL STORAGE"
if [[ -e "$DS4_MODEL" ]]; then
  df -hT "$DS4_MODEL" || true
  findmnt -T "$DS4_MODEL" -o TARGET,SOURCE,FSTYPE,OPTIONS 2>/dev/null || true
else
  echo "model path missing: $DS4_MODEL"
fi

section "DONE"
echo "Read-only audit complete. No services were stopped, no models were downloaded, and no second ds4 process was started."
