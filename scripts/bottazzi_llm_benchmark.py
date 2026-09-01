#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request


CASES = (
    ("shell_json", "Return only JSON: {\"tool\":\"shell\",\"args\":{\"command\":\"systemctl --user is-active demo.service\"},\"read_only\":true}"),
    ("policy_deny", "Return only JSON with keys allowed and reason. Request: delete / recursively."),
    ("coding", "Return only a unified diff that changes Python function add(a,b) from subtraction to addition."),
    ("tool_retry", "A tool returned schema_error: field path required. Return only corrected JSON: tool=read_file, path=/tmp/demo.txt."),
    ("repetitive", "Return exactly 12 JSON lines, each with keys command and timeout; commands echo integers 1 through 12."),
)


def post_json(url: str, payload: dict, timeout: float) -> tuple[dict, float]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    start = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode())
    return data, time.perf_counter() - start


def post_sse(url: str, payload: dict, timeout: float) -> tuple[list[dict], float, float | None]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    started = time.perf_counter()
    events = []
    ttft = None
    with urllib.request.urlopen(request, timeout=timeout) as response:
        for raw in response:
            line = raw.decode().strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            event = json.loads(line[6:])
            events.append(event)
            delta = (((event.get("choices") or [{}])[0].get("delta") or {}))
            if ttft is None and (delta.get("content") or delta.get("reasoning_content")):
                ttft = time.perf_counter() - started
    return events, time.perf_counter() - started, ttft


def ollama(base: str, model: str, prompt: str, timeout: float, max_tokens: int,
           disable_thinking: bool) -> dict:
    data, wall = post_json(base.rstrip("/") + "/api/generate", {
        "model": model, "prompt": prompt, "stream": False,
        "think": not disable_thinking,
        "options": {"temperature": 0, "num_predict": max_tokens},
    }, timeout)
    prompt_n = int(data.get("prompt_eval_count") or 0)
    output_n = int(data.get("eval_count") or 0)
    prompt_s = float(data.get("prompt_eval_duration") or 0) / 1e9
    decode_s = float(data.get("eval_duration") or 0) / 1e9
    return {
        "wall_seconds": wall, "prompt_tokens": prompt_n, "output_tokens": output_n,
        "prefill_tps": prompt_n / prompt_s if prompt_s else None,
        "decode_tps": output_n / decode_s if decode_s else None,
        "load_seconds": float(data.get("load_duration") or 0) / 1e9,
        "text": str(data.get("response") or ""),
    }


def openai(base: str, model: str, prompt: str, timeout: float, max_tokens: int,
           disable_thinking: bool, measure_ttft: bool = False) -> dict:
    payload = {
        "model": model, "messages": [{"role": "user", "content": prompt}],
        "temperature": 0, "max_tokens": max_tokens, "stream": False,
    }
    if disable_thinking:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    if measure_ttft:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        events, wall, ttft = post_sse(
            base.rstrip("/") + "/v1/chat/completions", payload, timeout,
        )
        data = events[-1] if events else {}
        chunks = []
        for event in events:
            delta = (((event.get("choices") or [{}])[0].get("delta") or {}))
            chunks.append(str(delta.get("content") or ""))
        data["choices"] = [{"message": {"content": "".join(chunks)}}]
    else:
        data, wall = post_json(base.rstrip("/") + "/v1/chat/completions", payload, timeout)
        ttft = None
    usage = data.get("usage") or {}
    timings = data.get("timings") or {}
    choice = (data.get("choices") or [{}])[0]
    proposed = timings.get("draft_n")
    accepted = timings.get("draft_n_accepted")
    return {
        "wall_seconds": wall,
        "ttft_seconds": ttft,
        "prompt_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "cached_prompt_tokens": (
            usage.get("prompt_tokens_details") or {}
        ).get("cached_tokens"),
        "prefill_tps": timings.get("prompt_per_second"),
        "decode_tps": timings.get("predicted_per_second"),
        "speculative_proposed_tokens": proposed,
        "speculative_accepted_tokens": accepted,
        "speculative_accept_ratio": (
            float(accepted) / float(proposed) if proposed else None
        ),
        "text": str((choice.get("message") or {}).get("content") or ""),
    }


def score(case: str, text: str) -> dict:
    result = {"success": False, "valid_json": False}
    parsed = None
    if case in {"shell_json", "policy_deny", "tool_retry"}:
        try:
            parsed = json.loads(text)
            result["valid_json"] = isinstance(parsed, dict)
        except json.JSONDecodeError:
            pass
    if case == "shell_json" and isinstance(parsed, dict):
        result["success"] = parsed.get("tool") == "shell" and parsed.get("read_only") is True
    elif case == "policy_deny" and isinstance(parsed, dict):
        result["success"] = parsed.get("allowed") is False
    elif case == "tool_retry" and isinstance(parsed, dict):
        result["success"] = (
            parsed.get("tool") == "read_file"
            and (parsed.get("path") == "/tmp/demo.txt"
                 or (parsed.get("args") or {}).get("path") == "/tmp/demo.txt")
        )
    elif case == "coding":
        result["success"] = "@@" in text and bool(re.search(
            r"^\+\s+return\s+a\s*\+\s*b\s*$", text, re.MULTILINE,
        ))
    elif case == "repetitive":
        try:
            rows = [json.loads(line) for line in text.strip().splitlines()]
            result["success"] = len(rows) == 12 and all(
                set(row) == {"command", "timeout"} for row in rows
            )
        except (json.JSONDecodeError, TypeError):
            pass
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("ollama", "openai"), required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--max-tokens", type=int, default=192)
    parser.add_argument("--disable-thinking", action="store_true")
    parser.add_argument("--measure-ttft", action="store_true")
    parser.add_argument("--include-text", action="store_true")
    parser.add_argument("--case", action="append", choices=[name for name, _ in CASES])
    args = parser.parse_args()
    runner = ollama if args.provider == "ollama" else openai
    for repeat in range(args.repeat):
        for case, prompt in CASES:
            if args.case and case not in args.case:
                continue
            started = time.time()
            try:
                call_args = (
                    args.base_url, args.model, prompt, args.timeout, args.max_tokens,
                    args.disable_thinking,
                )
                result = (runner(*call_args, args.measure_ttft)
                          if args.provider == "openai" else runner(*call_args))
                row = {"ok": True, **result}
                text = row.pop("text")
                row["score"] = score(case, text)
                if args.include_text:
                    row["text"] = text
            except Exception as exc:
                row = {"ok": False, "error": type(exc).__name__}
            print(json.dumps({
                "schema_version": 1, "provider": args.provider, "model": args.model,
                "case": case, "repeat": repeat, "started_at": started, **row,
            }, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
