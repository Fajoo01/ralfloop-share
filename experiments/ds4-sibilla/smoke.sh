#!/usr/bin/env bash
set -euo pipefail

DS4_SRC="${DS4_SRC:-$HOME/src/ds4}"
DS4_REPO="${DS4_REPO:-https://github.com/antirez/ds4.git}"
DS4_MODEL="${DS4_MODEL:-}"
DS4_CACHE="${DS4_CACHE:-3GB}"
DS4_CTX="${DS4_CTX:-2048}"
DS4_TOKENS="${DS4_TOKENS:-8}"
DS4_PROMPT="${DS4_PROMPT:-Rispondi soltanto: OK}"
DS4_CUDA_ARCH="${DS4_CUDA_ARCH:-sm_75}"
DS4_UPDATE="${DS4_UPDATE:-0}"

say() { printf '\n=== %s ===\n' "$*"; }

say "HOST"
uname -a || true
printf 'date: '; date -Is || true

say "CPU / RAM"
command -v lscpu >/dev/null && lscpu | sed -n '1,25p' || true
command -v free >/dev/null && free -h || true
command -v swapon >/dev/null && swapon --show || true

say "NVIDIA"
if ! command -v nvidia-smi >/dev/null; then
  echo "ERROR: nvidia-smi not found. CUDA ds4 test cannot proceed." >&2
  exit 2
fi
nvidia-smi
nvidia-smi --query-gpu=name,compute_cap,memory.total,memory.free,driver_version --format=csv,noheader || true

say "CUDA TOOLKIT"
if command -v nvcc >/dev/null; then
  nvcc --version
else
  echo "ERROR: nvcc not found. Install/activate a CUDA toolkit compatible with the installed driver." >&2
  exit 3
fi

say "STORAGE"
df -hT "$HOME" || true
if command -v lsblk >/dev/null; then
  lsblk -o NAME,MODEL,SIZE,TYPE,FSTYPE,MOUNTPOINTS || true
fi

say "DS4 SOURCE"
mkdir -p "$(dirname "$DS4_SRC")"
if [[ ! -d "$DS4_SRC/.git" ]]; then
  git clone "$DS4_REPO" "$DS4_SRC"
elif [[ "$DS4_UPDATE" == "1" ]]; then
  git -C "$DS4_SRC" fetch --all --prune
  git -C "$DS4_SRC" pull --ff-only
else
  echo "Using existing checkout: $DS4_SRC"
  echo "Set DS4_UPDATE=1 to fast-forward it before building."
fi

git -C "$DS4_SRC" status --short --branch
git -C "$DS4_SRC" rev-parse HEAD

say "BUILD CUDA ${DS4_CUDA_ARCH}"
make -C "$DS4_SRC" cuda CUDA_ARCH="$DS4_CUDA_ARCH"

DS4_BIN="$DS4_SRC/ds4"
if [[ ! -x "$DS4_BIN" ]]; then
  echo "ERROR: build completed but $DS4_BIN is not executable." >&2
  exit 4
fi

say "VERIFY STREAMING FLAGS"
HELP="$($DS4_BIN --help 2>&1 || true)"
printf '%s\n' "$HELP" | grep -E -- '--ssd-streaming|--ssd-streaming-cache-experts' || {
  echo "ERROR: expected SSD-streaming flags not found in this ds4 build." >&2
  exit 5
}

if [[ -z "$DS4_MODEL" ]]; then
  say "NO MODEL: STOPPING SAFELY"
  echo "Diagnostics/build completed."
  echo "No large model was downloaded and no inference was started."
  echo "Set DS4_MODEL=/path/to/ds4flash.gguf to run the guarded smoke test."
  exit 0
fi

if [[ ! -f "$DS4_MODEL" ]]; then
  echo "ERROR: DS4_MODEL does not exist: $DS4_MODEL" >&2
  exit 6
fi

say "MODEL"
ls -lh "$DS4_MODEL"
df -hT "$DS4_MODEL" || true

say "PRE-RUN GPU STATE"
nvidia-smi

say "SMOKE TEST"
echo "cache=$DS4_CACHE ctx=$DS4_CTX tokens=$DS4_TOKENS"

# Conservative defaults for Sibilla's 8 GiB RTX 2070.
# Do not pass --gpu-vram together with --ssd-streaming here.
set +e
DS4_CUDA_MMQ="${DS4_CUDA_MMQ:-0}" \
"$DS4_BIN" \
  --cuda \
  -m "$DS4_MODEL" \
  --ssd-streaming \
  --ssd-streaming-cache-experts "$DS4_CACHE" \
  --ctx "$DS4_CTX" \
  --tokens "$DS4_TOKENS" \
  --nothink \
  -p "$DS4_PROMPT"
rc=$?
set -e

say "POST-RUN GPU STATE"
nvidia-smi || true

echo "ds4 exit code: $rc"
exit "$rc"
