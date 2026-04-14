from __future__ import annotations


def advance_role_and_iteration(state, next_role_fn) -> None:
    state.current_role = next_role_fn(state.current_role)
    state.iteration += 1


def finalize_state(state, build_final_answer_fn) -> None:
    if state.stop_reason == "goal_completed":
        state.status = "completed"
        state.final_answer = build_final_answer_fn(state)
    else:
        state.status = "failed"
        state.stop_reason = state.stop_reason or "repeated_failure"
        state.final_answer = "Task non completato."
