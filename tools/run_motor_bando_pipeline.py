#!/usr/bin/env python3
from __future__ import annotations

import argparse
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
)
from ralfloop_agent.integration.motor_bando_pipeline import (
    MotorBandoPipeline,
    MotorBandoPipelineConfig,
)
from ralfloop_agent.integration.motor_prompt_budget import (
    count_rendered_tokens,
    render_ds41_judge_prompt,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the fused Bot-tazzi Motor bando pipeline")
    parser.add_argument("case_json", type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:19196")
    parser.add_argument("--max-rendered-tokens", type=int, default=360)
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument(
        "--ds4-bin",
        default="/home/sibilla-cumana/src/ds4-main-lowvram-port/ds4",
    )
    parser.add_argument(
        "--model",
        default="/home/sibilla-cumana/Dati/ds4-v41/DeepSeek-V4.1-Flash-Q2.gguf",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--no-grammar", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = json.loads(args.case_json.read_text(encoding="utf-8"))
    case = JudgeCase.model_validate(payload)

    def token_counter(chunk: JudgeCase) -> int:
        return count_rendered_tokens(
            render_ds41_judge_prompt(chunk),
            ds4_binary=args.ds4_bin,
            model_path=args.model,
        )

    judge = BotTazziMotorJudge(BotTazziMotorJudgeConfig(
        base_url=args.url,
        timeout_sec=args.timeout,
    ))
    pipeline = MotorBandoPipeline(
        token_counter=token_counter,
        judge=judge,
        config=MotorBandoPipelineConfig(
            max_rendered_tokens=args.max_rendered_tokens,
            use_grammar=not args.no_grammar,
        ),
    )
    result = pipeline.run(case).as_dict()
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
