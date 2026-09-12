# Sibilla ds4 runtime observation — 2026-09-12

Observed production process:

```text
/home/sibilla-cumana/src/ds4-cuda-stream-pr739/ds4-server
```

Model:

```text
/home/sibilla-cumana/Dati/ralfloop-models/deepseek-v4-flash-pr739/gguf/DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf
```

Arguments:

```text
--backend cuda
--ssd-streaming
--cuda-low-vram-stream
--ssd-streaming-cold
--ctx 16384
--prefill-chunk 128
--threads 8
--tokens 256
--host 127.0.0.1
--port 19194
```

Systemd service:

```text
bottazzi-ds4.service
```

Drop-in:

```text
DS4_CUDA_LOW_VRAM_STAGE_MB=640
```

Startup planner output:

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

Observed GPU residency from `nvidia-smi`:

```text
ds4-server     ~2238 MiB
AgentCPM       ~3718 MiB
Xorg            ~140 MiB
GPU total       8192 MiB
GPU used        ~6103 MiB
```

GPU/driver:

```text
NVIDIA GeForce RTX 2070
Driver 535.309.01
CUDA reported by nvidia-smi: 12.2
```

Notable startup message:

```text
CUDA host registration skipped: out of memory
```

This did not prevent successful initialization: ds4 subsequently prepared CUDA model mappings, created context buffers and listened on `127.0.0.1:19194`.

Operational conclusion: ds4 / DeepSeek V4 Flash is already running successfully on Sibilla. The next work should benchmark and tune the existing PR739 low-VRAM/cold-stream implementation rather than create a parallel installation.