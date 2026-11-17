"""Literal prepared inventories, bindings and pure lexical validation."""

import unicodedata
from pathlib import Path, PurePosixPath
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import Field, StrictInt, field_validator, model_validator

from jeff.domain.execution import (
    PREPARED_FILE_BYTES,
    PREPARED_FILE_LIMIT,
    PREPARED_TOTAL_BYTES,
    fingerprint,
    public_text,
)
from jeff.domain.repository import protected_path

from ._model import _Model, absolute_path


class PreparedFile(_Model):
    path: str = Field(min_length=1, max_length=1023)
    size: StrictInt = Field(ge=0, le=PREPARED_FILE_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def safe_relative_file(cls, value: str) -> str:
        path = PurePosixPath(public_text(value, 1023))
        if (
            path.is_absolute()
            or ".." in path.parts
            or str(path) != value
            or value == "."
            or "\\" in value
            or protected_path(value)
            or any(character in value for character in "*?[]")
        ):
            raise ValueError("Only literal unprotected relative files are supported.")
        return value


class PreparedInputs(_Model):
    """Inventory assertions, not proof that bytes were safely copied or verified."""

    snapshot_id: UUID = Field(default_factory=uuid4)
    policy: Literal["literal_files_v1"] = "literal_files_v1"
    files: tuple[PreparedFile, ...] = Field(default=(), max_length=PREPARED_FILE_LIMIT)

    @model_validator(mode="after")
    def bounded_inventory(self) -> Self:
        names = [item.path for item in self.files]
        if names != sorted(names) or len({n.casefold() for n in names}) != len(names):
            raise ValueError("Inventory must be sorted and have no colliding paths.")
        if self.total_bytes > PREPARED_TOTAL_BYTES:
            raise ValueError("Prepared input byte budget exceeded.")
        return self

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.files)

    @property
    def digest(self) -> str:
        return fingerprint({"policy": self.policy, "files": [f.model_dump() for f in self.files]})


class PreparedInputBinding(_Model):
    """Trusted verifier's live binding, not permission to mount an arbitrary client path."""

    snapshot_id: UUID
    inventory_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    path: Path

    @field_validator("path")
    @classmethod
    def mount_path(cls, value: Path) -> Path:
        absolute_path(value)
        if any(character in str(value) for character in ',"'):
            raise ValueError("Ambiguous mount-path encoding is unsupported.")
        return value


class PreparedInputFault(Exception):
    """Fixed public-safe refusal; never include source paths, bytes or OS diagnostics."""

    def __init__(
        self,
        code: Literal[
            "input_invalid",
            "input_unsafe",
            "input_changed",
            "input_limit",
            "input_capacity",
            "input_timeout",
            "input_busy",
            "input_unsealed",
            "input_scope_mismatch",
            "input_unconfirmed",
        ],
    ) -> None:
        self.code = code
        super().__init__(code)


def canonical_prepared_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    """Pure shared lexical validation, before source access or proposal replay."""
    if len(paths) > PREPARED_FILE_LIMIT:
        raise PreparedInputFault("input_limit")
    placeholders = tuple(PreparedFile(path=p, size=0, sha256="0" * 64) for p in sorted(paths))
    _ = PreparedInputs(files=placeholders).digest
    nodes: dict[tuple[str, ...], tuple[str, ...]] = {}
    leaves: set[tuple[str, ...]] = set()
    for item in placeholders:
        parts = PurePosixPath(item.path).parts
        for index in range(1, len(parts) + 1):
            literal = parts[:index]
            key = tuple(unicodedata.normalize("NFC", p).casefold() for p in literal)
            if key in nodes and nodes[key] != literal:
                raise PreparedInputFault("input_invalid")
            if index < len(parts) and key in leaves:
                raise PreparedInputFault("input_invalid")
            nodes[key] = literal
        key = tuple(unicodedata.normalize("NFC", p).casefold() for p in parts)
        if any(len(old) > len(key) and old[: len(key)] == key for old in nodes):
            raise PreparedInputFault("input_invalid")
        leaves.add(key)
    return tuple(item.path for item in placeholders)
