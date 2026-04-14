from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ActionDecision(BaseModel):
    tool_name: str
    tool_input: dict[str, Any] = Field(default_factory=dict)
    why: str = ""
