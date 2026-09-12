#!/usr/bin/env python3
"""Port PR739 selected-expert cache semantics onto the isolated modern tree."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import tempfile
from pathlib import Path


PR739 = "05632d23ba8c8cb943bdb51bb83b9dc2d8698c7d"
EXPECTED_MODERN_SHA256 = (
    "e181f42c4aed08a1067726a4080edc13484226d5f24b4325f43b5ff5ce011c22"
)
PORT_MARKER = "DS4_STREAM_SELECTED_PERSISTENT_CACHE_PORT"


def die(message: str) -> None:
    raise SystemExit(message)


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(args, check=check, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        die(f"{label}: expected one anchor, found {count}")
    return text.replace(old, new, 1)


def section(text: str, start: str, end: str, label: str) -> str:
    start_at = text.find(start)
    if start_at < 0:
        die(f"{label}: start anchor missing")
    end_at = text.find(end, start_at)
    if end_at < 0:
        die(f"{label}: end anchor missing")
    return text[start_at:end_at]


def resolve_all_theirs(text: str) -> tuple[str, int]:
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    conflicts = 0
    i = 0
    while i < len(lines):
        if not lines[i].startswith("<<<<<<< "):
            out.append(lines[i])
            i += 1
            continue
        conflicts += 1
        i += 1
        while i < len(lines) and not lines[i].startswith("======="):
            i += 1
        if i == len(lines):
            die("unterminated merge conflict before separator")
        i += 1
        while i < len(lines) and not lines[i].startswith(">>>>>>> "):
            out.append(lines[i])
            i += 1
        if i == len(lines):
            die("unterminated merge conflict after separator")
        i += 1
    return "".join(out), conflicts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--modern-source",
        default="/home/sibilla-cumana/src/ds4-main-lowvram-port/ds4_cuda.cu",
    )
    parser.add_argument(
        "--production-repo",
        default="/home/sibilla-cumana/src/ds4-cuda-stream-pr739",
    )
    parser.add_argument("--output")
    args = parser.parse_args()

    modern_path = Path(args.modern_source)
    production_repo = Path(args.production_repo)
    production_path = production_repo / "ds4_cuda.cu"
    output_path = Path(args.output) if args.output else modern_path

    modern = modern_path.read_text()
    if PORT_MARKER in modern:
        print(f"already_patched: {modern_path}")
        return
    modern_sha = hashlib.sha256(modern.encode()).hexdigest()
    if modern_sha != EXPECTED_MODERN_SHA256:
        die(f"unexpected modern ds4_cuda.cu sha256: {modern_sha}")
    production = production_path.read_text()

    base = run("git", "-C", str(production_repo), "show",
               f"{PR739}^:ds4_cuda.cu").stdout
    theirs = run("git", "-C", str(production_repo), "show",
                 f"{PR739}:ds4_cuda.cu").stdout
    with tempfile.TemporaryDirectory(prefix="ds4-pr739-port-") as tmp:
        tmp_path = Path(tmp)
        ours_file = tmp_path / "ours.cu"
        base_file = tmp_path / "base.cu"
        theirs_file = tmp_path / "theirs.cu"
        ours_file.write_text(modern)
        base_file.write_bytes(base)
        theirs_file.write_bytes(theirs)
        merged_run = run("git", "merge-file", "-p", str(ours_file),
                         str(base_file), str(theirs_file), check=False)
    if merged_run.returncode < 0 or merged_run.returncode > 127:
        die(f"git merge-file failed: {merged_run.stderr.decode().strip()}")
    merged, conflicts = resolve_all_theirs(merged_run.stdout.decode())
    if conflicts != 6:
        die(f"unexpected PR739 conflict count: {conflicts} (wanted 6)")

    # Current production carries the complete four-buffer selected-upload
    # lifecycle above PR739. Port only that named region; keep modern cache
    # sizing, GLM/V4.1, multi-GPU, placement, and decode-map logic.
    stage_support = section(
        production,
        "static void cuda_stream_selected_stage_release(void) {",
        "static uint64_t cuda_model_cache_limit_bytes(void);",
        "production selected stage support",
    )
    modern_stage = section(
        merged,
        "static void cuda_stream_selected_stage_release(void) {",
        "static uint64_t g_model_default_cache_limit;",
        "modern selected stage support",
    )
    merged = replace_once(merged, modern_stage, stage_support,
                          "selected stage lifecycle")

    old_stage_globals = """static void *g_stream_selected_stage_raw[4];
static void *g_stream_selected_stage[4];
static cudaEvent_t g_stream_selected_stage_event[4];
static uint64_t g_stream_selected_stage_bytes;
static cudaStream_t g_stream_selected_upload_stream;
"""
    new_stage_globals = """static void *g_stream_selected_stage_raw[4];
static void *g_stream_selected_stage[4];
static cudaEvent_t g_stream_selected_stage_event[4];
static uint64_t g_stream_selected_stage_bytes;
static uint64_t g_stream_selected_stage_next;
static int g_stream_selected_stage_recorded[4];
"""
    merged = replace_once(merged, old_stage_globals, new_stage_globals,
                          "selected stage globals")

    # Newer modern MMQ prefill block conflicts with PR739 because both changed
    # pointer selection. Keep modern block and route its two expert-indexed
    # calls through PR739 physical slots.
    mmq_start = "    if (iq2_path && n_tokens > 1u && !owned_filtered && cuda_use_mmq()) {"
    mmq_end = "    /* Q4_K routed-MoE dispatch:"
    mmq = section(merged, mmq_start, mmq_end, "modern IQ2 MMQ block")
    if mmq.count("(const int32_t *)selected->ptr") != 2:
        die("modern IQ2 MMQ block: selected pointer count changed")
    mmq = mmq.replace("(const int32_t *)selected->ptr",
                      "(const int32_t *)mmq_selected->ptr")
    mmq = replace_once(
        mmq,
        "(int)n_tokens, (int)n_total_expert, (int)n_expert,",
        "(int)n_tokens, (int)mmq_expert_count, (int)n_expert,",
        "IQ2 MMQ gate/up expert count",
    )
    mmq = replace_once(
        mmq,
        "(int)n_assignments, (int)n_total_expert,",
        "(int)n_assignments, (int)mmq_expert_count,",
        "IQ2 MMQ down expert count",
    )
    merged = replace_once(
        merged,
        section(merged, mmq_start, mmq_end, "merged IQ2 MMQ block"),
        mmq,
        "IQ2 MMQ persistent dispatch",
    )

    duplicate_mxfp4_stream = """        if (!gate_w || !up_w || !down_w || weight_experts == 0u) return 0;

        const cudaStream_t stream =
            n_tokens == 1u ? cuda_decode_stream() : (cudaStream_t)0;
        int rc = -1;
"""
    merged = replace_once(
        merged,
        duplicate_mxfp4_stream,
        """        if (!gate_w || !up_w || !down_w || weight_experts == 0u) return 0;
        int rc = -1;
""",
        "duplicate MXFP4 stream declaration",
    )

    # PR738's asynchronous selected-id handoff is parent context of PR739,
    # so a three-way merge of PR739 alone cannot introduce it into modern.
    # Replace this one unchanged API implementation from current production.
    begin_start = "static int cuda_stream_selected_cache_begin_load("
    begin_end = "__device__ __forceinline__ static float glm_rope_yarn_corr_factor_dev("
    production_begin = section(
        production, begin_start, begin_end, "production selected load",
    )
    merged_begin = section(
        merged, begin_start, begin_end, "modern selected load",
    )
    merged = replace_once(merged, merged_begin, production_begin,
                          "selected load lifecycle")

    marker_anchor = "/* The compact cache is one layer wide. This second bounded cache keeps\n"
    merged = replace_once(
        merged,
        marker_anchor,
        f"/* {PORT_MARKER}\n+ * Semantic port of PR739 selected-expert reuse onto modern upstream.\n+ * The compact cache is one layer wide. This second bounded cache keeps\n",
        "port marker",
    )

    required = {
        "port marker": PORT_MARKER,
        "persistent direct": "persistent_direct",
        "persistent load": "cuda_stream_persistent_cache_load_selected",
        "ready wait": "cuda_stream_selected_cache_wait_ready",
        "compute done": "cuda_stream_selected_cache_record_compute_done",
        "four-buffer reuse": "g_stream_selected_stage_recorded[4]",
        "host-register guard": "DS4_LOW_VRAM_NO_FILE_HOST_REGISTER",
    }
    for label, needle in required.items():
        if needle not in merged:
            die(f"missing {label}: {needle}")
    if any(line.startswith(("<<<<<<< ", "=======", ">>>>>>> "))
           for line in merged.splitlines()):
        die("unresolved merge marker remains")
    if merged.count("static cudaStream_t g_stream_selected_upload_stream;") != 1:
        die("selected upload stream declaration count is not one")
    if merged.count("typedef struct ds4_gpu_stream_expert_table {") != 1:
        die("stream expert table definition count is not one")

    output_path.write_text(merged)
    print(f"ported: {modern_path} -> {output_path}")
    print(f"resolved_conflicts={conflicts}")
    print(f"output_sha256={hashlib.sha256(merged.encode()).hexdigest()}")


if __name__ == "__main__":
    main()
