"""Versioned sealed inventory metadata and strict duplicate-key decoding."""

from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from jeff.application.execution import PreparedInputs

_SEAL_BYTES = 65536


_Publication = tuple[UUID, tuple[int, int, int, int], tuple[int, int, int, int]]


class _Seal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    version: StrictInt = Field(default=1, ge=1, le=1)
    project_id: UUID
    canonical_root: Path
    inventory: PreparedInputs
    inventory_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_identity: tuple[StrictInt, StrictInt, StrictInt, StrictInt]


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate prepared metadata key.")
        result[key] = value
    return result
