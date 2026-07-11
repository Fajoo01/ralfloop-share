from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import gc
import hashlib
import resource
import re
import socket
import subprocess
import sys
import time
from typing import Any


DEFAULT_ROOT = Path("/home/sibilla-cumana/RecursiveMAS")
REQUIRED_FILES = [
    "README.md",
    "requirements.txt",
    "inference/README.md",
    "inference/run.py",
    "inference/system_loader.py",
    "inference/modeling.py",
    "inference/hf_resolver.py",
    "inference/load_from_repo.py",
    "inference/inference_utils/inference_mas.py",
    "inference/inference_utils/inference_mas_mixture.py",
    "inference/inference_utils/inference_mas_distill.py",
    "inference/inference_utils/inference_mas_deliberation.py",
    "inference/inference_utils/llm_judge.py",
]
DEPENDENCY_MODULES = [
    "torch",
    "transformers",
    "datasets",
    "huggingface_hub",
    "accelerate",
    "safetensors",
]
UPSTREAM_MODULES = [
    "inference.modeling",
    "inference.system_loader",
    "inference.hf_resolver",
    "inference.load_from_repo",
    "inference.inference_utils.inference_mas",
    "inference.inference_utils.inference_mas_mixture",
    "inference.inference_utils.inference_mas_distill",
    "inference.inference_utils.inference_mas_deliberation",
    "inference.inference_utils.llm_judge",
]
TOKEN_ENV_NAMES = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_TOKEN")
SEQUENTIAL_LIGHT_REPOS = {
    "planner": "RecursiveMAS/Sequential-Light-Planner-Qwen3-1.7B",
    "critic": "RecursiveMAS/Sequential-Light-Critic-Llama3.2-1B",
    "solver": "RecursiveMAS/Sequential-Light-Solver-Qwen2.5-Math-1.5B",
    "outer": "RecursiveMAS/Sequential-Light-Outerlinks",
}
SEQUENTIAL_LIGHT_REPO_SET = set(SEQUENTIAL_LIGHT_REPOS.values())
SEQUENTIAL_OUTER_FILES = {
    "outer_12": "Planner-Critic-Outerlink(math).pt",
    "outer_23": "Critic-Solver-Outerlink(math).pt",
    "outer_31": "Solver-Planner-Outerlink(math).pt",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--probe", action="store_true")
    group.add_argument("--import-check", action="store_true")
    group.add_argument("--checkpoint-manifest", action="store_true")
    group.add_argument("--load-check", action="store_true")
    group.add_argument("--native-canary", action="store_true")
    group.add_argument("--native-diagnostic-matrix", action="store_true")
    group.add_argument("--gpu-stagewise-probe", action="store_true")
    group.add_argument("--gpu-stagewise-canary", action="store_true")
    group.add_argument("--gpu-stagewise-matrix", action="store_true")
    group.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    request = _read_request()
    root = Path(str(request.get("upstream_root") or os.getenv("RALFLOOP_RECURSIVE_MAS_ROOT") or DEFAULT_ROOT)).resolve()
    try:
        if args.probe:
            result = _probe(root)
        elif args.import_check:
            result = _import_check(root)
        elif args.checkpoint_manifest:
            result = _checkpoint_manifest(root, request)
        elif args.load_check:
            result = _load_check(root, request)
        elif args.native_canary:
            result = _native_canary(root, request)
        elif args.native_diagnostic_matrix:
            result = _native_diagnostic_matrix(root, request)
        elif args.gpu_stagewise_probe:
            result = _gpu_stagewise_probe(root, request)
        elif args.gpu_stagewise_canary:
            result = _gpu_stagewise_canary(root, request)
        elif args.gpu_stagewise_matrix:
            result = _gpu_stagewise_matrix(root, request)
        else:
            result = {
                "ok": True,
                "operation": "run",
                "implemented": False,
                "reason": "checkpoint_execution_not_enabled",
                "model_load_attempted": False,
                "download_attempted": False,
                "runtime_ready": False,
            }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("ok") else 1
    except Exception as exc:
        print(f"recursive_mas_worker_error={type(exc).__name__}: {exc}", file=sys.stderr)
        print(
            json.dumps(
                {
                    "ok": False,
                    "operation": _operation_name(args),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "model_load_attempted": False,
                    "download_attempted": False,
                    "runtime_ready": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1


def _read_request() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("stdin JSON must be an object")
    return data


def _operation_name(args: argparse.Namespace) -> str:
    if args.probe:
        return "probe"
    if args.import_check:
        return "import_check"
    if args.checkpoint_manifest:
        return "checkpoint_manifest"
    if args.load_check:
        return "load_check"
    if args.native_canary:
        return "native_canary"
    if args.native_diagnostic_matrix:
        return "native_diagnostic_matrix"
    if getattr(args, "gpu_stagewise_probe", False):
        return "gpu_stagewise_probe"
    if getattr(args, "gpu_stagewise_canary", False):
        return "gpu_stagewise_canary"
    if getattr(args, "gpu_stagewise_matrix", False):
        return "gpu_stagewise_matrix"
    return "run"


def _probe(root: Path) -> dict[str, Any]:
    offline_mode = _offline_mode()
    files_found, files_missing = _required_file_status(root)
    deps = _dependency_versions()
    commit = _git_commit(root)
    checkpoint = _checkpoint_status(root)
    dependencies_available = all(item.get("import_ok") for item in deps.values())
    repository_available = root.is_dir() and not files_missing
    runtime_ready = False
    return {
        "ok": True,
        "operation": "probe",
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "upstream_root": str(root),
        "upstream_commit": commit,
        "repository_available": repository_available,
        "upstream_files_found": files_found,
        "upstream_files_missing": files_missing,
        "dependencies": deps,
        "dependencies_available": dependencies_available,
        "offline_mode": offline_mode,
        "token_env_present": {name: bool(os.getenv(name)) for name in TOKEN_ENV_NAMES},
        "checkpoint_status": checkpoint["checkpoint_status"],
        "checkpoint_files_found": checkpoint["checkpoint_files_found"],
        "model_load_attempted": False,
        "download_attempted": False,
        "runtime_ready": runtime_ready,
        "reason": "checkpoint_execution_not_enabled" if not checkpoint["checkpoints_available"] else "native_disabled",
    }


def _import_check(root: Path) -> dict[str, Any]:
    _prepare_import_path(root)
    network = {"attempted": False}
    download = {"attempted": False}
    model_load = {"attempted": False}
    _patch_network(network)
    modules = DEPENDENCY_MODULES + UPSTREAM_MODULES
    results = []
    for module in modules:
        started = time.perf_counter()
        before = (network["attempted"], download["attempted"], model_load["attempted"])
        error = None
        ok = False
        try:
            imported = importlib.import_module(module)
            ok = True
            _patch_download_and_model_load(imported, download, model_load)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        after = (network["attempted"], download["attempted"], model_load["attempted"])
        results.append(
            {
                "module": module,
                "import_ok": ok,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "network_attempt_detected": after[0] != before[0],
                "download_attempt_detected": after[1] != before[1],
                "model_load_detected": after[2] != before[2],
                "error": error,
            }
        )
    return {
        "ok": all(item["import_ok"] for item in results),
        "operation": "import_check",
        "python_executable": sys.executable,
        "upstream_root": str(root),
        "offline_mode": _offline_mode(),
        "imports": results,
        "network_attempted": network["attempted"],
        "download_attempted": download["attempted"],
        "model_load_attempted": model_load["attempted"],
        "runtime_ready": False,
    }


def _checkpoint_manifest(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    style = str(request.get("style") or "sequential_light")
    if style != "sequential_light":
        return {"ok": False, "operation": "checkpoint-manifest", "style": style, "error": "unsupported_style"}
    repositories = {}
    missing_components = []
    total = 0
    for role, repo in SEQUENTIAL_LIGHT_REPOS.items():
        snapshot = _snapshot_path_for_repo(repo)
        files = _files_under(snapshot)
        size = sum(item["size_bytes"] for item in files)
        total += size
        if role == "outer":
            required = ["outerlink_config.json", *SEQUENTIAL_OUTER_FILES.values()]
        else:
            required = [
                "config.json",
                "generation_config.json",
                "tokenizer.json",
                "tokenizer_config.json",
                "model.safetensors",
                "adapter(math).pt",
                "adapter_config.json",
                "innerlink_config.json",
            ]
        present = {item["path"] for item in files}
        missing = [name for name in required if name not in present]
        if missing:
            missing_components.append({"repo_id": repo, "missing": missing})
        repositories[role] = {
            "repo_id": repo,
            "snapshot_path": str(snapshot),
            "exists": snapshot.is_dir(),
            "files": files,
            "size_bytes": size,
            "required_files": required,
            "missing_files": missing,
            "complete": snapshot.is_dir() and not missing,
            "outerlinks": _outerlink_presence(present) if role == "outer" else None,
        }
    complete = all(item["complete"] for item in repositories.values())
    return {
        "ok": True,
        "operation": "checkpoint-manifest",
        "style": style,
        "repositories": repositories,
        "complete": complete,
        "resolved_revisions": {role: _revision_from_snapshot(Path(data["snapshot_path"])) for role, data in repositories.items()},
        "total_size_bytes": total,
        "missing_components": missing_components,
        "unexpected_components": [],
        "offline_ready": complete,
    }


def _load_check(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    style = str(request.get("style") or "sequential_light")
    device = str(request.get("device") or "cpu")
    if style != "sequential_light":
        return {"ok": False, "operation": "load-check", "style": style, "error": "unsupported_style"}
    if device != "cpu":
        return {"ok": False, "operation": "load-check", "style": style, "device": device, "error": "cuda_forbidden_for_canary"}
    manifest = _checkpoint_manifest(root, {"style": style})
    if not manifest["complete"]:
        return {"ok": False, "operation": "load-check", "style": style, "device": device, "error": "checkpoint_manifest_incomplete", "manifest": manifest}
    dtype = _choose_cpu_dtype()
    if not dtype["ok"]:
        return {"ok": False, "operation": "load-check", "style": style, "device": device, **dtype}
    _prepare_import_path(root)
    guard = _OfflineGuard(root)
    guard.install()
    before = _rss_bytes()
    system = None
    unload_fn = None
    try:
        from system_loader import load_mas_system, unload_mas_system  # type: ignore

        unload_fn = unload_mas_system
        system = load_mas_system(
            style="sequential_light",
            dataset="math500",
            device="cpu",
            dtype=dtype["dtype_effective"],
            outer_dtype="float32",
            trust_remote_code=True,
        )
        agents = sorted(system.agents)
        inner = {role: Path(agent.inner_adapter_path).name for role, agent in system.agents.items()}
        outer = {key: Path(system.paths.outer_adapter_paths[key]).name for key in sorted(system.outer_adapters)}
        ok = agents == ["critic", "planner", "solver"] and set(outer) == {"outer_12", "outer_23", "outer_31"}
        peak = max(before, _rss_peak_bytes())
        if system is not None:
            unload_fn(system)
            system = None
        gc.collect()
        after = _rss_bytes()
        return {
            "ok": ok,
            "operation": "load-check",
            "style": style,
            "device": "cpu",
            "dtype_requested": "auto",
            "dtype_effective": dtype["dtype_effective"],
            "dtype_probe": dtype,
            "agents_loaded": agents,
            "inner_links_loaded": inner,
            "outer_links_loaded": outer,
            "native_components_verified": ok,
            "generation_attempted": False,
            "network_attempted": guard.network_attempted,
            "download_attempted": guard.download_attempted,
            "unexpected_repo_request": guard.unexpected_repo_request,
            "rss_before_bytes": before,
            "rss_peak_bytes": peak,
            "rss_after_unload_bytes": after,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:
        peak = max(before, _rss_peak_bytes())
        try:
            if system is not None and unload_fn is not None:
                unload_fn(system)
                system = None
        except Exception:
            pass
        gc.collect()
        after = _rss_bytes()
        return {
            "ok": False,
            "operation": "load-check",
            "style": style,
            "device": device,
            "dtype_effective": dtype.get("dtype_effective"),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "generation_attempted": False,
            "network_attempted": guard.network_attempted,
            "download_attempted": guard.download_attempted,
            "unexpected_repo_request": guard.unexpected_repo_request,
            "rss_before_bytes": before,
            "rss_peak_bytes": peak,
            "rss_after_unload_bytes": after,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
    finally:
        try:
            if system is not None and unload_fn is not None:
                unload_fn(system)
        except Exception:
            pass
        gc.collect()


def _native_canary(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    style = str(request.get("style") or "sequential_light")
    device = str(request.get("device") or "cpu")
    prompt = str(request.get("prompt") or "Solve and return only the final integer: 2 + 3")
    rounds = int(request.get("recursion_rounds") or 1)
    if os.getenv("RALFLOOP_RECURSIVE_MAS_NATIVE_CANARY") != "1":
        return {"ok": False, "operation": "native-canary", "error": "native_canary_disabled"}
    if style != "sequential_light" or device != "cpu":
        return {"ok": False, "operation": "native-canary", "error": "unsupported_style_or_device", "style": style, "device": device}
    if rounds != 1:
        return {"ok": False, "operation": "native-canary", "error": "only_one_round_canary_allowed"}
    if _mem_available_bytes() < 35_000_000_000:
        return {"ok": False, "operation": "native-canary", "error": "insufficient_memavailable_for_canary", "mem_available_bytes": _mem_available_bytes()}
    dtype = _choose_cpu_dtype()
    if not dtype["ok"]:
        return {"ok": False, "operation": "native-canary", **dtype}
    _prepare_import_path(root)
    guard = _OfflineGuard(root)
    guard.install()
    before = _rss_bytes()
    system = None
    unload_fn = None
    instrumentation: list[dict[str, Any]] = []
    answer = ""
    try:
        from argparse import Namespace
        import torch
        from system_loader import load_mas_system, unload_mas_system  # type: ignore
        from inference_utils import inference_mas as base  # type: ignore

        unload_fn = unload_mas_system
        system = load_mas_system(
            style="sequential_light",
            dataset="math500",
            device="cpu",
            dtype=dtype["dtype_effective"],
            outer_dtype="float32",
            trust_remote_code=True,
        )

        planner = system.agents["planner"]
        critic = system.agents["critic"]
        solver = system.agents["solver"]
        _wrap_adapter(planner.inner_adapter, instrumentation, "inner", "planner", "planner", 1)
        _wrap_adapter(critic.inner_adapter, instrumentation, "inner", "critic", "critic", 1)
        _wrap_adapter(solver.inner_adapter, instrumentation, "inner", "solver", "solver", 1)
        _wrap_adapter(system.outer_adapters["outer_12"], instrumentation, "outer", "planner", "critic", 1)
        _wrap_adapter(system.outer_adapters["outer_23"], instrumentation, "outer", "critic", "solver", 1)
        _wrap_adapter(system.outer_adapters["outer_31"], instrumentation, "outer", "solver", "planner", 1)

        question = prompt
        with torch.no_grad():
            p_tok = planner.tokenizer
            p_prompt = base.build_math_planner_prompt(question)
            p_ids = base.render_chat_prompt_ids(p_tok, p_prompt, False)
            input_ids, mask = base.pad_left_ids([p_ids], pad_id=p_tok.pad_token_id, device=torch.device("cpu"))
            p_emb = planner.model.get_input_embeddings()(input_ids)
            p_hidden = base.autoregressive_latent_rollout(planner.model, planner.inner_adapter, p_emb, mask, latent_steps=1)
            p_self = base.run_inner_adapter(planner.inner_adapter, p_hidden, output_dtype=p_emb.dtype)
            p_to_c = base.run_outer_adapter(system.outer_adapters["outer_12"], p_self, output_dtype=p_emb.dtype).detach().cpu()

            c_tok = critic.tokenizer
            c_prompt = base.build_math_refiner_prompt_with_slot(question)
            c_prefix, c_suffix = base.split_prompt_ids_by_slots(c_tok, c_prompt, [base.PLANNER_SLOT], False)
            c_embed = critic.model.get_input_embeddings()
            c_dtype = c_embed.weight.dtype
            c_seq = torch.cat([
                base.token_ids_to_embeds(c_embed, c_prefix, device=torch.device("cpu"), dtype=c_dtype),
                p_to_c[0].to(dtype=c_dtype),
                base.token_ids_to_embeds(c_embed, c_suffix, device=torch.device("cpu"), dtype=c_dtype),
            ], dim=0)
            c_batch, c_mask = base.pad_left_embeds([c_seq], device=torch.device("cpu"))
            c_hidden = base.autoregressive_latent_rollout(critic.model, critic.inner_adapter, c_batch, c_mask, latent_steps=1)
            c_self = base.run_inner_adapter(critic.inner_adapter, c_hidden, output_dtype=c_dtype)
            c_to_s = base.run_outer_adapter(system.outer_adapters["outer_23"], c_self, output_dtype=c_dtype).detach().cpu()

            args = Namespace(mas_shape="chain", solver_pre_question=0)
            s_tok = solver.tokenizer
            s_prompt = base.build_math_solver_prompt_with_slots(question, args, mas_shape="chain")
            s_prefix, s_suffix = base.split_prompt_ids_by_slots(s_tok, s_prompt, [base.REFINED_SLOT], False)
            s_embed = solver.model.get_input_embeddings()
            s_dtype = s_embed.weight.dtype
            s_seq = torch.cat([
                base.token_ids_to_embeds(s_embed, s_prefix, device=torch.device("cpu"), dtype=s_dtype),
                c_to_s[0].to(dtype=s_dtype),
                base.token_ids_to_embeds(s_embed, s_suffix, device=torch.device("cpu"), dtype=s_dtype),
            ], dim=0)
            s_batch, s_mask = base.pad_left_embeds([s_seq], device=torch.device("cpu"))
            generated = solver.model.generate(
                inputs_embeds=s_batch,
                attention_mask=s_mask,
                max_new_tokens=16,
                do_sample=False,
                pad_token_id=s_tok.pad_token_id or s_tok.eos_token_id,
                eos_token_id=s_tok.eos_token_id,
            )
            seq = generated.sequences if hasattr(generated, "sequences") else generated
            instrumentation.append(
                {
                    "kind": "model_generate",
                    "link": "solver->final_text",
                    "round": rounds,
                    "source_agent": "solver",
                    "destination_agent": "final_decode",
                    "input_shape": list(getattr(s_batch, "shape", [])),
                    "output_shape": list(getattr(seq, "shape", [])),
                    "dtype": str(getattr(seq, "dtype", "")),
                    "device": str(getattr(seq, "device", "")),
                    "norm_finite": _finite_norm(seq),
                }
            )
            gen_ids = seq if seq.size(1) <= 16 else seq[:, s_mask.size(1):]
            answer = s_tok.batch_decode(gen_ids, skip_special_tokens=True)[0].strip()
        outer_called = {item["link"]: True for item in instrumentation if item["kind"] == "outer"}
        inner_called = {item["source_agent"]: True for item in instrumentation if item["kind"] == "inner"}
        solver_final_decode = any(item["link"] == "solver->final_text" for item in instrumentation)
        native_verified = (
            all(inner_called.get(x) for x in ("planner", "critic"))
            and all(outer_called.get(x) for x in ("planner->critic", "critic->solver"))
            and solver_final_decode
        )
        peak = max(before, _rss_peak_bytes())
        if system is not None:
            unload_fn(system)
            system = None
        gc.collect()
        after = _rss_bytes()
        return {
            "ok": native_verified,
            "operation": "native-canary",
            "style": style,
            "device": "cpu",
            "dtype_effective": dtype["dtype_effective"],
            "recursion_rounds": rounds,
            "native_execution_success": native_verified,
            "native_latent": native_verified,
            "native_latent_verified": native_verified,
            "answer": answer,
            "answer_correct": answer.strip() == "5",
            "intermediate_decode_attempted": False,
            "final_decode_executed": True,
            "planner_executed": bool(inner_called.get("planner")),
            "critic_executed": bool(inner_called.get("critic")),
            "solver_executed": solver_final_decode,
            "solver_inner_link_called": bool(inner_called.get("solver")),
            "outer_31_expected_this_round": False,
            "instrumentation": instrumentation,
            "network_attempted": guard.network_attempted,
            "download_attempted": guard.download_attempted,
            "unexpected_repo_request": guard.unexpected_repo_request,
            "rss_before_bytes": before,
            "rss_peak_bytes": peak,
            "rss_after_unload_bytes": after,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:
        peak = max(before, _rss_peak_bytes())
        try:
            if system is not None and unload_fn is not None:
                unload_fn(system)
                system = None
        except Exception:
            pass
        gc.collect()
        after = _rss_bytes()
        return {
            "ok": False,
            "operation": "native-canary",
            "style": style,
            "device": device,
            "dtype_effective": dtype.get("dtype_effective"),
            "native_execution_success": False,
            "native_latent": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "instrumentation": instrumentation,
            "network_attempted": guard.network_attempted,
            "download_attempted": guard.download_attempted,
            "unexpected_repo_request": guard.unexpected_repo_request,
            "rss_before_bytes": before,
            "rss_peak_bytes": peak,
            "rss_after_unload_bytes": after,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
    finally:
        try:
            if system is not None and unload_fn is not None:
                unload_fn(system)
        except Exception:
            pass
        gc.collect()


def _native_diagnostic_matrix(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    style = str(request.get("style") or "sequential_light")
    device = str(request.get("device") or "cpu")
    question = str(request.get("question") or "What is 2 + 3?")
    include_release_like = str(request.get("include_release_like", "0")).lower() in {"1", "true", "yes"}
    if os.getenv("RALFLOOP_RECURSIVE_MAS_NATIVE_DIAGNOSTIC") != "1":
        return {"ok": False, "operation": "native-diagnostic-matrix", "error": "native_diagnostic_disabled"}
    if style != "sequential_light" or device != "cpu":
        return {
            "ok": False,
            "operation": "native-diagnostic-matrix",
            "error": "unsupported_style_or_device",
            "style": style,
            "device": device,
        }
    if _mem_available_bytes() < 35_000_000_000:
        return {
            "ok": False,
            "operation": "native-diagnostic-matrix",
            "error": "insufficient_memavailable_for_diagnostic",
            "mem_available_bytes": _mem_available_bytes(),
        }
    manifest = _checkpoint_manifest(root, {"style": style})
    if not manifest.get("complete"):
        return {
            "ok": False,
            "operation": "native-diagnostic-matrix",
            "error": "checkpoint_manifest_incomplete",
            "manifest": manifest,
        }
    dtype = _choose_cpu_dtype()
    if not dtype.get("ok"):
        return {"ok": False, "operation": "native-diagnostic-matrix", **dtype}

    _prepare_import_path(root)
    guard = _OfflineGuard(root)
    guard.install()
    before = _rss_bytes()
    system = None
    unload_fn = None
    cases: list[dict[str, Any]] = []
    solver_baseline: dict[str, Any] = {}
    upstream_status_before = _git_status(root)
    try:
        import torch
        from system_loader import load_mas_system, unload_mas_system  # type: ignore

        _set_seed(42)
        solver_baseline = _run_solver_standalone_baseline(
            root=root,
            question=question,
            dtype_effective=str(dtype["dtype_effective"]),
            guard=guard,
        )
        gc.collect()

        unload_fn = unload_mas_system
        system = load_mas_system(
            style="sequential_light",
            dataset="math500",
            device="cpu",
            dtype=str(dtype["dtype_effective"]),
            outer_dtype="float32",
            trust_remote_code=True,
        )
        matrix = [
            ("A", 1, "deterministic_diagnostic"),
            ("B", 2, "deterministic_diagnostic"),
            ("C", 3, "deterministic_diagnostic"),
        ]
        if include_release_like:
            matrix.append(("D", 3, "release_like"))
        for case_name, rounds, profile in matrix:
            if _mem_available_bytes() < 8_000_000_000:
                cases.append(
                    {
                        "case": case_name,
                        "rounds": rounds,
                        "profile": profile,
                        "ok": False,
                        "error": "memavailable_below_guard",
                        "memavailable_bytes": _mem_available_bytes(),
                    }
                )
                break
            _set_seed(42)
            case_result = _run_native_matrix_case(
                system=system,
                question=question,
                case_name=case_name,
                rounds=rounds,
                profile_name=profile,
            )
            cases.append(case_result)
        peak = max(before, _rss_peak_bytes())
        if system is not None:
            unload_fn(system)
            system = None
        gc.collect()
        after = _rss_bytes()
        labels = _classify_diagnostic(solver_baseline, cases)
        upstream_status_after = _git_status(root)
        return {
            "ok": all(case.get("native_execution_success") for case in cases if case.get("case") in {"A", "B", "C"}),
            "operation": "native-diagnostic-matrix",
            "question": question,
            "style": style,
            "device": "cpu",
            "dtype_effective": dtype["dtype_effective"],
            "solver_baseline": solver_baseline,
            "cases": cases,
            "best_diagnosis": ",".join(labels) if labels else "unknown",
            "diagnosis_labels": labels,
            "upstream_comparison": _upstream_comparison_table(),
            "upstream_status_before": upstream_status_before,
            "upstream_status_after": upstream_status_after,
            "upstream_modified": _status_has_tracked_changes(upstream_status_after),
            "network_attempted": guard.network_attempted,
            "download_attempted": guard.download_attempted,
            "unexpected_repo_request": guard.unexpected_repo_request,
            "rss_before_bytes": before,
            "rss_peak_bytes": peak,
            "rss_after_unload_bytes": after,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:
        peak = max(before, _rss_peak_bytes())
        try:
            if system is not None and unload_fn is not None:
                unload_fn(system)
                system = None
        except Exception:
            pass
        gc.collect()
        return {
            "ok": False,
            "operation": "native-diagnostic-matrix",
            "question": question,
            "style": style,
            "device": device,
            "dtype_effective": dtype.get("dtype_effective"),
            "solver_baseline": solver_baseline,
            "cases": cases,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "network_attempted": guard.network_attempted,
            "download_attempted": guard.download_attempted,
            "unexpected_repo_request": guard.unexpected_repo_request,
            "rss_before_bytes": before,
            "rss_peak_bytes": peak,
            "rss_after_unload_bytes": _rss_bytes(),
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
    finally:
        try:
            if system is not None and unload_fn is not None:
                unload_fn(system)
        except Exception:
            pass
        gc.collect()


class StagewiseGPUExecutor:
    def __init__(
        self,
        root: Path,
        style: str = "sequential_light",
        device: str = "cuda:0",
        dtype: str = "float16",
    ) -> None:
        self.root = root
        self.style = style
        self.device_name = device
        self.dtype_name = dtype
        self.system: Any | None = None
        self.unload_fn: Any | None = None
        self.guard = _OfflineGuard(root)
        self.stage_events: list[dict[str, Any]] = []
        self.memory_leak_detected = False
        self.cuda_oom = False
        self.cpu_resident_load: dict[str, Any] = {}

    def probe(self) -> dict[str, Any]:
        started = time.perf_counter()
        if self.style != "sequential_light":
            return {"ok": False, "operation": "gpu-stagewise-probe", "error": "unsupported_style", "style": self.style}
        gpu = _torch_gpu_info(self.device_name)
        if not gpu.get("cuda_available"):
            return {"ok": False, "operation": "gpu-stagewise-probe", "gpu": gpu, "error": "cuda_unavailable"}
        self.guard.install()
        try:
            self.load_cpu_resident_system()
            stages: dict[str, Any] = {}
            for role, outer in (("planner", "outer_12"), ("critic", "outer_23"), ("solver", "outer_31")):
                try:
                    stage = self.activate_stage(role, outer)
                    self.deactivate_stage(role)
                    stages[role] = {**stage, "fit": True}
                except StagewiseGateError as exc:
                    stages[role] = {"fit": False, "error_type": type(exc).__name__, "error": str(exc), **exc.details}
                except Exception as exc:
                    if _is_cuda_oom(exc):
                        self.cuda_oom = True
                        self.deactivate_stage(role)
                        stages[role] = {"fit": False, "error_type": "cuda_oom", "error": str(exc)}
                    else:
                        raise
            all_fit = all(item.get("fit") for item in stages.values())
            full_estimate = sum(_module_tree_bytes(self.system.agents[role].model) for role in ("planner", "critic", "solver"))
            full_estimate += sum(_module_tree_bytes(agent.inner_adapter) for agent in self.system.agents.values())
            full_estimate += sum(_module_tree_bytes(adapter) for adapter in self.system.outer_adapters.values())
            free_now, total = _cuda_mem_get_info(self.device_name)
            return {
                "ok": all_fit and not self.cuda_oom and not self.memory_leak_detected,
                "operation": "gpu-stagewise-probe",
                "style": self.style,
                "device": self.device_name,
                "dtype": self.dtype_name,
                "gpu": gpu,
                "cpu_resident_load": self.cpu_resident_load,
                "stages": stages,
                "all_individual_stages_fit": all_fit,
                "full_system_estimated_bytes": full_estimate,
                "full_system_fits": _vram_gate_passed(full_estimate, free_now, 750 * 1024 * 1024),
                "cuda_oom": self.cuda_oom,
                "memory_leak_detected": self.memory_leak_detected,
                "network_attempted": self.guard.network_attempted,
                "download_attempted": self.guard.download_attempted,
                "unexpected_repo_request": self.guard.unexpected_repo_request,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            }
        finally:
            self.unload()

    def load_cpu_resident_system(self) -> None:
        started = time.perf_counter()
        _prepare_import_path(self.root)
        import torch
        from system_loader import load_mas_system, unload_mas_system  # type: ignore

        self.unload_fn = unload_mas_system
        before = _rss_bytes()
        self.system = load_mas_system(
            style=self.style,
            dataset="math500",
            device="cpu",
            dtype=self.dtype_name,
            outer_dtype=self.dtype_name,
            trust_remote_code=True,
        )
        gc.collect()
        any_cuda = _any_system_parameter_on_cuda(self.system)
        agents = sorted(self.system.agents)
        inner = {role: _module_tree_bytes(agent.inner_adapter) for role, agent in self.system.agents.items()}
        models = {role: _module_tree_bytes(agent.model) for role, agent in self.system.agents.items()}
        outer = {key: _module_tree_bytes(adapter) for key, adapter in self.system.outer_adapters.items()}
        dtypes = {
            role: str(next(agent.model.parameters()).dtype)
            for role, agent in self.system.agents.items()
        }
        self.cpu_resident_load = {
            "agents_loaded": agents,
            "all_agents_present": agents == ["critic", "planner", "solver"],
            "inner_adapter_bytes": inner,
            "model_bytes": models,
            "outer_adapter_bytes": outer,
            "model_dtypes": dtypes,
            "any_parameter_cuda": any_cuda,
            "rss_before_bytes": before,
            "rss_after_bytes": _rss_bytes(),
            "rss_peak_bytes": max(before, _rss_peak_bytes()),
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
        if any_cuda:
            raise RuntimeError("cpu_resident_load_left_parameters_on_cuda")
        torch.cuda.empty_cache()

    def activate_stage(self, role: str, required_outer_link: str | None = None) -> dict[str, Any]:
        if self.system is None:
            raise RuntimeError("system_not_loaded")
        import torch

        stage_started = time.perf_counter()
        free_before, total_vram = _cuda_mem_get_info(self.device_name)
        modules = self._stage_modules(role, required_outer_link)
        estimated = sum(_module_tree_bytes(module) for module in modules.values()) + 512 * 1024 * 1024
        safety = 750 * 1024 * 1024
        gate = _vram_gate_passed(estimated, free_before, safety)
        gate_payload = {
            "role": role,
            "required_outer_link": required_outer_link,
            "free_before": free_before,
            "total_vram": total_vram,
            "estimated_stage_bytes": estimated,
            "safety_margin_bytes": safety,
            "gate_passed": gate,
        }
        if not gate:
            raise StagewiseGateError("vram_gate_failed", gate_payload)
        torch.cuda.reset_peak_memory_stats(_cuda_index(self.device_name))
        transfer_started = time.perf_counter()
        for module in modules.values():
            module.to(torch.device(self.device_name))
        torch.cuda.synchronize()
        transfer_ms = int((time.perf_counter() - transfer_started) * 1000)
        free_after_load, _total = _cuda_mem_get_info(self.device_name)
        event = {
            "event": "activate_stage",
            **gate_payload,
            "active_cuda_models": _cuda_model_roles(self.system),
            "active_cuda_adapters": _cuda_adapter_names(self.system),
            "one_model_active": _cuda_model_roles(self.system) == [role],
            "transfer_to_gpu_ms": transfer_ms,
            "allocated_bytes": int(torch.cuda.memory_allocated()),
            "reserved_bytes": int(torch.cuda.memory_reserved()),
            "peak_allocated_bytes": int(torch.cuda.max_memory_allocated()),
            "free_after_load": free_after_load,
            "duration_ms": int((time.perf_counter() - stage_started) * 1000),
        }
        self.stage_events.append(event)
        return event

    def deactivate_stage(self, role: str) -> dict[str, Any]:
        if self.system is None:
            return {"event": "deactivate_stage", "role": role, "system_loaded": False}
        import torch

        started = time.perf_counter()
        free_before, total_vram = _cuda_mem_get_info(self.device_name)
        for module in self._stage_modules(role, "outer_12").values():
            module.to("cpu")
        for key in ("outer_23", "outer_31"):
            if key in self.system.outer_adapters:
                self.system.outer_adapters[key].to("cpu")
        torch.cuda.synchronize()
        gc.collect()
        torch.cuda.empty_cache()
        free_after, _ = _cuda_mem_get_info(self.device_name)
        leak = max(0, free_before - free_after)
        leak_detected = _detect_cuda_leak(free_before, free_after)
        self.memory_leak_detected = self.memory_leak_detected or leak_detected
        event = {
            "event": "deactivate_stage",
            "role": role,
            "free_before_unload": free_before,
            "total_vram": total_vram,
            "free_after_unload": free_after,
            "allocated_after_unload": int(torch.cuda.memory_allocated()),
            "reserved_after_unload": int(torch.cuda.memory_reserved()),
            "leak_bytes": leak,
            "memory_leak_detected": leak_detected,
            "active_cuda_models": _cuda_model_roles(self.system),
            "active_cuda_adapters": _cuda_adapter_names(self.system),
            "unload_ms": int((time.perf_counter() - started) * 1000),
        }
        self.stage_events.append(event)
        return event

    def run_planner(self, base: Any, question: str, feedback: Any, profile: dict[str, Any], round_index: int) -> tuple[Any, str]:
        self.activate_stage("planner", "outer_12")
        try:
            started = time.perf_counter()
            if feedback is None:
                latent, rendered = _planner_stage(self.system, base, question, profile, __import__("torch").device(self.device_name))
            else:
                latent, rendered = _planner_feedback_stage(self.system, base, question, feedback, profile, __import__("torch").device(self.device_name))
            self.stage_events.append({"event": "compute_stage", "round": round_index, "role": "planner", "compute_ms": int((time.perf_counter() - started) * 1000), "latent_output_shape": list(getattr(latent, "shape", [])), "latent_output_device": str(getattr(latent, "device", ""))})
            return latent.detach().cpu(), rendered
        finally:
            self.deactivate_stage("planner")

    def run_critic(self, base: Any, question: str, planner_latent: Any, profile: dict[str, Any], round_index: int) -> tuple[Any, str]:
        self.activate_stage("critic", "outer_23")
        try:
            started = time.perf_counter()
            latent, rendered = _critic_stage(self.system, base, question, planner_latent, profile, __import__("torch").device(self.device_name))
            self.stage_events.append({"event": "compute_stage", "round": round_index, "role": "critic", "compute_ms": int((time.perf_counter() - started) * 1000), "latent_input_shape": list(getattr(planner_latent, "shape", [])), "latent_output_shape": list(getattr(latent, "shape", [])), "latent_output_device": str(getattr(latent, "device", ""))})
            return latent.detach().cpu(), rendered
        finally:
            self.deactivate_stage("critic")

    def run_solver_feedback(self, base: Any, question: str, refiner_latent: Any, profile: dict[str, Any], args: Any, round_index: int) -> tuple[Any, str]:
        self.activate_stage("solver", "outer_31")
        try:
            started = time.perf_counter()
            latent, rendered = _solver_feedback_stage(self.system, base, question, refiner_latent, profile, args, __import__("torch").device(self.device_name))
            self.stage_events.append({"event": "compute_stage", "round": round_index, "role": "solver_feedback", "compute_ms": int((time.perf_counter() - started) * 1000), "latent_input_shape": list(getattr(refiner_latent, "shape", [])), "latent_output_shape": list(getattr(latent, "shape", [])), "latent_output_device": str(getattr(latent, "device", ""))})
            return latent.detach().cpu(), rendered
        finally:
            self.deactivate_stage("solver")

    def run_solver_final(
        self,
        base: Any,
        question: str,
        refiner_latent: Any,
        profile: dict[str, Any],
        args: Any,
        decode_counts: dict[str, int],
        round_index: int,
    ) -> tuple[str, list[int], str]:
        self.activate_stage("solver", None)
        try:
            started = time.perf_counter()
            raw, token_ids, rendered = _solver_final_decode(
                self.system,
                base,
                question,
                refiner_latent,
                profile,
                args,
                __import__("torch").device(self.device_name),
                decode_counts,
            )
            self.stage_events.append({"event": "compute_stage", "round": round_index, "role": "solver_final", "compute_ms": int((time.perf_counter() - started) * 1000), "decode_performed": True, "generated_token_count": len(token_ids)})
            return raw, token_ids, rendered
        finally:
            self.deactivate_stage("solver")

    def run(self, question: str, rounds: int, profile_name: str) -> dict[str, Any]:
        started = time.perf_counter()
        if self.system is None:
            self.load_cpu_resident_system()
        _set_seed(42)
        import torch
        from argparse import Namespace
        from inference_utils import inference_mas as base  # type: ignore

        args = Namespace(mas_shape="chain", solver_pre_question=0, choice_old_prompt=0)
        profile = _generation_profile(profile_name)
        profile["dtype"] = self.dtype_name
        instrumentation: list[dict[str, Any]] = []
        decode_counts = {"decode_call_count": 0, "generate_call_count": 0, "intermediate_decode_count": 0, "final_decode_count": 0}
        current_round = {"value": 0}
        prompt_hashes: dict[str, str] = {}
        raw_answer = ""
        generated_token_ids: list[int] = []
        with torch.no_grad(), _temporary_instrumentation(self.system, instrumentation, current_round):
            feedback_to_planner = None
            refiner_to_solver = None
            for round_index in range(1, rounds + 1):
                current_round["value"] = round_index
                planner_to_refiner, rendered = self.run_planner(base, question, feedback_to_planner, profile, round_index)
                prompt_hashes[f"round_{round_index}_planner"] = _sha256_text(rendered)
                refiner_to_solver, rendered = self.run_critic(base, question, planner_to_refiner, profile, round_index)
                prompt_hashes[f"round_{round_index}_critic"] = _sha256_text(rendered)
                if round_index < rounds:
                    feedback_to_planner, rendered = self.run_solver_feedback(base, question, refiner_to_solver, profile, args, round_index)
                    prompt_hashes[f"round_{round_index}_solver_feedback"] = _sha256_text(rendered)
            current_round["value"] = rounds
            raw_answer, generated_token_ids, rendered = self.run_solver_final(base, question, refiner_to_solver, profile, args, decode_counts, rounds)
            prompt_hashes[f"round_{rounds}_solver_final"] = _sha256_text(rendered)
        parsed = _diagnose_answer(
            raw_answer,
            expected="5",
            generated_token_ids=generated_token_ids,
            max_new_tokens=int(profile["max_new_tokens"]),
            eos_token_id=self.system.agents["solver"].tokenizer.eos_token_id,
        )
        summary = _summarize_instrumentation(instrumentation, rounds, decode_counts)
        stage_summary = _summarize_stage_events(self.stage_events)
        return {
            "rounds": rounds,
            "profile": profile_name,
            "profile_config": profile,
            "native_execution_success": summary["native_recursive_execution_success"] and not self.cuda_oom and not self.memory_leak_detected,
            "native_latent_verified": summary["native_latent_verified"],
            "closed_loop_verified": summary["closed_loop_verified"],
            "one_model_at_a_time_verified": stage_summary["one_model_at_a_time_verified"],
            "raw_answer": raw_answer,
            "boxed_answer": parsed["boxed_answer"],
            "last_integer": parsed["last_integer"],
            "normalized_answer": parsed["normalized_answer"],
            "format_compliant": parsed["format_compliant"],
            "answer_correct": parsed["answer_correct"],
            "possibly_truncated": parsed["possibly_truncated"],
            "generated_token_count": len(generated_token_ids),
            "generated_token_ids": generated_token_ids[:256],
            "eos_seen": parsed["eos_seen"],
            "parser": parsed,
            "prompt_hashes": prompt_hashes,
            "instrumentation": summary,
            "instrumentation_events": instrumentation,
            "stage_events": self.stage_events,
            "stage_summary": stage_summary,
            "cuda_oom": self.cuda_oom,
            "memory_leak_detected": self.memory_leak_detected,
            "network_attempted": self.guard.network_attempted,
            "download_attempted": self.guard.download_attempted,
            "unexpected_repo_request": self.guard.unexpected_repo_request,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }

    def unload(self) -> None:
        if self.system is not None and self.unload_fn is not None:
            try:
                self.unload_fn(self.system)
            except Exception:
                pass
        self.system = None
        try:
            import torch

            torch.cuda.synchronize()
            torch.cuda.empty_cache()
        except Exception:
            pass
        gc.collect()

    def _stage_modules(self, role: str, required_outer_link: str | None) -> dict[str, Any]:
        assert self.system is not None
        agent = self.system.agents[role]
        modules = {f"{role}.model": agent.model, f"{role}.inner": agent.inner_adapter}
        if required_outer_link:
            modules[required_outer_link] = self.system.outer_adapters[required_outer_link]
        return modules


class StagewiseGateError(RuntimeError):
    def __init__(self, message: str, details: dict[str, Any]) -> None:
        super().__init__(message)
        self.details = details


def _gpu_stagewise_probe(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    if os.getenv("RALFLOOP_RECURSIVE_MAS_GPU_STAGEWISE") != "1":
        return {"ok": False, "operation": "gpu-stagewise-probe", "error": "gpu_stagewise_disabled"}
    executor = StagewiseGPUExecutor(
        root=root,
        style=str(request.get("style") or "sequential_light"),
        device=str(request.get("device") or "cuda:0"),
        dtype=str(request.get("dtype") or "float16"),
    )
    try:
        return executor.probe()
    except Exception as exc:
        if _is_cuda_oom(exc):
            executor.cuda_oom = True
            executor.unload()
            return {"ok": False, "operation": "gpu-stagewise-probe", "error_type": "cuda_oom", "error": str(exc), "cuda_oom": True}
        executor.unload()
        return {"ok": False, "operation": "gpu-stagewise-probe", "error_type": type(exc).__name__, "error": str(exc)}


def _gpu_stagewise_canary(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    if os.getenv("RALFLOOP_RECURSIVE_MAS_GPU_STAGEWISE") != "1":
        return {"ok": False, "operation": "gpu-stagewise-canary", "error": "gpu_stagewise_disabled"}
    style = str(request.get("style") or "sequential_light")
    device = str(request.get("device") or "cuda:0")
    rounds = int(request.get("rounds") or request.get("recursion_rounds") or 1)
    profile = str(request.get("profile") or "deterministic_diagnostic")
    question = str(request.get("question") or "What is 2 + 3?")
    if not device.startswith("cuda"):
        return {"ok": False, "operation": "gpu-stagewise-canary", "error": "device_must_be_cuda", "device": device, "fallback_cpu_used": False}
    if style != "sequential_light":
        return {"ok": False, "operation": "gpu-stagewise-canary", "error": "unsupported_style", "style": style}
    executor = StagewiseGPUExecutor(root=root, style=style, device=device, dtype="float16")
    executor.guard.install()
    before = _rss_bytes()
    try:
        manifest = _checkpoint_manifest(root, {"style": style})
        if not manifest.get("complete"):
            return {"ok": False, "operation": "gpu-stagewise-canary", "error": "checkpoint_manifest_incomplete", "manifest": manifest}
        executor.load_cpu_resident_system()
        result = executor.run(question=question, rounds=rounds, profile_name=profile)
        return {
            "ok": bool(result.get("native_execution_success")),
            "operation": "gpu-stagewise-canary",
            "style": style,
            "device": device,
            "dtype": "float16",
            "question": question,
            "cpu_resident_load": executor.cpu_resident_load,
            "fallback_cpu_used": False,
            "fallback_text_used": False,
            "quantization_used": False,
            "rss_before_bytes": before,
            "rss_peak_bytes": max(before, _rss_peak_bytes()),
            **result,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:
        if _is_cuda_oom(exc):
            executor.cuda_oom = True
            return {"ok": False, "operation": "gpu-stagewise-canary", "error_type": "cuda_oom", "error": str(exc), "cuda_oom": True, "fallback_cpu_used": False, "duration_ms": int((time.perf_counter() - started) * 1000)}
        return {"ok": False, "operation": "gpu-stagewise-canary", "error_type": type(exc).__name__, "error": str(exc), "fallback_cpu_used": False, "stage_events": executor.stage_events, "duration_ms": int((time.perf_counter() - started) * 1000)}
    finally:
        executor.unload()


def _gpu_stagewise_matrix(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    if os.getenv("RALFLOOP_RECURSIVE_MAS_GPU_STAGEWISE") != "1":
        return {"ok": False, "operation": "gpu-stagewise-matrix", "error": "gpu_stagewise_disabled"}
    started = time.perf_counter()
    cases = []
    for name, rounds in (("G1", 1), ("G2", 2), ("G3", 3)):
        result = _gpu_stagewise_canary(root, {**request, "rounds": rounds, "profile": "deterministic_diagnostic"})
        result["case"] = name
        cases.append(result)
        if not result.get("ok") or result.get("cuda_oom") or result.get("memory_leak_detected"):
            break
    return {
        "ok": all(case.get("ok") for case in cases),
        "operation": "gpu-stagewise-matrix",
        "cases": cases,
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }


def _stagewise_event_plan(rounds: int) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for round_index in range(1, rounds + 1):
        events.extend([
            {"event": "activate_stage", "round": round_index, "role": "planner", "outer": "outer_12"},
            {"event": "deactivate_stage", "round": round_index, "role": "planner"},
            {"event": "activate_stage", "round": round_index, "role": "critic", "outer": "outer_23"},
            {"event": "deactivate_stage", "round": round_index, "role": "critic"},
        ])
        if round_index < rounds:
            events.extend([
                {"event": "activate_stage", "round": round_index, "role": "solver", "outer": "outer_31", "decode": False},
                {"event": "deactivate_stage", "round": round_index, "role": "solver"},
            ])
        else:
            events.extend([
                {"event": "activate_stage", "round": round_index, "role": "solver", "outer": None, "decode": True},
                {"event": "deactivate_stage", "round": round_index, "role": "solver"},
            ])
    return events


def _vram_gate_passed(estimated_stage_bytes: int, free_before: int, safety_margin_bytes: int) -> bool:
    return int(estimated_stage_bytes) + int(safety_margin_bytes) <= int(free_before)


def _detect_cuda_leak(free_before: int, free_after: int, limit_bytes: int = 256 * 1024 * 1024) -> bool:
    return int(free_before) - int(free_after) > int(limit_bytes)


def _module_tree_bytes(module: Any) -> int:
    total = 0
    for attr in ("parameters", "buffers"):
        try:
            iterator = getattr(module, attr)()
        except Exception:
            continue
        for tensor in iterator:
            try:
                total += int(tensor.numel() * tensor.element_size())
            except Exception:
                pass
    return total


def _cuda_index(device: str) -> int:
    if ":" in str(device):
        return int(str(device).split(":", 1)[1])
    return 0


def _cuda_mem_get_info(device: str) -> tuple[int, int]:
    import torch

    index = _cuda_index(device)
    free, total = torch.cuda.mem_get_info(index)
    return int(free), int(total)


def _torch_gpu_info(device: str) -> dict[str, Any]:
    try:
        import torch

        info = {
            "torch": torch.__version__,
            "cuda_available": bool(torch.cuda.is_available()),
            "cuda_version": torch.version.cuda,
            "device_count": torch.cuda.device_count(),
            "device": device,
        }
        if torch.cuda.is_available():
            index = _cuda_index(device)
            free, total = torch.cuda.mem_get_info(index)
            props = torch.cuda.get_device_properties(index)
            info.update(
                {
                    "name": props.name,
                    "compute_capability": [props.major, props.minor],
                    "total_bytes": int(total),
                    "free_bytes": int(free),
                    "float16_operational": _cuda_dtype_probe(torch.float16, device),
                    "bfloat16_operational": _cuda_dtype_probe(torch.bfloat16, device),
                    "preferred_dtype": "float16",
                    "safety_margin_bytes": 750 * 1024 * 1024,
                }
            )
        return info
    except Exception as exc:
        return {"cuda_available": False, "device": device, "error_type": type(exc).__name__, "error": str(exc)}


def _cuda_dtype_probe(dtype: Any, device: str) -> bool | str:
    try:
        import torch

        x = torch.ones((2, 2), device=torch.device(device), dtype=dtype)
        y = x @ x
        torch.cuda.synchronize()
        ok = bool(torch.isfinite(y.float()).all().item())
        del x, y
        torch.cuda.empty_cache()
        return ok
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def _any_system_parameter_on_cuda(system: Any) -> bool:
    for agent in system.agents.values():
        if _module_has_cuda_param(agent.model) or _module_has_cuda_param(agent.inner_adapter):
            return True
    return any(_module_has_cuda_param(adapter) for adapter in system.outer_adapters.values())


def _module_has_cuda_param(module: Any) -> bool:
    try:
        return any(getattr(param, "is_cuda", False) for param in module.parameters())
    except Exception:
        return False


def _cuda_model_roles(system: Any) -> list[str]:
    return [role for role, agent in system.agents.items() if _module_has_cuda_param(agent.model)]


def _cuda_adapter_names(system: Any) -> list[str]:
    names = []
    for role, agent in system.agents.items():
        if _module_has_cuda_param(agent.inner_adapter):
            names.append(f"{role}.inner")
    for key, adapter in system.outer_adapters.items():
        if _module_has_cuda_param(adapter):
            names.append(key)
    return sorted(names)


def _summarize_stage_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    activate = [item for item in events if item.get("event") == "activate_stage"]
    peak = max([int(item.get("peak_allocated_bytes") or 0) for item in activate] or [0])
    per_role: dict[str, int] = {}
    for item in activate:
        role = str(item.get("role"))
        per_role[role] = max(per_role.get(role, 0), int(item.get("peak_allocated_bytes") or 0))
    return {
        "one_model_at_a_time_verified": all(item.get("one_model_active") for item in activate),
        "vram_peak_bytes": peak,
        "vram_peak_by_role": per_role,
        "activate_count": len(activate),
        "deactivate_count": len([item for item in events if item.get("event") == "deactivate_stage"]),
        "memory_leak_detected": any(item.get("memory_leak_detected") for item in events),
    }


def _is_cuda_oom(exc: BaseException) -> bool:
    try:
        import torch

        if isinstance(exc, torch.cuda.OutOfMemoryError):
            return True
    except Exception:
        pass
    return "out of memory" in str(exc).lower() and "cuda" in str(exc).lower()


def _run_solver_standalone_baseline(
    root: Path,
    question: str,
    dtype_effective: str,
    guard: "_OfflineGuard",
) -> dict[str, Any]:
    started = time.perf_counter()
    before = _rss_bytes()
    try:
        import torch
        from argparse import Namespace
        from system_loader import resolve_mas_paths  # type: ignore
        from inference_utils import inference_mas as base  # type: ignore

        paths = resolve_mas_paths(style="sequential_light", dataset="math500")
        solver_path = str(paths.repo_paths["solver"])
        model_dtype = base.resolve_dtype(dtype_effective)
        model, tokenizer = base.load_agent_model_and_tokenizer(
            model_name_or_path=solver_path,
            device=torch.device("cpu"),
            dtype=model_dtype,
            trust_remote_code=True,
            agent_name="solver-baseline",
        )
        args = Namespace(mas_shape="chain", solver_pre_question=0, choice_old_prompt=0)
        refined_plan = "Compute the arithmetic directly and return the final result."
        user_prompt = base.build_math_solver_prompt(question, refined_plan, args=args)
        rendered = base.render_chat_prompt(tokenizer, user_prompt, enable_thinking=False)
        prompt_hash = _sha256_text(rendered)
        inputs = tokenizer(rendered, return_tensors="pt", padding=False, truncation=False).to(torch.device("cpu"))
        generated = model.generate(
            input_ids=inputs["input_ids"],
            attention_mask=inputs["attention_mask"],
            max_new_tokens=256,
            do_sample=False,
            pad_token_id=tokenizer.pad_token_id or tokenizer.eos_token_id,
            eos_token_id=tokenizer.eos_token_id,
            return_dict_in_generate=True,
        )
        seq = generated.sequences if hasattr(generated, "sequences") else generated
        prompt_len = inputs["input_ids"].size(1)
        gen_ids = seq[:, prompt_len:] if seq.size(1) > 256 else seq
        raw = tokenizer.batch_decode(gen_ids, skip_special_tokens=True)[0].strip()
        token_ids = [int(x) for x in gen_ids[0].detach().cpu().tolist()]
        parsed = _diagnose_answer(raw, expected="5", generated_token_ids=token_ids, max_new_tokens=256, eos_token_id=tokenizer.eos_token_id)
        base.release_resources(model, tokenizer)
        gc.collect()
        return {
            "solver_baseline_execution_success": True,
            "solver_baseline_raw_answer": raw,
            "solver_baseline_answer_correct": parsed["answer_correct"],
            "solver_baseline_format_compliant": parsed["format_compliant"],
            "solver_baseline_generated_tokens": len(token_ids),
            "solver_baseline_prompt_hash": prompt_hash,
            "parser": parsed,
            "network_attempted": guard.network_attempted,
            "download_attempted": guard.download_attempted,
            "rss_before_bytes": before,
            "rss_peak_bytes": max(before, _rss_peak_bytes()),
            "rss_after_unload_bytes": _rss_bytes(),
            "solver_baseline_duration_ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:
        gc.collect()
        return {
            "solver_baseline_execution_success": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "rss_before_bytes": before,
            "rss_peak_bytes": max(before, _rss_peak_bytes()),
            "rss_after_unload_bytes": _rss_bytes(),
            "solver_baseline_duration_ms": int((time.perf_counter() - started) * 1000),
        }


def _run_native_matrix_case(
    system: Any,
    question: str,
    case_name: str,
    rounds: int,
    profile_name: str,
) -> dict[str, Any]:
    started = time.perf_counter()
    before = _rss_bytes()
    instrumentation: list[dict[str, Any]] = []
    decode_counts = {
        "decode_call_count": 0,
        "generate_call_count": 0,
        "intermediate_decode_count": 0,
        "final_decode_count": 0,
    }
    profile = _generation_profile(profile_name)
    prompt_hashes: dict[str, str] = {}
    generated_token_ids: list[int] = []
    raw_answer = ""
    try:
        import torch
        from argparse import Namespace
        from inference_utils import inference_mas as base  # type: ignore

        args = Namespace(mas_shape="chain", solver_pre_question=0, choice_old_prompt=0)
        device = torch.device("cpu")
        current_round = {"value": 0}
        with _temporary_instrumentation(system, instrumentation, current_round):
            feedback_to_planner = None
            refiner_to_solver = None
            for round_index in range(1, rounds + 1):
                current_round["value"] = round_index
                if round_index == 1:
                    planner_to_refiner, rendered = _planner_stage(system, base, question, profile, device)
                else:
                    planner_to_refiner, rendered = _planner_feedback_stage(system, base, question, feedback_to_planner, profile, device)
                prompt_hashes[f"round_{round_index}_planner"] = _sha256_text(rendered)

                refiner_to_solver, rendered = _critic_stage(system, base, question, planner_to_refiner, profile, device)
                prompt_hashes[f"round_{round_index}_critic"] = _sha256_text(rendered)

                if round_index < rounds:
                    feedback_to_planner, rendered = _solver_feedback_stage(system, base, question, refiner_to_solver, profile, args, device)
                    prompt_hashes[f"round_{round_index}_solver_feedback"] = _sha256_text(rendered)

            current_round["value"] = rounds
            raw_answer, generated_token_ids, rendered = _solver_final_decode(
                system,
                base,
                question,
                refiner_to_solver,
                profile,
                args,
                device,
                decode_counts,
            )
            prompt_hashes[f"round_{rounds}_solver_final"] = _sha256_text(rendered)
        parsed = _diagnose_answer(
            raw_answer,
            expected="5",
            generated_token_ids=generated_token_ids,
            max_new_tokens=int(profile["max_new_tokens"]),
            eos_token_id=system.agents["solver"].tokenizer.eos_token_id,
        )
        summary = _summarize_instrumentation(instrumentation, rounds, decode_counts)
        peak = max(before, _rss_peak_bytes())
        return {
            "case": case_name,
            "rounds": rounds,
            "profile": profile_name,
            "profile_config": profile,
            "native_execution_success": summary["native_recursive_execution_success"],
            "closed_loop_verified": summary["closed_loop_verified"],
            "native_latent_verified": summary["native_latent_verified"],
            "raw_answer": raw_answer,
            "boxed_answer": parsed["boxed_answer"],
            "normalized_answer": parsed["normalized_answer"],
            "format_compliant": parsed["format_compliant"],
            "answer_correct": parsed["answer_correct"],
            "possibly_truncated": parsed["possibly_truncated"],
            "parser": parsed,
            "generated_token_count": len(generated_token_ids),
            "generated_token_ids": generated_token_ids[:256],
            "eos_seen": parsed["eos_seen"],
            "prompt_builder_names": [
                "build_math_planner_prompt",
                "build_math_planner_prompt_with_feedback_slot",
                "build_math_refiner_prompt_with_slot",
                "build_math_solver_prompt_with_slots",
            ],
            "prompt_hashes": prompt_hashes,
            "instrumentation": summary,
            "instrumentation_events": instrumentation,
            "memory": {
                "rss_before_bytes": before,
                "rss_peak_bytes": peak,
                "rss_after_bytes": _rss_bytes(),
                "memavailable_min_bytes": _mem_available_bytes(),
                "swap_before_bytes": _swap_used_bytes(),
                "swap_peak_bytes": _swap_used_bytes(),
            },
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:
        return {
            "case": case_name,
            "rounds": rounds,
            "profile": profile_name,
            "native_execution_success": False,
            "closed_loop_verified": False,
            "native_latent_verified": False,
            "raw_answer": raw_answer,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "instrumentation": _summarize_instrumentation(instrumentation, rounds, decode_counts),
            "instrumentation_events": instrumentation,
            "memory": {
                "rss_before_bytes": before,
                "rss_peak_bytes": max(before, _rss_peak_bytes()),
                "rss_after_bytes": _rss_bytes(),
                "memavailable_min_bytes": _mem_available_bytes(),
            },
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }


def _planner_stage(system: Any, base: Any, question: str, profile: dict[str, Any], device: Any) -> tuple[Any, str]:
    agent = system.agents["planner"]
    tok = agent.tokenizer
    user_prompt = base.build_math_planner_prompt(question)
    rendered = base.render_chat_prompt(tok, user_prompt, enable_thinking=False)
    ids = base.render_chat_prompt_ids(tok, user_prompt, False)
    input_ids, mask = base.pad_left_ids([ids], pad_id=tok.pad_token_id, device=device)
    embed = agent.model.get_input_embeddings()
    emb = embed(input_ids)
    hidden = base.autoregressive_latent_rollout(agent.model, agent.inner_adapter, emb, mask, latent_steps=int(profile["latent_length"]))
    self_latent = base.run_inner_adapter(agent.inner_adapter, hidden, output_dtype=emb.dtype)
    return base.run_outer_adapter(system.outer_adapters["outer_12"], self_latent, output_dtype=emb.dtype).detach().cpu(), rendered


def _planner_feedback_stage(system: Any, base: Any, question: str, feedback: Any, profile: dict[str, Any], device: Any) -> tuple[Any, str]:
    agent = system.agents["planner"]
    tok = agent.tokenizer
    user_prompt = base.build_math_planner_prompt_with_feedback_slot(question)
    rendered = base.render_chat_prompt(tok, user_prompt, enable_thinking=False)
    prefix, suffix = base.split_prompt_ids_by_slots(tok, user_prompt, [base.FEEDBACK_SLOT], False)
    embed = agent.model.get_input_embeddings()
    dtype = embed.weight.dtype
    seq = _concat_slot_sequence(base, embed, prefix, feedback[0], suffix, device, dtype)
    batch, mask = base.pad_left_embeds([seq], device=device)
    hidden = base.autoregressive_latent_rollout(agent.model, agent.inner_adapter, batch, mask, latent_steps=int(profile["latent_length"]))
    self_latent = base.run_inner_adapter(agent.inner_adapter, hidden, output_dtype=dtype)
    return base.run_outer_adapter(system.outer_adapters["outer_12"], self_latent, output_dtype=dtype).detach().cpu(), rendered


def _critic_stage(system: Any, base: Any, question: str, planner_latent: Any, profile: dict[str, Any], device: Any) -> tuple[Any, str]:
    agent = system.agents["critic"]
    tok = agent.tokenizer
    user_prompt = base.build_math_refiner_prompt_with_slot(question)
    rendered = base.render_chat_prompt(tok, user_prompt, enable_thinking=False)
    prefix, suffix = base.split_prompt_ids_by_slots(tok, user_prompt, [base.PLANNER_SLOT], False)
    embed = agent.model.get_input_embeddings()
    dtype = embed.weight.dtype
    seq = _concat_slot_sequence(base, embed, prefix, planner_latent[0], suffix, device, dtype)
    batch, mask = base.pad_left_embeds([seq], device=device)
    hidden = base.autoregressive_latent_rollout(agent.model, agent.inner_adapter, batch, mask, latent_steps=int(profile["latent_length"]))
    self_latent = base.run_inner_adapter(agent.inner_adapter, hidden, output_dtype=dtype)
    return base.run_outer_adapter(system.outer_adapters["outer_23"], self_latent, output_dtype=dtype).detach().cpu(), rendered


def _solver_feedback_stage(system: Any, base: Any, question: str, refiner_latent: Any, profile: dict[str, Any], args: Any, device: Any) -> tuple[Any, str]:
    agent = system.agents["solver"]
    tok = agent.tokenizer
    user_prompt = base.build_math_solver_prompt_with_slots(question, args, mas_shape="chain")
    rendered = base.render_chat_prompt(tok, user_prompt, enable_thinking=False)
    prefix, suffix = base.split_prompt_ids_by_slots(tok, user_prompt, [base.REFINED_SLOT], False)
    embed = agent.model.get_input_embeddings()
    dtype = embed.weight.dtype
    seq = _concat_slot_sequence(base, embed, prefix, refiner_latent[0], suffix, device, dtype)
    batch, mask = base.pad_left_embeds([seq], device=device)
    hidden = base.autoregressive_latent_rollout(agent.model, agent.inner_adapter, batch, mask, latent_steps=int(profile["latent_length"]))
    self_latent = base.run_inner_adapter(agent.inner_adapter, hidden, output_dtype=dtype)
    return base.run_outer_adapter(system.outer_adapters["outer_31"], self_latent, output_dtype=dtype).detach().cpu(), rendered


def _solver_final_decode(
    system: Any,
    base: Any,
    question: str,
    refiner_latent: Any,
    profile: dict[str, Any],
    args: Any,
    device: Any,
    counts: dict[str, int],
) -> tuple[str, list[int], str]:
    agent = system.agents["solver"]
    tok = agent.tokenizer
    user_prompt = base.build_math_solver_prompt_with_slots(question, args, mas_shape="chain")
    rendered = base.render_chat_prompt(tok, user_prompt, enable_thinking=False)
    prefix, suffix = base.split_prompt_ids_by_slots(tok, user_prompt, [base.REFINED_SLOT], False)
    embed = agent.model.get_input_embeddings()
    dtype = embed.weight.dtype
    seq = _concat_slot_sequence(base, embed, prefix, refiner_latent[0], suffix, device, dtype)
    batch, mask = base.pad_left_embeds([seq], device=device)
    kwargs = base.build_generation_kwargs(
        tok,
        max_new_tokens=int(profile["max_new_tokens"]),
        do_sample=bool(profile["do_sample"]),
        temperature=float(profile["temperature"]),
        top_p=float(profile["top_p"]),
    )
    counts["generate_call_count"] += 1
    generated = agent.model.generate(
        inputs_embeds=batch,
        attention_mask=mask,
        return_dict_in_generate=True,
        **kwargs,
    )
    sequences = generated.sequences if hasattr(generated, "sequences") else generated
    if sequences.size(1) > int(profile["max_new_tokens"]):
        gen_ids = sequences[:, mask.size(1):]
    else:
        gen_ids = sequences
    counts["decode_call_count"] += 1
    counts["final_decode_count"] += 1
    text = tok.batch_decode(gen_ids, skip_special_tokens=True)[0].strip()
    token_ids = [int(x) for x in gen_ids[0].detach().cpu().tolist()]
    return text, token_ids, rendered


def _concat_slot_sequence(base: Any, embed: Any, prefix: list[int], latent: Any, suffix: list[int], device: Any, dtype: Any) -> Any:
    return __import__("torch").cat(
        [
            base.token_ids_to_embeds(embed, prefix, device=device, dtype=dtype),
            latent.to(device=device, dtype=dtype),
            base.token_ids_to_embeds(embed, suffix, device=device, dtype=dtype),
        ],
        dim=0,
    )


class _temporary_instrumentation:
    def __init__(self, system: Any, log: list[dict[str, Any]], current_round: dict[str, int]) -> None:
        self.system = system
        self.log = log
        self.current_round = current_round
        self.originals: list[tuple[Any, Any]] = []

    def __enter__(self) -> "_temporary_instrumentation":
        for role in ("planner", "critic", "solver"):
            self._wrap(self.system.agents[role].inner_adapter, "inner", role, role)
        self._wrap(self.system.outer_adapters["outer_12"], "outer", "planner", "critic")
        self._wrap(self.system.outer_adapters["outer_23"], "outer", "critic", "solver")
        self._wrap(self.system.outer_adapters["outer_31"], "outer", "solver", "planner")
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        for module, original in reversed(self.originals):
            module.forward = original

    def _wrap(self, module: Any, kind: str, src: str, dst: str) -> None:
        original = module.forward
        self.originals.append((module, original))
        log = self.log
        current_round = self.current_round

        def wrapped(x: Any, *args: Any, **kwargs: Any) -> Any:
            out = original(x, *args, **kwargs)
            log.append(
                {
                    "round": int(current_round["value"]),
                    "agent": src,
                    "operation": kind,
                    "kind": kind,
                    "link": f"{src}->{dst}",
                    "source": src,
                    "destination": dst,
                    "source_agent": src,
                    "destination_agent": dst,
                    "latent_shape": list(getattr(out, "shape", [])),
                    "latent_dtype": str(getattr(out, "dtype", "")),
                    "latent_device": str(getattr(out, "device", "")),
                    "latent_norm_finite": _finite_norm(out),
                    "decode_performed": False,
                }
            )
            return out

        module.forward = wrapped


def _generation_profile(name: str) -> dict[str, Any]:
    if name == "deterministic_diagnostic":
        return {
            "name": name,
            "do_sample": False,
            "temperature": 0.6,
            "top_p": 0.95,
            "latent_length": 32,
            "max_new_tokens": 256,
            "batch_size": 1,
            "enable_thinking": False,
            "seed": 42,
            "dtype": "float32",
        }
    if name == "release_like":
        return {
            "name": name,
            "do_sample": True,
            "temperature": 0.6,
            "top_p": 0.95,
            "latent_length": 32,
            "max_new_tokens": 1000,
            "batch_size": 1,
            "enable_thinking": False,
            "seed": 42,
            "dtype": "float32",
        }
    raise ValueError(f"unsupported diagnostic profile: {name}")


def _diagnose_answer(
    raw_answer: str,
    expected: str = "5",
    generated_token_ids: list[int] | None = None,
    max_new_tokens: int | None = None,
    eos_token_id: int | None = None,
) -> dict[str, Any]:
    parser_error = None
    boxed_answer = None
    upstream_numeric_match = False
    try:
        from inference_utils.answer_utils import compare_answers, extract_boxed_answer  # type: ignore

        boxed_answer = extract_boxed_answer(raw_answer)
        _gold, _pred, upstream_numeric_match, _gold_norm, _pred_norm = compare_answers(expected, raw_answer, dataset_name="math500")
    except Exception as exc:
        parser_error = f"{type(exc).__name__}: {exc}"
    if boxed_answer is None:
        match = re.findall(r"(?:\\?boxed)\{([^{}]+)\}", str(raw_answer))
        if match:
            boxed_answer = match[-1].strip()
    last_integer = _last_integer(raw_answer)
    direct = str(raw_answer).strip()
    normalized_answer = boxed_answer if boxed_answer is not None else (last_integer if last_integer is not None else None)
    exact_match = direct == expected
    numeric_match = bool(upstream_numeric_match or normalized_answer == expected)
    token_count = len(generated_token_ids or [])
    eos_seen = bool(eos_token_id is not None and generated_token_ids and eos_token_id in generated_token_ids)
    possibly_truncated = bool(max_new_tokens and token_count >= max_new_tokens and not eos_seen)
    format_compliant = bool(
        exact_match
        or boxed_answer == expected
        or re.fullmatch(r"\s*(?:\\?boxed\{)?5\}?\s*", direct)
    )
    return {
        "raw_answer": raw_answer,
        "normalized_answer": normalized_answer,
        "normalized_numeric_answer": normalized_answer,
        "generated_token_count": token_count,
        "max_new_tokens": max_new_tokens or 0,
        "eos_seen": eos_seen,
        "possibly_truncated": possibly_truncated,
        "boxed_answer": boxed_answer,
        "last_integer": last_integer,
        "exact_match": exact_match,
        "numeric_match": numeric_match,
        "format_compliant": format_compliant,
        "answer_correct": numeric_match,
        "parser_error": parser_error,
        "diagnosis": _answer_diagnosis_labels(raw_answer, expected, boxed_answer, last_integer, exact_match, numeric_match, possibly_truncated),
    }


def _answer_diagnosis_labels(
    raw_answer: str,
    expected: str,
    boxed_answer: str | None,
    last_integer: str | None,
    exact_match: bool,
    numeric_match: bool,
    possibly_truncated: bool,
) -> list[str]:
    labels = []
    if possibly_truncated:
        labels.append("token_truncation")
    if expected in raw_answer and not numeric_match:
        labels.append("parser_failure")
    if numeric_match and not (exact_match or boxed_answer == expected):
        labels.append("format_noncompliance")
    if not numeric_match and last_integer is not None and last_integer != expected:
        labels.append("wrong_numeric_answer")
    if not numeric_match and last_integer is None:
        labels.append("no_numeric_answer")
    return labels


def _last_integer(text: str) -> str | None:
    matches = re.findall(r"(?<![\w.])-?\d+(?!\w)", str(text))
    return matches[-1] if matches else None


def _summarize_instrumentation(log: list[dict[str, Any]], rounds: int, decode_counts: dict[str, int]) -> dict[str, Any]:
    link_counts: dict[str, int] = {}
    inner_counts: dict[str, int] = {}
    for item in log:
        if item.get("kind") == "outer":
            link_counts[str(item.get("link"))] = link_counts.get(str(item.get("link")), 0) + 1
        if item.get("kind") == "inner":
            src = str(item.get("source_agent") or item.get("source"))
            inner_counts[src] = inner_counts.get(src, 0) + 1
    expected_outer31 = max(0, rounds - 1)
    closed_loop = link_counts.get("solver->planner", 0) == expected_outer31
    if rounds == 1:
        closed_loop = False
    native_latent = (
        link_counts.get("planner->critic", 0) >= rounds
        and link_counts.get("critic->solver", 0) >= rounds
        and link_counts.get("solver->planner", 0) == expected_outer31
        and inner_counts.get("planner", 0) >= rounds
        and inner_counts.get("critic", 0) >= rounds
        and inner_counts.get("solver", 0) >= expected_outer31
    )
    decode_ok = decode_counts.get("intermediate_decode_count", 0) == 0 and decode_counts.get("final_decode_count", 0) == 1
    return {
        "link_counts": link_counts,
        "inner_counts": inner_counts,
        "solver_to_planner_expected": expected_outer31,
        "solver_to_planner_observed": link_counts.get("solver->planner", 0),
        "closed_loop_verified": bool(rounds > 1 and closed_loop),
        "native_latent_verified": bool(native_latent),
        "native_recursive_execution_success": bool(native_latent and decode_ok),
        "planner_inner_called": inner_counts.get("planner", 0),
        "critic_inner_called": inner_counts.get("critic", 0),
        "solver_inner_called": inner_counts.get("solver", 0),
        "decode_call_count": decode_counts.get("decode_call_count", 0),
        "generate_call_count": decode_counts.get("generate_call_count", 0),
        "intermediate_decode_count": decode_counts.get("intermediate_decode_count", 0),
        "final_decode_count": decode_counts.get("final_decode_count", 0),
        "no_intermediate_decode": decode_counts.get("intermediate_decode_count", 0) == 0,
        "single_final_decode": decode_counts.get("final_decode_count", 0) == 1,
    }


def _classify_diagnostic(solver_baseline: dict[str, Any], cases: list[dict[str, Any]]) -> list[str]:
    labels: set[str] = set()
    if not solver_baseline.get("solver_baseline_execution_success"):
        labels.add("solver_baseline_failure")
    elif solver_baseline.get("solver_baseline_answer_correct") is False:
        labels.add("checkpoint_quality_variance")
    case_map = {case.get("case"): case for case in cases}
    if case_map.get("A", {}).get("answer_correct"):
        labels.add("token_truncation")
    if case_map.get("A") and not case_map["A"].get("answer_correct") and any(case.get("answer_correct") for case in cases if case.get("case") in {"B", "C"}):
        labels.add("single_round_quality_failure")
        labels.add("recursive_improvement_observed")
    if case_map.get("D") and case_map.get("C"):
        if case_map["C"].get("answer_correct") != case_map["D"].get("answer_correct"):
            labels.add("generation_parameter_sensitivity")
    for case in cases:
        if not case.get("native_execution_success"):
            labels.add("closed_loop_failure" if int(case.get("rounds") or 0) > 1 else "worker_upstream_divergence")
        parser_labels = case.get("parser", {}).get("diagnosis") or []
        for label in parser_labels:
            labels.add(str(label))
    if solver_baseline.get("solver_baseline_answer_correct") and cases and not any(case.get("answer_correct") for case in cases):
        labels.add("recursive_collaboration_quality_failure")
    if not labels:
        labels.add("unknown")
    return sorted(labels)


def _upstream_comparison_table() -> list[dict[str, Any]]:
    return [
        {
            "component": "planner_prompt",
            "upstream_function": "prompts.build_math_planner_prompt",
            "worker_function": "_planner_stage",
            "equivalent": True,
            "difference": "worker calls upstream builder directly for one question",
            "impact": "no prompt rewrite",
        },
        {
            "component": "critic_prompt",
            "upstream_function": "prompts.build_math_refiner_prompt_with_slot",
            "worker_function": "_critic_stage",
            "equivalent": True,
            "difference": "worker calls upstream builder directly",
            "impact": "latent slot preserved",
        },
        {
            "component": "solver_prompt",
            "upstream_function": "prompts.build_math_solver_prompt_with_slots",
            "worker_function": "_solver_feedback_stage/_solver_final_decode",
            "equivalent": True,
            "difference": "worker calls upstream builder directly",
            "impact": "boxed-answer instruction preserved",
        },
        {
            "component": "recursive_control_flow",
            "upstream_function": "inference_mas main recursive branch",
            "worker_function": "_run_native_matrix_case",
            "equivalent": True,
            "difference": "single-question in-memory diagnostic, no dataset loader",
            "impact": "no dataset/download; same latent link order",
        },
        {
            "component": "final_answer_parser",
            "upstream_function": "answer_utils.extract_boxed_answer/compare_answers",
            "worker_function": "_diagnose_answer",
            "equivalent": True,
            "difference": "adds last-integer diagnostic fallback",
            "impact": "fallback only labels canary; not official benchmark scoring",
        },
    ]


def _set_seed(seed: int) -> None:
    try:
        import random
        import torch

        random.seed(seed)
        torch.manual_seed(seed)
    except Exception:
        pass


def _sha256_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _git_status(root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-c", f"safe.directory={root}", "-C", str(root), "status", "--short"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""


def _status_has_tracked_changes(status: str) -> bool:
    for line in status.splitlines():
        if line and not line.startswith("?? "):
            return True
    return False


def _swap_used_bytes() -> int:
    info = _meminfo()
    total = int(info.get("SwapTotal", 0))
    free = int(info.get("SwapFree", 0))
    return max(0, total - free)


def _prepare_import_path(root: Path) -> None:
    inference = root / "inference"
    for path in (str(root), str(inference)):
        if path not in sys.path:
            sys.path.insert(0, path)


def _snapshot_path_for_repo(repo_id: str) -> Path:
    cache = Path(os.getenv("HUGGINGFACE_HUB_CACHE") or os.getenv("HF_HOME", "") + "/hub")
    slug = "models--" + repo_id.replace("/", "--")
    base = cache / slug / "snapshots"
    if not base.is_dir():
        return base / "missing"
    snapshots = sorted([p for p in base.iterdir() if p.is_dir()])
    return snapshots[-1] if snapshots else base / "missing"


def _revision_from_snapshot(path: Path) -> str | None:
    return path.name if path.is_dir() else None


def _files_under(path: Path) -> list[dict[str, Any]]:
    if not path.is_dir():
        return []
    out = []
    for item in sorted(path.rglob("*")):
        if "_plain_model_view" in item.parts:
            continue
        if item.is_file():
            out.append(
                {
                    "path": str(item.relative_to(path)),
                    "size_bytes": int(item.stat().st_size),
                    "is_symlink": item.is_symlink(),
                }
            )
    return out


def _outerlink_presence(files: set[str]) -> dict[str, bool]:
    return {
        "Planner-Critic": SEQUENTIAL_OUTER_FILES["outer_12"] in files,
        "Critic-Solver": SEQUENTIAL_OUTER_FILES["outer_23"] in files,
        "Solver-Planner": SEQUENTIAL_OUTER_FILES["outer_31"] in files,
    }


def _choose_cpu_dtype() -> dict[str, Any]:
    try:
        import torch
    except Exception as exc:
        return {"ok": False, "reason": "torch_unavailable", "error": str(exc)}
    probes = {}
    for name, dtype in (("float32", torch.float32), ("float16", torch.float16), ("bfloat16", torch.bfloat16)):
        try:
            lin = torch.nn.Linear(4, 4, dtype=dtype)
            x = torch.ones((2, 4), dtype=dtype)
            y = torch.nn.functional.gelu(lin(x))
            y = torch.nn.LayerNorm(4, dtype=dtype)(y)
            probes[name] = {"ok": bool(torch.isfinite(y.float()).all()), "error": None}
        except Exception as exc:
            probes[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    mem = _mem_available_bytes()
    fp32_estimate = 26_500_000_000
    if probes["float32"]["ok"] and fp32_estimate < int(mem * 0.70) and mem - fp32_estimate >= 10_000_000_000:
        return {
            "ok": True,
            "dtype_effective": "float32",
            "dtype_probe": probes,
            "mem_available_bytes": mem,
            "estimate_bytes": fp32_estimate,
            "reason": "float32_cpu_safe_margin",
        }
    for name in ("bfloat16", "float16"):
        if probes[name]["ok"]:
            return {
                "ok": True,
                "dtype_effective": name,
                "dtype_probe": probes,
                "mem_available_bytes": mem,
                "estimate_bytes": 14_600_000_000,
                "reason": f"{name}_cpu_probe_ok",
            }
    return {"ok": False, "reason": "cpu_dtype_unavailable", "dtype_probe": probes, "mem_available_bytes": mem}


def _mem_available_bytes() -> int:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 0


def _rss_bytes() -> int:
    try:
        import psutil

        return int(psutil.Process().memory_info().rss)
    except Exception:
        return 0


def _rss_peak_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value * 1024


def _wrap_adapter(module: Any, log: list[dict[str, Any]], kind: str, src: str, dst: str, round_index: int) -> None:
    original = module.forward

    def wrapped(x: Any, *args: Any, **kwargs: Any) -> Any:
        out = original(x, *args, **kwargs)
        log.append(
            {
                "kind": kind,
                "link": f"{src}->{dst}",
                "round": round_index,
                "source_agent": src,
                "destination_agent": dst,
                "input_shape": list(getattr(x, "shape", [])),
                "output_shape": list(getattr(out, "shape", [])),
                "dtype": str(getattr(out, "dtype", "")),
                "device": str(getattr(out, "device", "")),
                "norm_finite": _finite_norm(out),
            }
        )
        return out

    module.forward = wrapped


def _finite_norm(tensor: Any) -> bool:
    try:
        import torch

        return bool(torch.isfinite(tensor.float().norm()).item())
    except Exception:
        return False


class _OfflineGuard:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.network_attempted = False
        self.download_attempted = False
        self.unexpected_repo_request: list[str] = []

    def install(self) -> None:
        _prepare_import_path(self.root)
        self._patch_network()
        self._patch_hf()

    def _patch_network(self) -> None:
        real_socket = socket.socket
        guard = self

        class GuardedSocket(real_socket):  # type: ignore[misc, valid-type]
            def connect(self, *args: Any, **kwargs: Any) -> Any:
                guard.network_attempted = True
                raise RuntimeError("network disabled during RecursiveMAS canary")

            def connect_ex(self, *args: Any, **kwargs: Any) -> int:
                guard.network_attempted = True
                return 1

        socket.socket = GuardedSocket  # type: ignore[assignment]

    def _patch_hf(self) -> None:
        try:
            import huggingface_hub
        except Exception:
            return
        guard = self

        def local_only_snapshot_download(repo_id: str, *args: Any, **kwargs: Any) -> Any:
            if repo_id not in SEQUENTIAL_LIGHT_REPO_SET:
                guard.unexpected_repo_request.append(str(repo_id))
                guard.download_attempted = True
                raise RuntimeError(f"unexpected repository request: {repo_id}")
            snapshot = _snapshot_path_for_repo(repo_id)
            if not snapshot.is_dir():
                guard.download_attempted = True
                raise RuntimeError(f"local checkpoint snapshot missing: {repo_id}")
            return str(snapshot)

        huggingface_hub.snapshot_download = local_only_snapshot_download
        try:
            import hf_resolver  # type: ignore

            hf_resolver.snapshot_download = local_only_snapshot_download
        except Exception:
            pass


def _patch_network(state: dict[str, bool]) -> None:
    real_socket = socket.socket

    class GuardedSocket(real_socket):  # type: ignore[misc, valid-type]
        def connect(self, *args: Any, **kwargs: Any) -> Any:
            state["attempted"] = True
            raise RuntimeError("network connect disabled during RecursiveMAS import check")

        def connect_ex(self, *args: Any, **kwargs: Any) -> int:
            state["attempted"] = True
            return 1

    socket.socket = GuardedSocket  # type: ignore[assignment]


def _patch_download_and_model_load(module: Any, download: dict[str, bool], model_load: dict[str, bool]) -> None:
    if getattr(module, "__name__", "") == "huggingface_hub":
        def blocked_download(*args: Any, **kwargs: Any) -> Any:
            download["attempted"] = True
            raise RuntimeError("download disabled during RecursiveMAS import check")

        if hasattr(module, "snapshot_download"):
            module.snapshot_download = blocked_download
        if hasattr(module, "hf_hub_download"):
            module.hf_hub_download = blocked_download
    if getattr(module, "__name__", "") == "torch" and hasattr(module, "load"):
        real_load = module.load

        def guarded_load(*args: Any, **kwargs: Any) -> Any:
            model_load["attempted"] = True
            return real_load(*args, **kwargs)

        module.load = guarded_load
    if getattr(module, "__name__", "") == "transformers":
        def blocked_from_pretrained(*args: Any, **kwargs: Any) -> Any:
            model_load["attempted"] = True
            raise RuntimeError("from_pretrained disabled during RecursiveMAS import check")

        for attr in ("AutoModelForCausalLM", "AutoTokenizer"):
            obj = getattr(module, attr, None)
            if obj is not None and hasattr(obj, "from_pretrained"):
                obj.from_pretrained = blocked_from_pretrained


def _dependency_versions() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for module in DEPENDENCY_MODULES:
        try:
            imported = importlib.import_module(module)
            out[module] = {"import_ok": True, "version": getattr(imported, "__version__", "unknown")}
        except Exception as exc:
            out[module] = {"import_ok": False, "version": None, "error": f"{type(exc).__name__}: {exc}"}
    return out


def _required_file_status(root: Path) -> tuple[list[str], list[str]]:
    found = [item for item in REQUIRED_FILES if (root / item).is_file()]
    missing = [item for item in REQUIRED_FILES if item not in found]
    return found, missing


def _checkpoint_status(root: Path) -> dict[str, Any]:
    search_paths = [
        Path(os.getenv("HUGGINGFACE_HUB_CACHE") or ""),
        Path(os.getenv("HF_HOME") or "") / "hub",
        root / "checkpoints",
    ]
    files = []
    for path in search_paths:
        if not str(path) or not path.exists():
            continue
        for candidate in path.rglob("*"):
            if candidate.is_file() and candidate.suffix in {".bin", ".safetensors", ".pt", ".pth"}:
                files.append(str(candidate))
    return {
        "checkpoint_status": "missing" if not files else "partial",
        "checkpoint_files_found": files,
        "checkpoints_available": False,
    }


def _offline_mode() -> bool:
    return (
        os.getenv("HF_HUB_OFFLINE") == "1"
        and os.getenv("TRANSFORMERS_OFFLINE") == "1"
        and os.getenv("HF_DATASETS_OFFLINE") == "1"
    )


def _git_commit(root: Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-c", f"safe.directory={root}", "-C", str(root), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
