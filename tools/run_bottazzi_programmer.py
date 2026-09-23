#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path

from ralfloop_agent.programmer import ProgrammerAgent, ProgrammerConfig, ProgrammerState


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Bot-tazzi Programmatore on one isolated Git worktree")
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--validator", default="git diff --check")
    parser.add_argument("--root", action="append", default=[], help="Allowed worktree root; repeatable. Overrides env roots.")
    parser.add_argument("--protected-glob", action="append", default=[])
    parser.add_argument("--allow-test-changes", action="store_true")
    parser.add_argument("--allow-worker-shell", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    config = ProgrammerConfig.from_environment(
        workdir=args.workdir,
        task=args.task,
        validator_command=args.validator,
        allow_test_changes=args.allow_test_changes,
        protected_globs=tuple(args.protected_glob),
        allow_worker_shell=args.allow_worker_shell,
    )
    if args.root:
        config = replace(config, allowed_roots=tuple(Path(item) for item in args.root))
    result = ProgrammerAgent(config).run()
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.state is ProgrammerState.CANDIDATE_READY else 2


if __name__ == "__main__":
    raise SystemExit(main())
