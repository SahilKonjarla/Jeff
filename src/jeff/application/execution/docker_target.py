"""Cold Docker declarations and observed metadata, not launch permission."""

import re
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, StrictInt, field_validator, model_validator

from jeff.domain.execution import ARGV_COUNT, fingerprint, public_text

from ._model import _Model, absolute_path
from .guard_execution import GuardExecutionPolicy
from .profiles import ExecutionProfile


class DockerTargetDeclaration(_Model):
    """Explicit public image defaults, separate from all persisted manifest schemas.

    These declarations are not automatic environment discovery or secret detection.
    Future creation must override image argv/user/cwd and verify the effective config.
    """

    profile: ExecutionProfile
    image_environment: tuple[tuple[str, str], ...] = Field(default=(), max_length=32)
    image_user: str = Field(default="", max_length=128)
    image_entrypoint: tuple[str, ...] = Field(default=(), max_length=ARGV_COUNT)
    image_command: tuple[str, ...] = Field(default=(), max_length=ARGV_COUNT)
    image_working_dir: str = Field(default="", max_length=1023)

    @model_validator(mode="after")
    def public_defaults(self) -> Self:
        names = [name for name, _ in self.image_environment]
        if names != sorted(names) or len(names) != len(set(names)):
            raise ValueError("Image environment must be sorted and unique.")
        for name, value in self.image_environment:
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name) or re.search(
                r"(?:PASSWORD|PASSWD|TOKEN|SECRET|CREDENTIAL|API_?KEY|ACCESS_KEY|AUTHORIZATION)",
                name,
            ):
                raise ValueError("Sensitive environment channels are unsupported.")
            public_text(value)
        for value in (
            self.image_user,
            self.image_working_dir,
            *self.image_entrypoint,
            *self.image_command,
        ):
            public_text(value)
        fingerprint(self.model_dump(mode="json"))
        return self

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.model_dump(mode="json"))


class DockerCLIIdentity(_Model):
    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: StrictInt = Field(gt=0, le=268435456)

    @field_validator("path")
    @classmethod
    def canonical_path(cls, value: Path) -> Path:
        return absolute_path(value)


class DockerBackendConfiguration(_Model):
    """Explicit installation-private declaration, not verification or authority.

    Construction is cold: paths/identity/defaults must be supplied, never discovered.
    Actual ownership, permissions and binary/engine identity remain adapter checks.
    This standalone contract does not extend legacy settings or enable execution.
    """

    binary: DockerCLIIdentity
    config: Path
    declarations: tuple[DockerTargetDeclaration, ...] = Field(min_length=1, max_length=16)

    @field_validator("config")
    @classmethod
    def config_path(cls, value: Path) -> Path:
        return absolute_path(value)

    @model_validator(mode="after")
    def explicit_and_bounded(self) -> Self:
        if (
            self.config == self.binary.path
            or self.config in self.binary.path.parents
            or self.binary.path in self.config.parents
        ):
            raise ValueError("Private CLI and configuration paths must not overlap.")
        identities = [(d.profile.project_id, d.profile.name) for d in self.declarations]
        if len(identities) != len(set(identities)):
            raise ValueError("Duplicate backend project/profile declaration.")
        fingerprint(self.model_dump(mode="json"))
        return self

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.model_dump(mode="json"))


class GuardedDockerBackendConfiguration(DockerBackendConfiguration):
    """Settings4 explicitly supplies guard identity and timing; no default substitution."""

    guard: GuardExecutionPolicy


class VerifiedDockerTarget(_Model):
    """Observed metadata, not isolation proof, input verification or launch authority."""

    declaration_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    binary: DockerCLIIdentity
    endpoint: str = Field(max_length=1023)
    engine_id: str = Field(min_length=1, max_length=128)
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    platform: Literal["linux/arm64", "linux/amd64"]
    client_version: str = Field(max_length=64)
    server_version: str = Field(max_length=64)
    image_configuration_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    effective_environment: tuple[tuple[str, str], ...] = Field(max_length=35)
    isolation_verified: Literal[False] = False
    inputs_verified: Literal[False] = False
    launch_authority_issued: Literal[False] = False

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.model_dump(mode="json"))
