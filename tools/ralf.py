#!/usr/bin/env python3
import argparse
import json
import os
import sys
import shutil
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
    return {
        "user_goal": enriched_prompt,
        "skill_context": args.skill_context,
        "extra_context": {
            "cwd": cwd,
            "workspace_root": cwd,
            "keep_sandbox": bool(args.apply_back),
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

def apply_back_from_sandbox(obj: dict, target_root: str) -> list[str]:
    workspace = str(obj.get("sandbox_workspace") or "").strip()
    if not workspace:
        return []

    root = Path(target_root).resolve()
    sandbox_root = Path(workspace).resolve()
    copied: list[str] = []

    for art in obj.get("artifacts") or []:
        rel = str(art or "").strip()
        if not rel or rel.startswith("/") or rel.startswith("tmp/debug/"):
            continue
        src_path = (sandbox_root / rel).resolve()
        if not src_path.exists() or not src_path.is_file():
            continue
        dst_path = (root / rel).resolve()
        if root not in dst_path.parents:
            continue
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dst_path)
        copied.append(str(dst_path))

    return copied


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
    parser.add_argument("--apply-back", action="store_true")
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
    if args.apply_back and obj.get("ok"):
        copied = apply_back_from_sandbox(obj, os.getcwd())
        obj["applied_back_files"] = copied

    if args.json:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        print_human(obj)
        if copied:
            print("\napplied_back_files:")
            for path in copied:
                print(path)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
