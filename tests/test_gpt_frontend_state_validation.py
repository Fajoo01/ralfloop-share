from ralfloop_agent.integration.gpt_frontend import SetJobState
from ralfloop_agent.integration.gpt_work_queue import GptJobState


def test_job_state_accepts_json_enum_value() -> None:
    payload = SetJobState.model_validate({"state": "cancelled"})
    assert payload.state is GptJobState.CANCELLED
