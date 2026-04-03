from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PlannerDecision:
    tool_name: str
    tool_input: dict
    why: str


class DeterministicPlanner:
    """Planner minimale per MVP: serve a validare il loop senza LLM reale."""

    def choose_next_action(self, user_goal: str, iteration: int) -> PlannerDecision:
        goal = user_goal.lower()
        if "hello" in goal or "ciao" in goal:
            if iteration == 0:
                return PlannerDecision(
                    tool_name="sandbox_write_file",
                    tool_input={"path": "/workspace/out/hello.txt", "content": "hello from ralfloop\n"},
                    why="Creo un artefatto minimo per validare scrittura e lifecycle.",
                )
            return PlannerDecision(
                tool_name="sandbox_read_file",
                tool_input={"path": "/workspace/out/hello.txt"},
                why="Verifico il contenuto scritto prima di fermarmi.",
            )
        return PlannerDecision(
            tool_name="sandbox_exec",
            tool_input={"command": "pwd && ls -la", "timeout_sec": 10},
            why="Fallback minimale per osservare l'ambiente di lavoro.",
        )
