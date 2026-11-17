"""Immutable project scope and shared frozen dispatch-model policy."""

from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator


class _DispatchModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class ProjectScope(_DispatchModel):
    directory: Path
    expected_root: Path | None = None
    expected_project_id: UUID | None = None

    @field_validator("directory", "expected_root")
    @classmethod
    def absolute(cls, value: Path | None) -> Path | None:
        if value is not None:
            text = str(value)
            if (
                not value.is_absolute()
                or "\0" in text
                or ".." in value.parts
                or text.startswith("//")
                or len(text.encode("utf-8")) > 1023
            ):
                raise ValueError("A valid bounded absolute directory is required.")
        return value
