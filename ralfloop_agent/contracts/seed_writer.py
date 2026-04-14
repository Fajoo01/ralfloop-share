from __future__ import annotations

import json


def write_seed_files(state, ctx, adapter, sandbox, logger) -> None:
    seed_files = {
        "user_goal.txt": state.user_goal,
        "skill_context.txt": str(ctx.get("skill_context", "") or ""),
        "extra_context.json": json.dumps(ctx.get("extra_context", {}) or {}, ensure_ascii=False, indent=2),
        "README_CONTEXT.txt": (
            "Initial context files available in the workspace:\n"
            "- user_goal.txt\n"
            "- skill_context.txt\n"
            "- extra_context.json\n"
            "Read only these files for initial context unless you create new files yourself.\n"
        ),
    }

    for seed_path, seed_content in seed_files.items():
        seed_result = adapter.write_file(sandbox, path=seed_path, content=seed_content)
        logger.log(
            task_id=state.task_id,
            iteration=state.iteration,
            tool_name="sandbox_write_file",
            tool_input={"path": seed_path},
            tool_output=seed_result.model_dump(),
            decision="seed",
        )
