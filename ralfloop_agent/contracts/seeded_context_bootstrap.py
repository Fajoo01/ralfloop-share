from __future__ import annotations

from ralfloop_agent.core.decisions import ActionDecision


def choose_seeded_context_action(state, choose_next_action_fn):
    goal_low = (state.user_goal or "").lower()

    if "user_goal.txt" in goal_low and "skill_context.txt" in goal_low:
        read_paths = set()
        for mem in state.memory:
            content = getattr(mem, "content", "")
            if content.startswith("file_read::"):
                try:
                    _, path, _body = content.split("::", 2)
                except ValueError:
                    continue
                read_paths.add(path.strip())

        if "user_goal.txt" not in read_paths:
            return ActionDecision(
                tool_name="sandbox_read_file",
                tool_input={"path": "user_goal.txt"},
                why="Leggo user_goal.txt per recuperare il contesto richiesto",
            )

        if "skill_context.txt" not in read_paths:
            return ActionDecision(
                tool_name="sandbox_read_file",
                tool_input={"path": "skill_context.txt"},
                why="Leggo skill_context.txt per recuperare il contesto richiesto",
            )

    return choose_next_action_fn(state.user_goal, state.iteration)
