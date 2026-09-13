# DeepSeek V4.1 Flash CUDA sm_75 — Gate 4 validated

- Base branch before these fixes: `a8d3f7b8673adb5ba7bb89688263a29dc731817b`
- GPU: NVIDIA GeForce RTX 2070 / sm_75
- CUDA primitive oracle: 12/12 PASS
- router384: PASS
- Gate 3, 1 token: PASS
- Gate 4A, 2 tokens: PASS
- Gate 4B, 4 tokens: PASS
- Decode observed: ~0.31 tok/s
- EXT4 mpage warnings: 0
- GGUF metadata unchanged
- Production 19194 untouched

Patch application order after v41-cuda-scalar-graph.patch:
1. v41-router384.patch
2. v41-stream-selected-experts.patch
