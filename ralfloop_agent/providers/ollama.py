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
                    tool_name="sandbox_exec",
                    tool_input={
                        "command": "mkdir -p out && echo 'hello from sandbox_exec' > out/hello_exec.txt && cat out/hello_exec.txt",
                        "timeout_sec": 20,
                    },
                    why="Creo il file tramite exec e verifico subito che il comando abbia funzionato.",
                )
            return PlannerDecision(
                tool_name="sandbox_read_file",
                tool_input={"path": "out/hello_exec.txt"},
                why="Rileggo il file creato per confermare il contenuto finale.",
            )

        return PlannerDecision(
            tool_name="sandbox_exec",
            tool_input={"command": "pwd && ls -la && find . -maxdepth 2 -type f | sort", "timeout_sec": 10},
            why="Fallback minimale per osservare l'ambiente di lavoro.",
        )
