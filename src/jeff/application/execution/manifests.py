"""Distinct review-only and fresh manifests; exact canonical action identity."""

from pathlib import Path, PurePosixPath
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from jeff.domain.execution import ARGV_BYTES, ARGV_COUNT, fingerprint, public_text
from jeff.domain.projects import Project

from ._model import _Model
from .prepared_inputs import PreparedInputs
from .profiles import ExecutionProfile


class ActionManifest(_Model):
    profile: ExecutionProfile
    argv: tuple[str, ...] = Field(min_length=1, max_length=ARGV_COUNT)
    authority_kind: Literal["review_only_v1"] = "review_only_v1"
    target_verification: Literal["declared_only"] = "declared_only"
    execution_available: Literal[False] = False

    @field_validator("argv")
    @classmethod
    def public_arguments(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            public_text(value)
        if sum(len(value.encode("utf-8")) for value in values) > ARGV_BYTES:
            raise ValueError("Argument budget exceeded.")
        return values

    @model_validator(mode="after")
    def exact(self) -> Self:
        if self.argv[0] != self.profile.executable:
            raise ValueError("Manifest executable differs from its declaration.")
        fingerprint(self.model_dump(mode="json"))
        return self

    @property
    def canonical_root(self) -> Path:
        return self.profile.canonical_root

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.model_dump(mode="json"))


def build_manifest(
    project: Project, profile: ExecutionProfile, argv: tuple[str, ...]
) -> ActionManifest:
    if profile.project_id != project.project_id or profile.canonical_root != project.root:
        raise ValueError("Execution profile does not bind this project.")
    if not argv or argv[0] not in (profile.executable, PurePosixPath(profile.executable).name):
        raise ValueError("Executable is not declared by this profile.")
    return ActionManifest(profile=profile, argv=(profile.executable, *argv[1:]))


class ExecutionManifest(_Model):
    """Fresh action identity; the trusted verifier must still check target and inputs.

    Not a subclass or reinterpretation of the permanently review-only ActionManifest.
    Public intent is separate; only the trusted target/input verifier grants fresh authority.
    """

    authority_kind: Literal["execution_v1"] = "execution_v1"
    verification: Literal["required_before_authority"] = "required_before_authority"
    profile: ExecutionProfile
    argv: tuple[str, ...] = Field(min_length=1, max_length=ARGV_COUNT)
    inputs: PreparedInputs

    @model_validator(mode="after")
    def coherent_action(self) -> Self:
        ActionManifest(profile=self.profile, argv=self.argv)
        fingerprint(self.model_dump(mode="json"))
        return self

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.model_dump(mode="json"))
