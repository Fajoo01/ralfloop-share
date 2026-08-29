from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping

from .contracts import DestructiveLevel, ShellCapability


class ShellParserError(RuntimeError):
    pass


READ_COMMANDS = {"cat", "grep", "rg", "head", "tail", "wc", "test", "stat", "ls", "find"}
NETWORK_COMMANDS = {"curl", "wget", "ssh", "scp", "rsync", "nc", "ncat"}
DELETE_COMMANDS = {"rm", "rmdir", "unlink", "shred"}
WRITE_COMMANDS = {"cp", "mv", "mkdir", "touch", "tee", "install", "ln"}
_SAFE_DIRNAME_SUBSTITUTION = re.compile(r"^\$\(dirname ([A-Za-z0-9_./-]+)\)$")


def parser_binary() -> Path:
    configured = os.getenv("RALF_SHELL_JUDGE_PARSER")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "bin" / "ralf-shell-ast"


def parse_bash(command: str, *, binary: Path | None = None, timeout: float = 2.0) -> Mapping[str, Any]:
    target = binary or parser_binary()
    if not target.is_file() or not os.access(target, os.X_OK):
        raise ShellParserError("shell_parser_unavailable")
    try:
        proc = subprocess.run(
            [str(target)], input=command, text=True, capture_output=True,
            timeout=timeout, shell=False, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ShellParserError("shell_parser_failed") from exc
    try:
        payload = json.loads(proc.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ShellParserError("shell_parser_malformed") from exc
    if proc.returncode != 0 or payload.get("parse_ok") is not True:
        raise ShellParserError("bash_parse_error")
    return payload


def _literal(value: str) -> bool:
    return bool(value) and not any(char in value for char in "$`*?[]{}<>()")


def _resolve(value: str, cwd: Path, unresolved: list[str]) -> str | None:
    dirname = _SAFE_DIRNAME_SUBSTITUTION.fullmatch(value)
    if dirname:
        nested = _resolve(dirname.group(1), cwd, unresolved)
        return str(Path(nested).parent) if nested else None
    if not _literal(value) or value.startswith("-"):
        if value and not value.startswith("-"):
            unresolved.append("dynamic_path")
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    return str(candidate.resolve(strict=False))


def extract_capabilities(ast: Mapping[str, Any], *, cwd: str) -> ShellCapability:
    base = Path(cwd).resolve(strict=True)
    reads: list[str] = []
    writes: list[str] = []
    deletes: list[str] = []
    env: list[str] = []
    commands: list[tuple[str, ...]] = []
    unresolved = list(str(v) for v in ast.get("unresolved_elements") or ())
    network = False
    destructive = DestructiveLevel.LOW

    rows = ast.get("commands") or []
    effective_cwd = base
    for row in rows:
        argv = tuple(str(v) for v in row.get("argv") or ())
        if not argv:
            continue
        executable = Path(argv[0]).name
        assignments = row.get("assignments") or ()
        env.extend(str(item.get("name") or "") for item in assignments)
        if assignments:
            unresolved.append("environment_or_assignment")
        if executable == "command" and len(argv) > 1:
            argv = argv[1:]
            executable = Path(argv[0]).name
        elif executable == "busybox" and len(argv) > 1:
            argv = argv[1:]
            executable = Path(argv[0]).name
        elif executable == "env" and len(argv) > 1:
            index = 1
            while index < len(argv) and "=" in argv[index] and not argv[index].startswith("="):
                env.append(argv[index].split("=", 1)[0])
                unresolved.append("environment_or_assignment")
                index += 1
            if index < len(argv):
                argv = argv[index:]
                executable = Path(argv[0]).name
        commands.append(argv)
        operands = [arg for arg in argv[1:] if not arg.startswith("-")]
        if executable == "dirname" and len(operands) == 1:
            continue
        if executable == "cd":
            if len(operands) != 1:
                unresolved.append("unmodeled_cwd_change")
            else:
                destination = _resolve(operands[0], effective_cwd, unresolved)
                if destination:
                    effective_cwd = Path(destination)
            continue
        if executable in READ_COMMANDS:
            for operand in operands:
                resolved = _resolve(operand, effective_cwd, unresolved)
                if resolved: reads.append(resolved)
        elif executable in DELETE_COMMANDS:
            destructive = DestructiveLevel.HIGH
            for operand in operands:
                resolved = _resolve(operand, effective_cwd, unresolved)
                if resolved: deletes.append(resolved)
        elif executable in WRITE_COMMANDS:
            for operand in operands:
                resolved = _resolve(operand, effective_cwd, unresolved)
                if resolved: writes.append(resolved)
        if executable in NETWORK_COMMANDS:
            network = True
        if executable in {"eval", "source", "."}:
            unresolved.append("dynamic_code_execution")
        if executable in {"bash", "sh", "zsh"} and any(arg in {"-c", "-lc"} for arg in argv[1:]):
            unresolved.append("nested_shell")

    redirects: list[dict[str, object]] = []
    for redirect in ast.get("redirects") or ():
        operator = str(redirect.get("operator") or "")
        target = str(redirect.get("target") or "")
        data_redirect = bool(redirect.get("heredoc")) or operator == "<<<"
        resolved = None if data_redirect else _resolve(target, base, unresolved)
        item = {"operator": operator, "target": target, "resolved": resolved,
                "heredoc": bool(redirect.get("heredoc"))}
        redirects.append(item)
        if resolved:
            if operator in {"<", "<<", "<<<", "<>"}: reads.append(resolved)
            else: writes.append(resolved)

    substitutions = tuple(str(item.get("text") or "") for item in ast.get("substitutions") or ())
    safe_substitutions = bool(substitutions) and all(_SAFE_DIRNAME_SUBSTITUTION.fullmatch(item) for item in substitutions)
    if safe_substitutions:
        unresolved = [item for item in unresolved if item != "word_expansion:*syntax.CmdSubst"]
    elif substitutions:
        unresolved.append("command_substitution")
    if redirects and any(Path(argv[0]).name == "cd" for argv in commands if argv):
        unresolved.append("redirect_after_cwd_change")

    first = commands[0] if commands else ()
    return ShellCapability(
        executable=first[0] if first else "", argv=first, cwd=str(base),
        resolved_paths_read=tuple(dict.fromkeys(reads)),
        resolved_paths_write=tuple(dict.fromkeys(writes)),
        resolved_paths_delete=tuple(dict.fromkeys(deletes)),
        redirects=tuple(redirects), env_assignments=tuple(v for v in env if v),
        substitutions=substitutions, pipelines=int(ast.get("pipelines") or 0),
        subshells=int(ast.get("subshells") or 0),
        background_execution=bool(ast.get("background_execution")),
        network_or_external_effects=network,
        unresolved_elements=tuple(dict.fromkeys(unresolved)),
        destructive_level=destructive, commands=tuple(commands),
    )
