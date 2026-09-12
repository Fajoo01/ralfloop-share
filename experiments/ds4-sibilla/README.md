# DwarfStar / ds4 on Sibilla

Sibilla already has a working DwarfStar/`ds4` CUDA SSD-streaming runtime. Do **not** create a second installation or download another model before auditing the existing service.

## Confirmed runtime (2026-09-12)

Host/GPU:

- Linux x86_64
- NVIDIA GeForce RTX 2070, 8 GiB VRAM, Turing / `sm_75`
- NVIDIA driver 535.309.01, reported CUDA 12.2
- model stored on Sibilla local storage

Existing source/runtime:

```text
/home/sibilla-cumana/src/ds4-cuda-stream-pr739/ds4-server
```

System service:

```text
bottazzi-ds4.service
```

Wrapper:

```text
/home/sibilla-cumana/.local/bin/bottazzi-ds4-server
```

Model:

```text
/home/sibilla-cumana/Dati/ralfloop-models/deepseek-v4-flash-pr739/gguf/DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf
```

Observed command line:

```text
ds4-server \
  -m .../DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf \
  --backend cuda \
  --ssd-streaming \
  --cuda-low-vram-stream \
  --ssd-streaming-cold \
  --ctx 16384 \
  --prefill-chunk 128 \
  --threads 8 \
  --tokens 256 \
  --host 127.0.0.1 \
  --port 19194
```

Systemd drop-in:

```text
DS4_CUDA_LOW_VRAM_STAGE_MB=640
```

Observed startup memory plan:

```text
KV 0.23 GiB (raw 0.02 + compressed 0.21)
resident model 0.99 GiB
planned total 1.22 GiB
ctx=16384
prefill_cap=128
raw_kv_rows=256
compressed_kv_rows=4098
backend=cuda
```

The process was observed using about 2238 MiB of VRAM in `nvidia-smi`. The difference from the printed 1.22 GiB plan is expected to include CUDA/runtime allocations and other device-side overhead; use measured `nvidia-smi` as the operational ceiling, not only the planner number.

A startup log line reported:

```text
CUDA host registration skipped: out of memory
```

but startup continued successfully, CUDA initialized, context buffers were created, and the server listened on `127.0.0.1:19194`. Treat this as a degraded/optional optimization path unless later evidence shows inference failure.

## Important: VRAM coexistence

At the time of the audit the RTX 2070 had roughly:

```text
ds4-server       ~2238 MiB
AgentCPM llama    ~3718 MiB
Xorg              ~140 MiB
```

Total GPU use was about 6103 MiB / 8192 MiB before additional workloads. Fish Speech demonstrated that this remaining headroom is tight enough to produce CUDA OOM when another ~1.7 GiB model is loaded concurrently.

Therefore the immediate optimization question is **not** whether DwarfStar can run on Sibilla — it already does. The useful questions are:

1. what exact ds4 commit / PR739 code is deployed;
2. whether the current cold-stream / low-VRAM configuration is optimal;
3. how much VRAM DwarfStar can relinquish while keeping acceptable latency;
4. whether AgentCPM and DwarfStar should be scheduled rather than kept resident together;
5. whether a newer upstream ds4 can replace the PR739 checkout without regressing Turing support.

## Git ownership

The ds4 checkout belongs to the `sibilla-cumana` account. Running `git` there as `bandi` triggers Git's `dubious ownership` protection. Do **not** blindly add it as a global safe directory. For read-only repository inspection use the repository owner instead, for example:

```bash
sudo -u sibilla-cumana -H git -C /home/sibilla-cumana/src/ds4-cuda-stream-pr739 status --short --branch
sudo -u sibilla-cumana -H git -C /home/sibilla-cumana/src/ds4-cuda-stream-pr739 log -1 --oneline
sudo -u sibilla-cumana -H git -C /home/sibilla-cumana/src/ds4-cuda-stream-pr739 remote -v
```

## Audit runner

Run:

```bash
bash experiments/ds4-sibilla/smoke.sh
```

Despite the historical filename, the script is now non-destructive by default: it audits the existing deployment and does not stop services, rebuild ds4, download models, or start a second server.

Optional mutations/rebuilds should only be added after the deployed commit and current performance are recorded.