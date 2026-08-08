from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any

from .contracts import CompactRoute
from .evolver import Candidate, EvolutionArchive, EvolutionSpec, ProgramDatabase, compute_fitness
from .media import AudiobookFactory, SocialMediaFactory, VisualRagWorker
from .policy import RalfPolicy
from .resources import ResourceManager
from .router import FunctionGemmaClient, LocalRouter, ToolRegistry
from .sandbox import BubblewrapSandbox, TestVector
from .store import ArtifactStore, VersionedCache, atomic_write, canonical_json


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE = Path(os.environ.get("RALF_LOCAL_ARCH_STATE", "~/.local/state/ralf/local-arch")).expanduser()


def add_local_arch_parsers(sub: argparse._SubParsersAction) -> None:
    router = sub.add_parser("router", help="compact local capability router")
    router_sub = router.add_subparsers(dest="router_action", required=True)
    for name in ("status", "health", "catalog", "metrics"):
        router_sub.add_parser(name)
    classify = router_sub.add_parser("classify")
    classify.add_argument("text", nargs="+")
    classify.add_argument("--dry-run", action="store_true")
    explain = router_sub.add_parser("explain")
    explain.add_argument("text", nargs="+")
    benchmark = router_sub.add_parser("benchmark")
    benchmark.add_argument("--dataset", default=str(PROJECT_ROOT / "config/router_benchmark_templates_v1.json"))
    benchmark.add_argument("--output")

    evolve = sub.add_parser("evolve", help="local evolutionary program optimizer")
    evolve_sub = evolve.add_subparsers(dest="evolve_action", required=True)
    create = evolve_sub.add_parser("create")
    create.add_argument("spec")
    create.add_argument("--dry-run", action="store_true")
    for name in ("status", "pause", "resume", "best", "metrics", "stop", "run"):
        item = evolve_sub.add_parser(name)
        item.add_argument("experiment")
        if name == "run":
            item.add_argument("--dry-run", action="store_true")
    lineage = evolve_sub.add_parser("lineage")
    lineage.add_argument("candidate")
    compare = evolve_sub.add_parser("compare")
    compare.add_argument("a")
    compare.add_argument("b")
    export = evolve_sub.add_parser("export")
    export.add_argument("candidate")
    export.add_argument("--output")

    visual = sub.add_parser("visual", help="region-first multimodal retrieval")
    visual_sub = visual.add_subparsers(dest="visual_action", required=True)
    ingest = visual_sub.add_parser("ingest")
    ingest.add_argument("file")
    ingest.add_argument("--dry-run", action="store_true")
    query = visual_sub.add_parser("query")
    query.add_argument("document")
    query.add_argument("question", nargs="+")

    audiobook = sub.add_parser("audiobook", help="artifact-based audiobook pipeline")
    audiobook_sub = audiobook.add_subparsers(dest="audiobook_action", required=True)
    audio_create = audiobook_sub.add_parser("create")
    audio_create.add_argument("file")
    audio_create.add_argument("--dry-run", action="store_true")
    audio_status = audiobook_sub.add_parser("status")
    audio_status.add_argument("id")

    media = sub.add_parser("media", help="deterministic social media composition")
    media_sub = media.add_subparsers(dest="media_action", required=True)
    for name in ("image", "video"):
        item = media_sub.add_parser(name)
        item.add_argument("spec")
        item.add_argument("--dry-run", action="store_true")
    compose = media_sub.add_parser("compose")
    compose.add_argument("spec")
    compose.add_argument("--execute", action="store_true")


def run_local_arch(args: argparse.Namespace) -> int:
    if args.command == "router":
        result = _router(args)
    elif args.command == "evolve":
        result = _evolve(args)
    elif args.command == "visual":
        result = _visual(args)
    elif args.command == "audiobook":
        result = _audiobook(args)
    elif args.command == "media":
        result = _media(args)
    else:
        raise ValueError("unknown_local_arch_command")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def _registry() -> ToolRegistry:
    return ToolRegistry.load(PROJECT_ROOT / "config/local_arch_tools_v1.json")


def _router_instance() -> LocalRouter:
    cache = VersionedCache(DEFAULT_STATE / "cache/router")
    return LocalRouter(_registry(), FunctionGemmaClient(), cache)


def _router(args: argparse.Namespace) -> dict[str, Any]:
    registry = _registry()
    client = FunctionGemmaClient()
    if args.router_action in {"status", "health"}:
        resource = ResourceManager(DEFAULT_STATE).check("tiny_cpu")
        return {
            "v": 1,
            "healthy": client.health(),
            "endpoint": client.base_url,
            "model_present": _functiongemma_model() is not None,
            "resource": asdict(resource),
            "fallback": "deterministic_then_colibri",
        }
    if args.router_action == "catalog":
        return {"schema_version": registry.version, "tools": [asdict(item) for item in registry.tools]}
    if args.router_action in {"classify", "explain"}:
        text = " ".join(args.text)
        decision = _router_instance().classify(text, dry_run=getattr(args, "dry_run", False))
        return decision.as_dict()
    if args.router_action == "metrics":
        metrics = DEFAULT_STATE / "router-metrics.jsonl"
        rows = [json.loads(line) for line in metrics.read_text(encoding="utf-8").splitlines()] if metrics.exists() else []
        return {"requests": len(rows), "latest": rows[-20:]}
    if args.router_action == "benchmark":
        result = _router_benchmark(Path(args.dataset))
        if args.output:
            atomic_write(Path(args.output), canonical_json(result) + b"\n")
        return result
    raise ValueError("unknown_router_action")


def _router_benchmark(path: Path) -> dict[str, Any]:
    templates = json.loads(path.read_text(encoding="utf-8"))
    variations = templates.get("variations", ["{q}"])
    cases = [(template["a"], variation.format(q=template["q"])) for template in templates["templates"] for variation in variations]
    router = _router_instance()
    started = time.perf_counter_ns()
    correct = 0
    invalid = 0
    latencies: list[float] = []
    confusion: dict[str, dict[str, int]] = {}
    for expected, text in cases:
        try:
            decision = router.classify(text, dry_run=True)
            actual = decision.route.a
            latencies.append(decision.latency_ms)
        except Exception:
            invalid += 1
            actual = "INVALID"
        correct += actual == expected
        confusion.setdefault(expected, {})[actual] = confusion.setdefault(expected, {}).get(actual, 0) + 1
    per_class = {}
    actions = sorted({item[0] for item in cases})
    for action in actions:
        tp = confusion.get(action, {}).get(action, 0)
        expected_total = sum(confusion.get(action, {}).values())
        predicted_total = sum(row.get(action, 0) for row in confusion.values())
        precision = tp / predicted_total if predicted_total else 0.0
        recall = tp / expected_total if expected_total else 0.0
        per_class[action] = {"precision": precision, "recall": recall, "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0}
    ordered = sorted(latencies)
    return {
        "v": 1, "cases": len(cases), "accuracy": correct / len(cases),
        "macro_f1": sum(item["f1"] for item in per_class.values()) / len(per_class), "per_class": per_class,
        "invalid_json": invalid, "latency_p50_ms": ordered[len(ordered) // 2] if ordered else None,
        "latency_p95_ms": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] if ordered else None,
        "wall_ms": (time.perf_counter_ns() - started) / 1_000_000,
        "mode": "dry_run_no_tool_execution", "real_functiongemma": FunctionGemmaClient().health(),
    }


def _evolve(args: argparse.Namespace) -> dict[str, Any]:
    state = DEFAULT_STATE / "evolver"
    store = ArtifactStore(state / "artifacts")
    db = ProgramDatabase(state / "population.sqlite3")
    try:
        action = args.evolve_action
        if action == "create":
            raw = json.loads(Path(args.spec).read_text(encoding="utf-8"))
            spec = EvolutionSpec.from_mapping(raw)
            if getattr(args, "dry_run", False):
                return {"valid": True, "problem_id": spec.problem_id, "dry_run": True}
            store.get(spec.seed_artifact)
            store.get(spec.acceptance_tests)
            experiment = db.create_experiment(spec)
            target = state / "experiments" / f"{experiment}.json"
            atomic_write(target, canonical_json(raw) + b"\n")
            return {"experiment": experiment, "status": "created", "spec_hash": target.stem}
        if action in {"pause", "resume", "stop"}:
            status = {"pause": "paused", "resume": "created", "stop": "stopped"}[action]
            changed = db.connection.execute("UPDATE experiments SET status=? WHERE experiment_id=?", (status, args.experiment)).rowcount
            db.connection.commit()
            return {"experiment": args.experiment, "status": status, "found": bool(changed)}
        if action == "status":
            row = db.connection.execute("SELECT * FROM experiments WHERE experiment_id=?", (args.experiment,)).fetchone()
            return dict(row) if row else {"error": "experiment_not_found"}
        if action == "run":
            return _run_seed(args.experiment, db, store, dry_run=args.dry_run)
        if action == "best":
            return {"experiment": args.experiment, "best": db.best(args.experiment)}
        if action == "metrics":
            rows = db.connection.execute("SELECT generation,COUNT(*) candidates,SUM(correctness) valid,MAX(fitness) best FROM programs WHERE experiment_id=? GROUP BY generation ORDER BY generation", (args.experiment,)).fetchall()
            return {"experiment": args.experiment, "generations": [dict(row) for row in rows]}
        if action == "lineage":
            return {"candidate": args.candidate, "lineage": db.lineage(args.candidate)}
        if action == "compare":
            rows = db.connection.execute("SELECT * FROM programs WHERE candidate_id IN (?,?)", (args.a, args.b)).fetchall()
            return {"candidates": [dict(row) for row in rows]}
        if action == "export":
            row = db.connection.execute("SELECT * FROM programs WHERE candidate_id=?", (args.candidate,)).fetchone()
            if not row:
                return {"error": "candidate_not_found"}
            if args.output:
                atomic_write(Path(args.output), canonical_json(dict(row)) + b"\n")
            return dict(row)
    finally:
        db.close()
    raise ValueError("unknown_evolve_action")


def _run_seed(experiment: str, db: ProgramDatabase, store: ArtifactStore, *, dry_run: bool) -> dict[str, Any]:
    spec_path = DEFAULT_STATE / "evolver/experiments" / f"{experiment}.json"
    spec = EvolutionSpec.from_mapping(json.loads(spec_path.read_text(encoding="utf-8")))
    source = store.get(spec.seed_artifact).decode("utf-8")
    raw_tests = json.loads(store.get(spec.acceptance_tests))
    tests = [TestVector(**item) for item in raw_tests]
    sandbox = BubblewrapSandbox()
    if dry_run:
        return {"experiment": experiment, "sandbox": sandbox.command("./candidate"), "tests": len(tests), "executed": False}
    result = sandbox.evaluate(source, tests)
    fitness = compute_fitness(result)
    candidate_id = f"cand-{result.source_hash[:16]}"
    candidate = Candidate(candidate_id, None, 0, result.source_hash, "seed", fitness)
    db.add_candidate(candidate, result, experiment_id=experiment)
    archive = EvolutionArchive(store).save_candidate(
        source=source, patch="", compile_log=result.failure or "compile_ok", result=result, fitness=fitness,
        lineage={"candidate_id": candidate_id, "generation": 0}, metadata={"experiment": experiment, "candidate": candidate_id},
    )
    for kind, ref in archive.items():
        db.connection.execute("INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?)", (ref, candidate_id, kind, len(store.get(ref))))
    db.connection.execute("UPDATE experiments SET status=? WHERE experiment_id=?", ("ready" if result.correct else "failed", experiment))
    db.connection.commit()
    return {"experiment": experiment, "candidate": candidate_id, "evaluation": result.as_dict(), "fitness": asdict(fitness), "artifacts": archive}


def _visual(args: argparse.Namespace) -> dict[str, Any]:
    worker = VisualRagWorker(DEFAULT_STATE / "visual")
    if args.visual_action == "ingest":
        return worker.ingest(args.file, dry_run=args.dry_run)
    return worker.query(args.document, " ".join(args.question))


def _audiobook(args: argparse.Namespace) -> dict[str, Any]:
    store = ArtifactStore(DEFAULT_STATE / "artifacts")
    if args.audiobook_action == "create":
        result = AudiobookFactory(store, DEFAULT_STATE / "cache/tts").plan(args.file, dry_run=args.dry_run)
        if result.get("artifact"):
            atomic_write(DEFAULT_STATE / "audiobooks" / f"{result['artifact'].split(':')[1][:16]}.json", canonical_json(result) + b"\n")
        return result
    target = DEFAULT_STATE / "audiobooks" / f"{args.id}.json"
    return json.loads(target.read_text(encoding="utf-8")) if target.exists() else {"error": "audiobook_not_found"}


def _media(args: argparse.Namespace) -> dict[str, Any]:
    factory = SocialMediaFactory(ArtifactStore(DEFAULT_STATE / "artifacts"))
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    if args.media_action == "image":
        return factory.image(spec, dry_run=args.dry_run)
    if args.media_action == "video":
        return factory.video(spec, dry_run=args.dry_run)
    raw_output = Path(str(spec.get("output", "output.mp4")))
    if raw_output.is_absolute() or raw_output.name != str(raw_output) or ".." in raw_output.parts:
        raise ValueError("media_output_must_be_filename")
    safe_output = DEFAULT_STATE / "media" / raw_output.name
    safe_output.parent.mkdir(parents=True, exist_ok=True)
    safe_spec = dict(spec)
    safe_spec["output"] = str(safe_output)
    command = factory.compose_command(safe_spec)
    if not args.execute:
        return {"command": command, "executed": False, "publish": False}
    result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=300)
    artifact = None
    if result.returncode == 0 and safe_output.is_file():
        artifact = factory.store.put(safe_output.read_bytes(), media_type="video/mp4", provenance={"pipeline": "media_compose", "command": command}).ref
    return {"command": command, "executed": True, "returncode": result.returncode, "stderr": result.stderr[-2000:], "artifact": artifact, "publish": False}


def _functiongemma_model() -> str | None:
    configured = os.environ.get("RALF_FUNCTIONGEMMA_MODEL")
    candidates = [
        Path(configured) if configured else None,
        Path("/home/sibilla-cumana/Dati/ralfloop-models/functiongemma-270m/functiongemma-270m-it-q8_0.gguf"),
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return str(candidate)
    return None
