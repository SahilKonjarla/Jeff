"""One strict request schema, finite operation expansion and exact argv round trip."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from jeff.domain.repository import git_exclusions
from jeff.integrations import git as adapter

from .. import worker as _worker


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    version: StrictInt = Field(ge=1, le=1)
    operation: Literal[
        "discovery.root",
        "discovery.private_tracking",
        "filters.names",
        "status.branch",
        "status.changes",
    ]
    directory: StrictStr
    deadline_ns: StrictInt = Field(gt=0, le=(1 << 63) - 1)
    drivers: list[StrictStr] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def safe(self) -> _Request:
        path = Path(self.directory)
        if (
            not path.is_absolute()
            or self.directory.startswith("//")
            or "\0" in self.directory
            or ".." in path.parts
            or len(os.fsencode(path)) > 1023
            or sum(len(d) for d in self.drivers) > adapter.GIT_OUTPUT_LIMIT
            or any(re.fullmatch(r"[A-Za-z0-9_.-]+", d) is None for d in self.drivers)
            or self.drivers != sorted(set(self.drivers))
            or (self.drivers and self.operation != "status.branch")
        ):
            raise ValueError("Unsupported private request")
        return self


def _arguments(operation: str, drivers: list[str]) -> tuple[str, ...]:
    """Expand a finite private operation, not caller-provided command authority."""
    if operation == "discovery.root":
        return ("rev-parse", "--show-toplevel")
    if operation == "discovery.private_tracking":
        return ("ls-files", "--", "jeff.local.yaml")
    if operation == "filters.names":
        return ("config", "--includes", "--null", "--name-only", "--get-regexp", _worker._FILTER)
    if operation not in ("status.branch", "status.changes"):
        raise ValueError("Unsupported operation")
    branch = operation == "status.branch"
    overrides = tuple(
        value
        for driver in drivers
        for setting in (
            f"filter.{driver}.clean=",
            f"filter.{driver}.process=",
            f"filter.{driver}.required=false",
        )
        for value in ("-c", setting)
    )
    return (
        *overrides,
        "status",
        "--porcelain=v2",
        "--branch",
        "-z",
        "--untracked-files=no" if branch else "--untracked-files=all",
        "--ignore-submodules=all",
        "--no-ahead-behind",
        "--no-renames" if branch else "--renames",
        "--",
        ".",
        *((":(glob,exclude)**",) if branch else git_exclusions()),
    )


def _request(directory: Path, arguments: tuple[str, ...], deadline: float) -> _Request:
    """Require an exact argv round-trip through the private operation allowlist."""
    drivers: list[str] = []
    position = 0
    while position < len(arguments) and arguments[position] == "-c":
        if position + 5 >= len(arguments):
            raise ValueError("Invalid override")
        match = re.fullmatch(r"filter\.([A-Za-z0-9_.-]+)\.clean=", arguments[position + 1])
        if match is None:
            raise ValueError("Invalid override")
        driver = match[1]
        expected = (
            "-c",
            f"filter.{driver}.clean=",
            "-c",
            f"filter.{driver}.process=",
            "-c",
            f"filter.{driver}.required=false",
        )
        if arguments[position : position + 6] != expected:
            raise ValueError("Invalid override")
        drivers.append(driver)
        position += 6
    for operation in (
        "discovery.root",
        "discovery.private_tracking",
        "filters.names",
        "status.branch",
        "status.changes",
    ):
        candidate = _worker._Request(
            version=1,
            operation=operation,
            directory=str(directory),
            deadline_ns=int(deadline * 1e9),
            drivers=drivers if operation == "status.branch" else [],
        )
        if _worker._arguments(operation, candidate.drivers) == arguments:
            return candidate
    raise ValueError("Unsupported Git arguments")
