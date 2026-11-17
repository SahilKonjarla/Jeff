"""Strict revision-bound authentication/control/startup DTOs; no IPC implementation."""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, SecretStr, StrictBool, StrictInt, field_validator, model_validator

from jeff.application.storage import StorageIssueCode

from .. import runtime as _runtime
from .base import _RuntimeModel
from .failures import ControlIssueCode
from .limits import PROTOCOL_REVISION
from .observations import ControlObservation, RuntimeIdentityData


class AuthRequest(_RuntimeModel):
    revision: StrictInt = Field(ge=PROTOCOL_REVISION, le=PROTOCOL_REVISION)
    token: SecretStr

    @field_validator("token", mode="before")
    @classmethod
    def credential_format(cls, value: object) -> SecretStr:
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
        ):
            raise ValueError("Invalid authentication format.")
        return _runtime.SecretStr(value)


class AuthAccepted(_RuntimeModel):
    revision: StrictInt = Field(ge=PROTOCOL_REVISION, le=PROTOCOL_REVISION)
    authenticated: StrictBool
    identity: RuntimeIdentityData | None = None
    error: Literal["runtime_authentication_failed", "runtime_protocol_incompatible"] | None = None

    @model_validator(mode="after")
    def accepted(self) -> Self:
        if self.authenticated != (self.identity is not None):
            raise ValueError("Only authenticated peers receive instance identity.")
        if self.authenticated != (self.error is None):
            raise ValueError("Authentication requires exactly one bounded outcome.")
        return self


ControlOperation = Literal["hello", "status", "stop"]


class ControlRequest(_RuntimeModel):
    kind: Literal["control"] = "control"
    revision: StrictInt = Field(ge=PROTOCOL_REVISION, le=PROTOCOL_REVISION)
    request_id: UUID
    operation: ControlOperation
    expected_instance_id: UUID | None = None

    @model_validator(mode="after")
    def targeted_stop(self) -> Self:
        if self.operation == "stop" and self.expected_instance_id is None:
            raise ValueError("Shutdown must target an authenticated instance.")
        return self


class ControlResponse(_RuntimeModel):
    revision: StrictInt = Field(ge=PROTOCOL_REVISION, le=PROTOCOL_REVISION)
    request_id: UUID
    observation: ControlObservation | None = None
    error: ControlIssueCode | None = None

    @model_validator(mode="after")
    def one_outcome(self) -> Self:
        if (self.observation is None) == (self.error is None):
            raise ValueError("Exactly one bounded control outcome is required.")
        return self


class StartupResult(_RuntimeModel):
    """Transient child diagnostic only; never authority for control readiness."""

    revision: StrictInt = Field(ge=PROTOCOL_REVISION, le=PROTOCOL_REVISION)
    instance_id: UUID
    error: StorageIssueCode | None = None
