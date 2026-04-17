#!/usr/bin/env python3
import argparse
import json
import os
import sys
import shutil
import subprocess
from pathlib import Path
from urllib import request, error

DEFAULT_URL = os.environ.get("RALFLOOP_TASKS_RUN_URL", "http://127.0.0.1:19090/tasks/run")

def post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read().decode("utf-8"))

def build_payload(prompt: str, args: argparse.Namespace) -> dict:
    cwd = os.getcwd()
    enriched_prompt = f"[WORKDIR={cwd}] {prompt}"
    wants_apply = bool(args.apply_back or args.apply_all or args.apply_file)
    return {
        "user_goal": enriched_prompt,
        "skill_context": args.skill_context,
        "extra_context": {
            "cwd": cwd,
            "workspace_root": cwd,
            "keep_sandbox": bool(wants_apply or args.preview_diff),
        },
        "planner_model_profile": args.planner_profile,
        "coder_model_profile": args.coder_profile,
        "judge_model_profile": args.judge_profile,
        "planner_model_name": args.planner_model,
        "coder_model_name": args.coder_model,
        "judge_model_name": args.judge_model,
        "planner_rag_collection": args.planner_rag,
        "coder_rag_collection": args.coder_rag,
        "judge_rag_collection": args.judge_rag,
        "internal_prompt_style": args.internal_prompt_style,
        "ocr_backend": args.ocr_backend,
        "ocr_model_name": args.ocr_model_name,
    }


def collect_sandbox_files(obj: dict, target_root: str, preview_root: str) -> list[tuple[str, str]]:
    workspace = str(obj.get("sandbox_workspace") or "").strip()
    if not workspace:
        return []

    root = Path(target_root).resolve()
    sandbox_root = Path(workspace).resolve()
    preview = Path(preview_root).resolve()
    preview.mkdir(parents=True, exist_ok=True)

    collected: list[tuple[str, str]] = []
    for art in obj.get("artifacts") or []:
        rel = str(art or "").strip()
        if not rel or rel.startswith("/") or rel.startswith("tmp/debug/"):
            continue
        src_path = (sandbox_root / rel).resolve()
        if not src_path.exists() or not src_path.is_file():
            continue
        real_path = (root / rel).resolve()
        if root not in real_path.parents:
            continue
        preview_path = (preview / rel).resolve()
        if preview not in preview_path.parents and preview_path != preview:
            continue
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, preview_path)
        collected.append((str(real_path), str(preview_path)))
    return collected


def select_preview_pairs(
    pairs: list[tuple[str, str]],
    target_root: str,
    requested_files: list[str] | None = None,
) -> tuple[list[tuple[str, str]], list[str]]:
    root = Path(target_root).resolve()
    requested = [str(item or "").strip() for item in (requested_files or []) if str(item or "").strip()]
    if not requested:
        return list(pairs), []

    by_rel: dict[str, tuple[str, str]] = {}
    for real_path, preview_path in pairs:
        rel_path = os.path.relpath(real_path, root)
        by_rel[rel_path] = (real_path, preview_path)

    selected = [by_rel[rel] for rel in requested if rel in by_rel]
    missing = [rel for rel in requested if rel not in by_rel]
    return selected, missing


def apply_preview_pairs(pairs: list[tuple[str, str]], target_root: str) -> list[str]:
    root = Path(target_root).resolve()
    copied: list[str] = []

    for real_path, preview_path in pairs:
        dst_path = Path(real_path).resolve()
        src_path = Path(preview_path).resolve()
        if root not in dst_path.parents:
            continue
        if not src_path.exists() or not src_path.is_file():
            continue
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dst_path)
        copied.append(str(dst_path))

    return copied

def render_diff_pairs(pairs: list[tuple[str, str]]) -> str:
    chunks: list[str] = []
    for real_path, preview_path in pairs:
        real_exists = Path(real_path).exists()
        if real_exists:
            cmd = ["git", "--no-pager", "diff", "--no-index", "--", real_path, preview_path]
        else:
            cmd = ["git", "--no-pager", "diff", "--no-index", "--", "/dev/null", preview_path]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        out = (proc.stdout or "") + (proc.stderr or "")
        if out.strip():
            chunks.append(out.rstrip())
    return "\n\n".join(chunks)

def print_human(obj: dict) -> None:
    for key in [
        "ok",
        "mode",
        "stop_reason",
        "current_role",
        "used_internal_prompt_style",
        "used_ocr_backend",
        "used_ocr_model_name",
    ]:
        if key in obj:
            print(f"{key}: {obj.get(key)}")
    for key in ["used_profiles", "used_models", "used_rag"]:
        if key in obj:
            print(f"{key}: {json.dumps(obj.get(key), ensure_ascii=False)}")
    print("\nfinal_answer:")
    print(obj.get("final_answer", ""))
    arts = obj.get("artifacts") or []
    if arts:
        print("\nartifacts:")
        for a in arts:
            print(a)

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt", nargs="+")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--preview-diff", action="store_true")
    parser.add_argument("--apply-back", action="store_true")
    parser.add_argument("--apply-all", action="store_true")
    parser.add_argument("--apply-file", action="append", default=[])
    parser.add_argument("--skill-context", default="")
    parser.add_argument("--planner-profile", default="generalist")
    parser.add_argument("--coder-profile", default="generalist")
    parser.add_argument("--judge-profile", default="generalist")
    parser.add_argument("--planner-model", default="qwen2.5:7b")
    parser.add_argument("--coder-model", default="qwen2.5:7b")
    parser.add_argument("--judge-model", default="qwen2.5:7b")
    parser.add_argument("--planner-rag", default="")
    parser.add_argument("--coder-rag", default="")
    parser.add_argument("--judge-rag", default="")
    parser.add_argument("--internal-prompt-style", default="standard")
    parser.add_argument("--ocr-backend", default="none")
    parser.add_argument("--ocr-model-name", default="")
    args = parser.parse_args()

    wants_apply = bool(args.apply_back or args.apply_all or args.apply_file)
    if wants_apply and not args.preview_diff:
        print("apply_requires_preview_diff", file=sys.stderr)
        return 2

    prompt = " ".join(args.prompt).strip()
    payload = build_payload(prompt, args)

    try:
        obj = post_json(args.url, payload)
    except error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print(body, file=sys.stderr)
        return 1
    except Exception as e:
        print(f"request_failed: {e}", file=sys.stderr)
        return 1

    copied = []
    preview_pairs = []
    diff_text = ""
    selected_pairs = []

    if obj.get("ok"):
        preview_dir = os.path.join(os.getcwd(), ".ralf_preview")
        preview_pairs = collect_sandbox_files(obj, os.getcwd(), preview_dir)
        if args.preview_diff:
            diff_text = render_diff_pairs(preview_pairs)
            obj["preview_diff"] = diff_text
            obj["preview_pairs"] = preview_pairs

        if wants_apply:
            requested_files = [] if (args.apply_back or args.apply_all) else list(args.apply_file)
            selected_pairs, missing = select_preview_pairs(preview_pairs, os.getcwd(), requested_files)
            obj["selected_apply_files"] = [
                os.path.relpath(real_path, os.getcwd())
                for real_path, _ in selected_pairs
            ]
            if missing:
                obj["apply_missing_files"] = missing
                if args.json:
                    print(json.dumps(obj, ensure_ascii=False, indent=2))
                else:
                    print_human(obj)
                    print("\npreview_diff:")
                    print(diff_text or "(nessuna differenza)")
                    print("\napply_missing_files:")
                    for rel in missing:
                        print(rel)
                return 2
            copied = apply_preview_pairs(selected_pairs, os.getcwd())
            obj["applied_back_files"] = copied

    if args.json:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        print_human(obj)
        if args.preview_diff:
            print("\npreview_diff:")
            print(diff_text or "(nessuna differenza)")
        if copied:
            print("\napplied_back_files:")
            for path in copied:
                print(path)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
