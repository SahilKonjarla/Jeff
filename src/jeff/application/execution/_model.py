"""Shared frozen model policy and canonical absolute-path validation."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from jeff.domain.execution import public_text


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


def absolute_path(value: Path) -> Path:
    text = str(value)
    if not value.is_absolute() or ".." in value.parts or text.startswith("//"):
        raise ValueError("A canonical absolute path is required.")
    public_text(text, 1023)
    return value
