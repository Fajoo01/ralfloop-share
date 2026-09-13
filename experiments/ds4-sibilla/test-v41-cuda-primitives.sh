#!/usr/bin/env bash
set -euo pipefail
PORT="${PORT:-/home/sibilla-cumana/src/ds4-main-lowvram-port}"
CUDA_ARCH="${CUDA_ARCH:-sm_75}"
cd "$PORT"
/usr/local/cuda/bin/nvcc -O2 -arch="$CUDA_ARCH" -std=c++17 -I. \
  -c "${TEST_SOURCE:?set TEST_SOURCE to test-v41-cuda-primitives.cu}" \
  -o tests/test_v41_cuda_primitives.o
/usr/local/cuda/bin/nvcc -O2 -arch="$CUDA_ARCH" \
  -o tests/test_v41_cuda_primitives tests/test_v41_cuda_primitives.o \
  ds4_help.o ds4_prompt_prefix.o linenoise.o ds4_gpu_args.o ds4.o ds4_image.o \
  ds4_distributed.o ds4_tp.o ds4_ssd.o ds4_cuda.o ds4_layer_pack.o ds4_engram.o \
  cuda/mmq/ds4_ggml_stubs.o cuda/mmq/ds4_mmq.o cuda/mmq/ds4_mmq_d2r.o \
  cuda/mmq/quantize.o cuda/mmq/mmid.o cuda/mmq/mmvq.o cuda/mmq/ds4_repack.o \
  -lm -Xcompiler -pthread -L/usr/local/cuda/lib64 -lcudart -lcublas
tests/test_v41_cuda_primitives
