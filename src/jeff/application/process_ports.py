"""Bounded public observations of native project loopback listeners."""

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from jeff.application.projects import Failure
from jeff.domain.projects import Project


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NativeListener(_Model):
    pid: StrictInt = Field(gt=0, le=2**31 - 1)
    port: StrictInt = Field(gt=0, le=65535)
    address: Literal["127.0.0.1"] = "127.0.0.1"


class ProcessPortsData(_Model):
    listeners: tuple[NativeListener, ...] = Field(max_length=64)
    unconfirmed: StrictInt = Field(default=0, ge=0, le=64)
    scope: Literal["same_user_native_ipv4_loopback_tcp"] = "same_user_native_ipv4_loopback_tcp"


class ProcessPortsSuccess(_Model):
    ok: Literal[True] = True
    data: ProcessPortsData
    error: None = None


class ProcessPortsReader(Protocol):
    def __call__(self, project: Project, deadline: float) -> ProcessPortsSuccess | Failure: ...


class ProcessListData(_Model):
    pids: tuple[StrictInt, ...] = Field(max_length=64)
    unconfirmed: StrictInt = Field(default=0, ge=0, le=512)
    scope: Literal["same_user_native_project_cwd"] = "same_user_native_project_cwd"


class ProcessListSuccess(_Model):
    ok: Literal[True] = True
    data: ProcessListData
    error: None = None


class ProcessListReader(Protocol):
    def __call__(self, project: Project, deadline: float) -> ProcessListSuccess | Failure: ...
