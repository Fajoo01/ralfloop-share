#!/usr/bin/env bash
set -euo pipefail
cd /home/sibilla-cumana/ralfloop_agent_scaffold
python3 -m py_compile openshell_backend/skill_grammar_rag.py
PYTHONPATH=. python3 tests/grammar/run_grammar_tests.py
