"""Strict frozen Docker metadata models and bounded unambiguous JSON parsing."""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from jeff.application.execution import DockerBackendFault

from .queries import _LIMIT


class _Metadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class _Engine(_Metadata):
    ID: str = Field(min_length=1, max_length=128)
    OSType: str
    Architecture: str
    OperatingSystem: str
    ServerVersion: str = Field(max_length=64)
    SecurityOptions: tuple[str, ...] = Field(max_length=16)
    MemoryLimit: StrictBool
    PidsLimit: StrictBool
    CPUCfsPeriod: StrictBool
    CPUCfsQuota: StrictBool


class _ImageConfig(_Metadata):
    Env: tuple[str, ...] | None = Field(default=None, max_length=32)
    User: str = Field(default="", max_length=128)
    Entrypoint: tuple[str, ...] | None = Field(default=None, max_length=64)
    Cmd: tuple[str, ...] | None = Field(default=None, max_length=64)
    WorkingDir: str = Field(default="", max_length=1023)
    Volumes: dict[str, object] | None = None
    Healthcheck: dict[str, object] | None = None
    OnBuild: tuple[str, ...] | None = None
    # Raw OCI Config may omit defaults or include known inert metadata. Reject active extras.
    Labels: dict[str, str] | None = Field(default=None, max_length=0)
    ExposedPorts: dict[str, object] | None = Field(default=None, max_length=0)
    ArgsEscaped: StrictBool = False
    StopSignal: Literal[""] | None = None
    Shell: tuple[str, ...] | None = Field(default=None, max_length=0)


class _Image(_Metadata):
    ID: str
    OS: str
    Architecture: str
    Variant: str | None = None
    Config: _ImageConfig


def _json(data: bytes) -> object:
    if not 0 < len(data) <= _LIMIT:
        raise DockerBackendFault("docker_output_limit")

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate metadata key.")
            result[key] = value
        return result

    def finite(value: str) -> object:
        raise ValueError("Nonfinite metadata.")

    return json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=finite)
