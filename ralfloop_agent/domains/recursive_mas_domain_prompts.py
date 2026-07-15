from __future__ import annotations


PLANNER_SLOT = "<<LATENT_PLANNER_SLOT>>"
REFINED_SLOT = "<<LATENT_REFINED_SLOT>>"
FEEDBACK_SLOT = "<<LATENT_FEEDBACK_SLOT>>"


def build_domain_planner_prompt(question: str) -> str:
    return (
        "You are the planner in a native latent domain-reasoning system.\n"
        "The DOMAIN_INPUT JSON below is authoritative.\n"
        "Identify issues; separate facts and rules; select only supplied source/rule IDs; "
        "form at least two interpretations; identify missing information.\n"
        "Do not decide actions, approvals, or domain promotion.\n"
        f"DOMAIN_INPUT:\n{question}\n"
        "Produce a concise structured planning signal for the critic."
    )


def build_domain_planner_prompt_with_feedback_slot(question: str) -> str:
    return (
        "You are the planner in a later native latent domain-reasoning round.\n"
        f"Solver feedback signal:\n{FEEDBACK_SLOT}\n"
        "Recheck alternatives, provenance, missing facts, and unresolved objections. "
        "Treat feedback as non-binding. Use only supplied IDs.\n"
        f"DOMAIN_INPUT:\n{question}\n"
        "Produce a corrected concise planning signal for the critic."
    )


def build_domain_critic_prompt_with_slot(question: str) -> str:
    return (
        "You are the critic in a native latent domain-reasoning system.\n"
        f"Planner latent signal:\n{PLANNER_SLOT}\n"
        "Contest every interpretation; verify source provenance and rule application; "
        "find contradictions, bias, and logical gaps; state the strongest counterargument; "
        "preserve unresolved issues. Never invent an ID.\n"
        f"DOMAIN_INPUT:\n{question}\n"
        "Produce a concise structured critical signal for the solver."
    )


def build_domain_solver_prompt_with_slots(question: str, args=None, mas_shape: str = "chain") -> str:
    if mas_shape != "chain":
        raise ValueError(f"unsupported_mas_shape:{mas_shape}")
    return (
        "You are the solver in a native latent domain-reasoning system.\n"
        f"Critic latent signal:\n{REFINED_SLOT}\n"
        "Compare interpretations; apply only supplied rules; cite only supplied fact/source IDs; "
        "keep unresolved objections as uncertainties; distinguish fact, rule, inference, opinion, "
        "uncertainty, and recommendation. Do not authorize actions or approvals.\n"
        f"DOMAIN_INPUT:\n{question}\n"
        "Return ONLY one JSON object matching domain_opinion_v1. Required keys: "
        "domain_id, question, position, supporting_arguments, counterarguments, rule_application, "
        "evidence_used, uncertainties, alternative_interpretations, recommendation, confidence, "
        "human_decision_required. Copy domain_id and question exactly. confidence must be 0..1."
    )
