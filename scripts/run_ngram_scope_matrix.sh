#!/usr/bin/env bash
set -euo pipefail

root_dir=$(cd "$(dirname "$0")/.." && pwd)
out_dir="$root_dir/benchmarks/qwen35/selective-ngram"
server=/home/sibilla-cumana/src/llama.cpp/build-cpu/bin/llama-server
model=/home/sibilla-cumana/Dati/ralfloop-models/qwen3.5-35b-a3b/Qwen3.5-35B-A3B-Q4_K_M.gguf
port=19196
mkdir -p "$out_dir"

cleanup() {
    vpnpc run sibilla -- pkill -TERM -f "^$server .*--port $port " >/dev/null 2>&1 || true
    if [[ -n "${vpnpc_pid:-}" ]]; then
        wait "$vpnpc_pid" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

read -r -a scenarios <<<"${SCENARIOS:-exact_repeat near_repeat same_project_different_file same_domain_different_host unrelated_project novel_prose}"
read -r -a scopes <<<"${SCOPES:-off global project session}"
for scenario in "${scenarios[@]}"; do
    for scope in "${scopes[@]}"; do
        log="$out_dir/${scenario}-${scope}.server.log"
        result="$out_dir/${scenario}-${scope}.jsonl"
        spec_args=()
        if [[ "$scope" != off ]]; then
            spec_args=(
                --spec-type ngram-mod
                --spec-ngram-mod-n-match 24
                --spec-ngram-mod-n-min 48
                --spec-ngram-mod-n-max 64
            )
        fi
        vpnpc run sibilla -- "$server" \
            --model "$model" --alias qwen3.5-35b-a3b \
            --host 127.0.0.1 --port "$port" --ctx-size 4096 --parallel 1 \
            --threads 6 --threads-batch 6 --batch-size 512 --ubatch-size 256 \
            --cache-type-k q8_0 --cache-type-v q8_0 --cache-ram 0 \
            --ctx-checkpoints 0 --cache-reuse 0 \
            --flash-attn auto --no-cache-prompt --metrics --slots --perf --mmap \
            --no-warmup --offline --no-webui --fit off "${spec_args[@]}" \
            >"$log" 2>&1 &
        vpnpc_pid=$!
        ready=0
        for _ in $(seq 1 120); do
            if curl -fsS --max-time 1 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
                ready=1
                break
            fi
            sleep 1
        done
        if [[ "$ready" != 1 ]]; then
            echo "server_start_failed:$scenario:$scope" >&2
            exit 1
        fi
        python3 "$root_dir/scripts/bottazzi_ngram_generalization.py" \
            --scenario "$scenario" --scope "$scope" --repeat "${REPEAT:-2}" \
            --lookup-ns 24.150 >"$result"
        cleanup
        unset vpnpc_pid
    done
done
