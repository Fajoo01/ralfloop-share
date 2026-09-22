from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ralfloop_agent.integration.bottazzi_motor_judge import (
    BotTazziMotorJudge,
    BotTazziMotorJudgeConfig,
    JudgeCase,
    judge_case_digest,
)
from ralfloop_agent.integration.motor_bando_pipeline import (
    BandoChunkOutcome,
    BandoPipelineOutcome,
    MotorBandoPipelineConfig,
    aggregate_bando_outcomes,
    build_bando_chunks,
)
from ralfloop_agent.integration.motor_prompt_budget import (
    count_rendered_tokens,
    render_ds41_judge_prompt,
)
from ralfloop_agent.integration.motor_semif_fast_gate import (
    Qwen35SemIfScorer,
    SemIfFastGateConfig,
    SemIfScore,
    semantic_fast_pass_allowed,
    semantic_fast_pass_outcome,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Two-phase SemIf -> Motor bando pipeline")
    parser.add_argument("phase", choices=("triage", "escalate"))
    parser.add_argument("case_json", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--triage-report", type=Path)
    parser.add_argument("--semif-url", default="http://127.0.0.1:19237")
    parser.add_argument("--semif-timeout", type=float, default=15.0)
    parser.add_argument("--semif-fast-pass-threshold", type=float, default=0.97)
    parser.add_argument("--motor-url", default="http://127.0.0.1:19196")
    parser.add_argument("--motor-timeout", type=float, default=240.0)
    parser.add_argument("--max-rendered-tokens", type=int, default=360)
    parser.add_argument(
        "--ds4-bin",
        default="/home/sibilla-cumana/src/ds4-main-lowvram-port/ds4",
    )
    parser.add_argument(
        "--model",
        default="/home/sibilla-cumana/Dati/ds4-v41/DeepSeek-V4.1-Flash-Q2.gguf",
    )
    parser.add_argument("--no-grammar", action="store_true")
    return parser.parse_args()


def _load_case(path: Path) -> JudgeCase:
    return JudgeCase.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _case_key(case_id: str) -> str:
    return hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:12]


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _build_chunks(args: argparse.Namespace, case: JudgeCase):
    def token_counter(chunk: JudgeCase) -> int:
        return count_rendered_tokens(
            render_ds41_judge_prompt(chunk),
            ds4_binary=args.ds4_bin,
            model_path=args.model,
        )
    return build_bando_chunks(
        case,
        token_counter=token_counter,
        config=MotorBandoPipelineConfig(
            max_rendered_tokens=args.max_rendered_tokens,
            use_grammar=not args.no_grammar,
        ),
    )


def _triage(args: argparse.Namespace, case: JudgeCase, chunks) -> int:
    scorer = Qwen35SemIfScorer(SemIfFastGateConfig(
        base_url=args.semif_url,
        timeout_sec=args.semif_timeout,
        fast_pass_threshold=args.semif_fast_pass_threshold,
    ))
    rows = []
    for chunk in chunks:
        try:
            score = scorer.score(chunk.case)
            fast = semantic_fast_pass_allowed(
                chunk.case, score, threshold=args.semif_fast_pass_threshold,
            )
            row = {
                "index": chunk.index,
                "case_key": _case_key(chunk.case.case_id),
                "rendered_tokens": chunk.rendered_tokens,
                "decision": score.decision,
                "probabilities": score.probabilities,
                "pass_probability": score.pass_probability,
                "latency_ms": score.latency_ms,
                "prompt_sha256": score.prompt_sha256,
                "fast_pass": fast,
                "escalate": not fast,
            }
        except Exception as exc:
            row = {
                "index": chunk.index,
                "case_key": _case_key(chunk.case.case_id),
                "rendered_tokens": chunk.rendered_tokens,
                "decision": None,
                "probabilities": {},
                "pass_probability": 0.0,
                "latency_ms": None,
                "prompt_sha256": None,
                "fast_pass": False,
                "escalate": True,
                "error": type(exc).__name__,
            }
        rows.append(row)
    fast_count = sum(row["fast_pass"] for row in rows)
    payload = {
        "schema_version": 1,
        "phase": "semif_triage",
        "case_digest": judge_case_digest(case),
        "case_key": _case_key(case.case_id),
        "threshold": args.semif_fast_pass_threshold,
        "chunk_count": len(chunks),
        "fast_pass_chunks": fast_count,
        "escalation_chunks": len(chunks) - fast_count,
        "total_rendered_tokens": sum(chunk.rendered_tokens for chunk in chunks),
        "max_chunk_tokens": max((chunk.rendered_tokens for chunk in chunks), default=0),
        "chunks": rows,
    }
    _write(args.output, payload)
    return 0


def _score_from_row(row: dict) -> SemIfScore:
    return SemIfScore(
        decision=str(row["decision"]),
        probabilities={str(k): float(v) for k, v in dict(row["probabilities"]).items()},
        latency_ms=float(row.get("latency_ms") or 0.0),
        prompt_sha256=str(row.get("prompt_sha256") or ""),
    )


def _escalate(args: argparse.Namespace, case: JudgeCase, chunks) -> int:
    if args.triage_report is None:
        raise SystemExit("--triage-report is required for escalate")
    triage = json.loads(args.triage_report.read_text(encoding="utf-8"))
    if triage.get("case_digest") != judge_case_digest(case):
        raise SystemExit("triage report case digest mismatch")
    if int(triage.get("chunk_count", -1)) != len(chunks):
        raise SystemExit("triage report chunk count mismatch")
    triage_rows = {int(row["index"]): row for row in triage.get("chunks", [])}
    if set(triage_rows) != {chunk.index for chunk in chunks}:
        raise SystemExit("triage report chunk index mismatch")

    motor = BotTazziMotorJudge(BotTazziMotorJudgeConfig(
        base_url=args.motor_url,
        timeout_sec=args.motor_timeout,
    ))
    rows: list[BandoChunkOutcome] = []
    for chunk in chunks:
        triage_row = triage_rows[chunk.index]
        if triage_row.get("case_key") != _case_key(chunk.case.case_id):
            raise SystemExit(f"triage report case key mismatch at chunk {chunk.index}")
        if int(triage_row.get("rendered_tokens", -1)) != chunk.rendered_tokens:
            raise SystemExit(f"triage report token mismatch at chunk {chunk.index}")
        if triage_row.get("fast_pass") is True:
            score = _score_from_row(triage_row)
            outcome = semantic_fast_pass_outcome(chunk.case, score)
        else:
            outcome = motor.judge(chunk.case)
        rows.append(BandoChunkOutcome(chunk=chunk, outcome=outcome))

    verdict, gate = aggregate_bando_outcomes(case, rows)
    result = BandoPipelineOutcome(
        case_key=_case_key(case.case_id),
        case_digest=judge_case_digest(case),
        chunks=tuple(rows),
        verdict=verdict,
        gate=gate,
    ).as_dict()
    result["phase"] = "motor_escalation"
    result["triage_report_sha256"] = hashlib.sha256(
        args.triage_report.read_bytes()
    ).hexdigest()
    _write(args.output, result)
    return 0


def main() -> int:
    args = parse_args()
    case = _load_case(args.case_json)
    chunks = _build_chunks(args, case)
    if args.phase == "triage":
        return _triage(args, case, chunks)
    return _escalate(args, case, chunks)


if __name__ == "__main__":
    raise SystemExit(main())
