#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ralfloop_agent.unified_assistant.platform import (
    CapabilityDescriptor, CapabilityPermission, CapabilityRegistry, PromotionState,
    RetrievalEvalCase, evaluate_retrieval,
)


ROWS = (
    ("arci_verify_membership", "arci", ("socio", "tessera", "membership")),
    ("jellyfin_get_user", "jellyfin", ("jellyfin", "utente", "account")),
    ("bandi_get_deadlines", "bandi", ("bandi", "scadono", "scadenze")),
    ("memory_get_practice", "memory", ("pratica", "punto", "stato")),
    ("memory_get_timeline", "memory", ("timeline", "cambiato", "cronologia")),
    ("memory_find_previous_cases", "memory", ("simile", "precedente", "gia")),
    ("tiremm_get_blocked", "tiremm_admin", ("bloccato", "bloccate", "blocker")),
    ("tiremm_get_next_actions", "tiremm_admin", ("prossima", "azione", "fare")),
    ("tiremm_get_sources", "tiremm_admin", ("fonte", "sai", "evidenza")),
    ("runtsuite_list_projects", "runtsuite", ("runtsuite", "progetti")),
    ("runtsuite_list_meetings", "runtsuite", ("runtsuite", "riunioni")),
    ("jellyfin_list_users", "jellyfin", ("jellyfin", "utenti", "elenco")),
    ("arci_list_cards", "arci", ("arci", "tessere", "elenco")),
)
PROMPTS = (
    ("controlla socio membership", "arci_verify_membership"),
    ("trova account utente jellyfin", "jellyfin_get_user"),
    ("quali bandi scadono", "bandi_get_deadlines"),
    ("a che punto pratica", "memory_get_practice"),
    ("cosa e cambiato timeline", "memory_get_timeline"),
    ("caso simile precedente", "memory_find_previous_cases"),
    ("cosa risulta bloccato", "tiremm_get_blocked"),
    ("qual e prossima azione", "tiremm_get_next_actions"),
    ("da quale fonte lo sai", "tiremm_get_sources"),
    ("progetti runtsuite", "runtsuite_list_projects"),
    ("riunioni runtsuite", "runtsuite_list_meetings"),
    ("elenco utenti jellyfin", "jellyfin_list_users"),
    ("elenco tessere arci", "arci_list_cards"),
)
VARIANTS = ("", " per Tiremm", " adesso", " per favore", " stato corrente", " controllo", " amministrazione", " verifica")


def main() -> int:
    descriptors = tuple(CapabilityDescriptor(
        capability_id=identity, server_id=f"{domain}.mcp", domain=domain,
        name=identity, description=f"Semantic {identity}", keywords=keywords,
        permission=CapabilityPermission.READ, source_system=domain, version="1",
        enabled=True, promotion=PromotionState.READ_ONLY, health="ok",
        input_schema={"type": "object"}, output_schema={"type": "object"},
    ) for identity, domain, keywords in ROWS)
    registry = CapabilityRegistry(descriptors)
    cases = tuple(RetrievalEvalCase(f"case-{index:03d}", prompt + suffix, frozenset({expected})) for index, (prompt, expected, suffix) in enumerate(((prompt, expected, suffix) for prompt, expected in PROMPTS for suffix in VARIANTS), 1))
    metrics = evaluate_retrieval(registry, cases, k=3)
    all_tools_per_case = sum(math.ceil(len(json.dumps({"name": row.name, "description": row.description, "input": row.input_schema, "output": row.output_schema}, sort_keys=True, separators=(",", ":")).encode()) / 4) for row in descriptors)
    payload = {**metrics.__dict__, "all_tools_schema_tokens": all_tools_per_case * len(cases)}
    print(json.dumps(payload, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
