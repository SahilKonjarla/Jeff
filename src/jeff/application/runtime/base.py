"""Frozen, strict runtime DTO foundation; no lifecycle or transport implementation."""

from pydantic import BaseModel, ConfigDict


class _RuntimeModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
