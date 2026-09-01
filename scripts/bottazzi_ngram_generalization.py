#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import time
import urllib.request


@dataclass(frozen=True)
class Scenario:
    name: str
    project: str | None
    domain: str | None
    session_seed: str
    project_seed: str
    global_seed: str
    target: str
    required: tuple[str, ...]
    forbidden: tuple[str, ...] = ()


def structured_prompt(project: str, host: str, environment: str, service: str, path: str) -> str:
    return (
        "Return only a JSON array of exactly 4 objects. Every object must contain keys "
        "project, host, environment, service, path, action, ordinal. Repeat the exact "
        f"values project={project}, host={host}, environment={environment}, "
        f"service={service}, path={path}, action=deploy; ordinal runs 1 through 4."
    )


SCENARIOS = {
    "exact_repeat": Scenario(
        "exact_repeat", "ralfloop", "systemd",
        structured_prompt("ralfloop", "host-a", "production", "ralf.service", "/srv/ralf/app.py"),
        structured_prompt("ralfloop", "host-a", "production", "ralf.service", "/srv/ralf/app.py"),
        structured_prompt("ralfloop", "host-a", "production", "ralf.service", "/srv/ralf/app.py"),
        structured_prompt("ralfloop", "host-a", "production", "ralf.service", "/srv/ralf/app.py"),
        ('"host":"host-a"', '"environment":"production"'),
    ),
    "near_repeat": Scenario(
        "near_repeat", "ralfloop", "systemd",
        structured_prompt("ralfloop", "host-a", "production", "ralf.service", "/srv/ralf/app.py"),
        structured_prompt("ralfloop", "host-a", "production", "ralf.service", "/srv/ralf/app.py"),
        structured_prompt("ralfloop", "host-a", "production", "ralf.service", "/srv/ralf/app.py"),
        structured_prompt("ralfloop", "host-a", "production", "ralf.service", "/srv/ralf/worker.py"),
        ('"path":"/srv/ralf/worker.py"',), ('"path":"/srv/ralf/app.py"',),
    ),
    "same_project_different_file": Scenario(
        "same_project_different_file", "ralfloop", "python",
        structured_prompt("ralfloop", "host-a", "production", "ralf.service", "/srv/ralf/app.py"),
        structured_prompt("ralfloop", "host-a", "production", "ralf.service", "/srv/ralf/app.py"),
        structured_prompt("ralfloop", "host-a", "production", "ralf.service", "/srv/ralf/app.py"),
        structured_prompt("ralfloop", "host-a", "production", "ralf-worker.service", "/srv/ralf/jobs.py"),
        ('"service":"ralf-worker.service"', '"path":"/srv/ralf/jobs.py"'),
        ('"service":"ralf.service"',),
    ),
    "same_domain_different_host": Scenario(
        "same_domain_different_host", None, "systemd",
        structured_prompt("ops", "host-a", "production", "api.service", "/srv/api/app"),
        structured_prompt("ops", "host-a", "production", "api.service", "/srv/api/app"),
        structured_prompt("ops", "host-a", "production", "api.service", "/srv/api/app"),
        structured_prompt("ops", "host-b", "staging", "api.service", "/srv/api/app"),
        ('"host":"host-b"', '"environment":"staging"'),
        ('"host":"host-a"', '"environment":"production"'),
    ),
    "unrelated_project": Scenario(
        "unrelated_project", "browser-bridge", "systemd",
        structured_prompt("browser-bridge", "host-b", "staging", "browser.service", "/opt/browser/main.py"),
        structured_prompt("browser-bridge", "host-b", "staging", "browser.service", "/opt/browser/main.py"),
        structured_prompt("ralfloop", "host-a", "production", "ralf.service", "/srv/ralf/app.py"),
        structured_prompt("browser-bridge", "host-b", "staging", "browser.service", "/opt/browser/main.py"),
        ('"project":"browser-bridge"', '"environment":"staging"'),
        ('"project":"ralfloop"',),
    ),
    "novel_prose": Scenario(
        "novel_prose", None, None,
        "Write one short paragraph explaining why exact token identity matters.",
        "Write one short paragraph explaining why exact token identity matters.",
        structured_prompt("ralfloop", "host-a", "production", "ralf.service", "/srv/ralf/app.py"),
        "Write one short paragraph, without JSON or shell commands, explaining why a "
        "semantic match is insufficient to restore recurrent model state.",
        ("recurrent",), ("\"ordinal\"",),
    ),
}


def request_stream(base_url: str, model: str, prompt: str, max_tokens: int) -> dict:
    payload = {
        "model": model, "messages": [{"role": "user", "content": prompt}],
        "temperature": 0, "max_tokens": max_tokens, "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    started = time.perf_counter()
    ttft = None
    text_parts = []
    final = {}
    with urllib.request.urlopen(request, timeout=900) as response:
        for raw in response:
            line = raw.decode().strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            event = json.loads(line[6:])
            final = event
            delta = (((event.get("choices") or [{}])[0].get("delta") or {}))
            piece = str(delta.get("content") or "")
            if piece and ttft is None:
                ttft = time.perf_counter() - started
            text_parts.append(piece)
    timings = final.get("timings") or {}
    usage = final.get("usage") or {}
    proposed = int(timings.get("draft_n") or 0)
    accepted = int(timings.get("draft_n_accepted") or 0)
    return {
        "text": "".join(text_parts), "ttft_seconds": ttft,
        "wall_seconds": time.perf_counter() - started,
        "decode_tps": timings.get("predicted_per_second"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "cached_prompt_tokens": (
            usage.get("prompt_tokens_details") or {}
        ).get("cached_tokens"),
        "speculative_proposed_tokens": proposed,
        "speculative_accepted_tokens": accepted,
        "speculative_accept_ratio": accepted / proposed if proposed else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:19196")
    parser.add_argument("--model", default="qwen3.5-35b-a3b")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), required=True)
    parser.add_argument(
        "--scope", choices=("off", "global", "contaminated_global", "project", "session"),
        required=True,
    )
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--warmup-max-tokens", type=int, default=128)
    parser.add_argument("--lookup-ns", type=float)
    args = parser.parse_args()
    scenario = SCENARIOS[args.scenario]
    seeds = []
    if args.scope == "global":
        seeds = list(dict.fromkeys(
            seed for item in SCENARIOS.values()
            for seed in (item.global_seed, item.session_seed)
        ))
    elif args.scope == "contaminated_global":
        seeds = [scenario.global_seed]
    elif args.scope == "project":
        seeds = [scenario.project_seed]
    elif args.scope == "session":
        seeds = [scenario.session_seed]
    for seed in seeds:
        request_stream(args.base_url, args.model, seed, args.warmup_max_tokens)
    for repeat in range(args.repeat):
        selection_started = time.perf_counter()
        selected_pool_id = (
            None if args.scope == "off" else
            f"{args.scope}:{scenario.project or scenario.domain or scenario.name}"
        )
        selection_seconds = time.perf_counter() - selection_started
        result = request_stream(args.base_url, args.model, scenario.target, args.max_tokens)
        text = result.pop("text")
        normalized = "".join(text.lower().split())
        correct = (
            all("".join(value.lower().split()) in normalized for value in scenario.required)
            and not any("".join(value.lower().split()) in normalized for value in scenario.forbidden)
        )
        proposed = result.get("speculative_proposed_tokens") or 0
        accepted = result.get("speculative_accepted_tokens") or 0
        prompt_tokens = result.get("prompt_tokens") or 0
        cached_tokens = result.get("cached_prompt_tokens") or 0
        print(json.dumps({
            "schema_version": 1, "scenario": scenario.name, "ngram_pool_scope": args.scope,
            "ngram_pool_id": selected_pool_id,
            "ngram_pool_selection_seconds": selection_seconds,
            "ngram_pool_hits": accepted,
            "ngram_pool_misses": max(0, proposed - accepted),
            "ngram_pool_bytes": 0 if args.scope == "off" else 16 * 1024 * 1024,
            "ngram_speculative_accept_ratio": accepted / proposed if proposed else None,
            "prompt_cached_token_ratio": cached_tokens / prompt_tokens if prompt_tokens else 0.0,
            "repeat": repeat, "correct": correct,
            "output_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "incorrect_output_excerpt": None if correct else text[-500:],
            "ngram_lookup_cpu_seconds_estimate": (
                proposed * args.lookup_ns / 1e9 if args.lookup_ns is not None else None
            ),
            **result,
        }, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
