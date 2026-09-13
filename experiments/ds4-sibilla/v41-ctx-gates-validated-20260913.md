# DeepSeek V4.1 Flash CUDA sm_75 — context gates

- GPU: NVIDIA GeForce RTX 2070 / 8 GiB / sm_75
- ctx 4096: PASS, stage=640 MiB, reserve=512 MiB
- ctx 8192: PASS, stage=640 MiB, reserve=512 MiB
- ctx 16384: PASS, stage=640 MiB, reserve=1152 MiB
- ctx 16384 static context buffers: 1189.09 MiB
- ctx 16384 persistent model cache: 3.26 GiB
- ctx 16384 decode observed: ~0.29 tok/s
- ctx 16384 selected-expert upload: 24.47 GiB
- ctx 16384 low-VRAM staging: 67.80 GiB
- EXT4 mpage warnings: 0
- model inode/size/mtime/ctime unchanged
- production ds4 endpoint 19194 untouched

Recommended RTX 2070 production profile:
- ctx <= 8192: DS4_CUDA_LOW_VRAM_STAGE_MB=640, DS4_CUDA_LOW_VRAM_RESERVE_MB=512
- ctx = 16384: DS4_CUDA_LOW_VRAM_STAGE_MB=640, DS4_CUDA_LOW_VRAM_RESERVE_MB=1152
