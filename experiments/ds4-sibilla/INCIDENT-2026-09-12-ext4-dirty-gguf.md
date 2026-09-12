# 2026-09-12 DS4 low-VRAM smoke: EXT4 dirty-page incident

## Summary

The isolated `ds4-main-lowvram-port` smoke successfully reached the OpenAI-compatible chat endpoint and returned a valid completion, but after inference Sibilla became effectively unusable because the kernel accumulated about 51.8 GB of dirty page-cache pages on the DeepSeek GGUF inode.

The affected file was:

`/home/sibilla-cumana/Dati/ralfloop-models/deepseek-v4-flash-pr739/gguf/DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf`

Observed inode: `2883591`, size `86720111488` bytes, filesystem `/dev/nvme0n1p1` (ext4).

Kernel symptoms included repeated:

`EXT4-fs warning ... mpage_prepare_extent_to_map ... inode #2883591 ... page ... does not have buffers attached`

and system-wide `balance_dirty_pages` stalls. Chrome/crashpad processes were victims of the global dirty-page throttling, not the root cause.

After reboot, `Dirty` returned to tens of MB, `Writeback` to zero, swap to unused, and no new EXT4 warnings appeared during normal production DS4 startup.

## Source comparison

Both the modern port and the production tree already use:

`cudaHostRegisterMapped | cudaHostRegisterReadOnly`

for model-range and whole-model registration, so the incident was **not** caused by accidentally dropping `cudaHostRegisterReadOnly` during the port.

However, the NVIDIA Linux 535 driver family has an unpin path (`os_unlock_user_pages`) that marks pinned user pages dirty during unregister. Public kernel/NVIDIA traces show this exact family of EXT4 warnings after NVIDIA pinned-page cleanup. Therefore file-backed GGUF pages must not be passed through CUDA host registration in the low-VRAM SSD staging path on Sibilla.

## Mitigation

`patch-lowvram-no-file-host-register.sh` changes only the isolated modern port and:

1. disables per-range `cudaHostRegister` while `g_cuda_low_vram_stream` is active;
2. skips whole-model/no-copy `cudaHostRegister` when low-VRAM streaming is active and a model fd is present;
3. leaves the existing fd-backed bounded staging path as the data source;
4. does not touch the live production checkout or service.

## Safety status

Do not run another inference smoke until all of the following hold:

- the mitigation patch is applied to `/home/sibilla-cumana/src/ds4-main-lowvram-port`;
- `git diff --check` is clean;
- the port rebuilds with `CUDA_ARCH=sm_75`;
- CLI prerequisite tests still pass;
- a startup-only smoke shows no growth in `/proc/meminfo:Dirty` and no EXT4 warnings;
- inference is reintroduced with a very short request while monitoring Dirty/Writeback and dmesg.

Production `bottazzi-ds4.service` must remain untouched during these tests.
