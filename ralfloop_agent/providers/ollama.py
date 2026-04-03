from __future__ import annotations

from dataclasses import dataclass
import json
import re

import requests


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

        if "ollama" in goal or "modelli" in goal or "models" in goal:
            return PlannerDecision(
                tool_name="sandbox_http_fetch",
                tool_input={"url": "http://127.0.0.1:11434/api/tags", "method": "GET"},
                why="Interrogo Ollama locale per osservare i modelli disponibili.",
            )

        return PlannerDecision(
            tool_name="sandbox_exec",
            tool_input={"command": "pwd && ls -la && find . -maxdepth 2 -type f | sort", "timeout_sec": 10},
            why="Fallback minimale per osservare l'ambiente di lavoro.",
        )


class OllamaPlanner:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "qwen2.5:7b",
        fallback: DeterministicPlanner | None = None,
        timeout_sec: int = 60,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.fallback = fallback or DeterministicPlanner()
        self.timeout_sec = timeout_sec

    def _clean_raw(self, text: str) -> str:
        text = text.strip()
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"^```json\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"^```\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        return text.strip()

    def _extract_json_object(self, text: str) -> dict:
        text = self._clean_raw(text)

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise json.JSONDecodeError("No JSON object found", text, 0)

        return json.loads(match.group(0))

    def _normalize_decision(self, parsed: dict, user_goal: str, iteration: int) -> PlannerDecision:
        tool_name = parsed.get("tool_name", "")
        tool_input = parsed.get("tool_input", {}) or {}
        why = parsed.get("why", "Decisione generata da Ollama.")

        if tool_name == "sandbox_read_file":
            if "file_path" in tool_input and "path" not in tool_input:
                tool_input["path"] = tool_input.pop("file_path")

        if tool_name == "sandbox_list_dir":
            if "file_path" in tool_input and "path" not in tool_input:
                tool_input["path"] = tool_input.pop("file_path")
            tool_input.setdefault("path", ".")

        goal = user_goal.lower()
        if ("hello" in goal or "ciao" in goal) and iteration == 0 and tool_name == "sandbox_exec":
            cmd = str(tool_input.get("command", "")).strip()
            if "out/hello_exec.txt" in cmd:
                if "mkdir -p out" not in cmd:
                    cmd = f"mkdir -p out && {cmd}"
                if "cat out/hello_exec.txt" not in cmd:
                    cmd = f"{cmd} && cat out/hello_exec.txt"
                tool_input["command"] = cmd
            tool_input.setdefault("timeout_sec", 20)

        if tool_name not in {"sandbox_exec", "sandbox_read_file", "sandbox_list_dir", "sandbox_http_fetch"}:
            raise ValueError(f"Unsupported tool_name from model: {tool_name}")

        return PlannerDecision(tool_name=tool_name, tool_input=tool_input, why=why)

    def choose_next_action(self, user_goal: str, iteration: int) -> PlannerDecision:
        goal = user_goal.lower()

        if "ollama" in goal or "modelli" in goal or "models" in goal:
            return PlannerDecision(
                tool_name="sandbox_http_fetch",
                tool_input={"url": "http://127.0.0.1:11434/api/tags", "method": "GET"},
                why="Interrogo Ollama locale per osservare i modelli disponibili.",
            )

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

        schema = {
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "enum": ["sandbox_exec", "sandbox_read_file", "sandbox_list_dir", "sandbox_http_fetch"],
                },
                "tool_input": {"type": "object"},
                "why": {"type": "string"},
            },
            "required": ["tool_name", "tool_input", "why"],
            "additionalProperties": False,
        }

        prompt = f"""Sei un planner per un agente sandbox.
Devi scegliere SOLO il prossimo passo minimo.

Regole:
- Non usare comandi distruttivi.
- Se l'obiettivo parla di hello o ciao:
  - iteration 0 => usa sandbox_exec per creare out/hello_exec.txt con contenuto esatto: hello from sandbox_exec
  - iteration 1 => usa sandbox_read_file con tool_input.path = out/hello_exec.txt
- Per sandbox_read_file usa SEMPRE la chiave path, non file_path.
- Se non sei sicuro, usa sandbox_exec con un comando innocuo di osservazione.

user_goal: {user_goal}
iteration: {iteration}
"""

        try:
            r = requests.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "format": schema,
                    "options": {
                        "temperature": 0
                    }
                },
                timeout=self.timeout_sec,
            )
            r.raise_for_status()
            data = r.json()
            raw = data.get("response", "")
            parsed = self._extract_json_object(raw)
            return self._normalize_decision(parsed, user_goal, iteration)
        except Exception:
            return self.fallback.choose_next_action(user_goal, iteration)
