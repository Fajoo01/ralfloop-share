from __future__ import annotations


def next_role(current: str) -> str:
    if current == "planner":
        return "coder"
    if current == "coder":
        return "judge"
    return "planner"


def profile_for_role(state) -> str:
    if state.current_role == "planner":
        return state.planner_model_profile
    if state.current_role == "coder":
        return state.coder_model_profile
    return state.judge_model_profile


def model_name_for_role(state) -> str:
    if state.current_role == "planner":
        return state.planner_model_name
    if state.current_role == "coder":
        return state.coder_model_name
    return state.judge_model_name


def rag_for_role(state) -> str:
    if state.current_role == "planner":
        return state.planner_rag_collection
    if state.current_role == "coder":
        return state.coder_rag_collection
    return state.judge_rag_collection


def planner_for_role(agent, state):
    if state.current_role == "planner":
        return agent.planner
    if state.current_role == "coder":
        return agent.coder_planner
    return agent.judge_planner
