"""Single-use owned Git helper and private inherited-channel runner; no public service."""

from __future__ import annotations

import argparse as argparse
import base64 as base64
import json as json
import os as os
import re as re
import select as select
import socket as socket
import stat as stat
import struct as struct
import subprocess as subprocess
import sys as sys
import threading as threading
import time as time
from collections.abc import Callable as Callable
from pathlib import Path as Path
from typing import Literal as Literal

from pydantic import BaseModel as BaseModel
from pydantic import ConfigDict as ConfigDict
from pydantic import Field as Field
from pydantic import StrictInt as StrictInt
from pydantic import StrictStr as StrictStr
from pydantic import model_validator as model_validator

from jeff.domain.projects import ProjectError as ProjectError
from jeff.domain.repository import git_exclusions as git_exclusions
from jeff.integrations import git as adapter

from .helper import _main as _main
from .helper import _validate_descriptors as _validate_descriptors
from .limits import _FILTER as _FILTER
from .limits import _FRAME_LIMIT as _FRAME_LIMIT
from .limits import _REQUEST_LIMIT as _REQUEST_LIMIT
from .request_policy import _arguments as _arguments
from .request_policy import _Request as _Request
from .request_policy import _request as _request
from .runner import OwnedGitRunner as OwnedGitRunner


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate field")
        result[key] = value
    return result


def _json(payload: bytes) -> object:
    def invalid(value: str) -> None:
        raise ValueError("Non-finite private value")

    return json.loads(payload.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=invalid)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _wait(channel: socket.socket, deadline: float, watch: int | None, *, writing: bool) -> None:
    readers = ([watch] if watch is not None else []) + ([] if writing else [channel])
    readable, writable, _ = select.select(
        readers, [channel] if writing else [], [], _remaining(deadline)
    )
    if watch is not None and watch in readable:
        adapter._parent_live(watch)
    if not (writable if writing else readable):
        raise TimeoutError


def _receive(
    channel: socket.socket, deadline: float, limit: int, watch: int | None = None
) -> object:
    def exact(count: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < count:
            _wait(channel, deadline, watch, writing=False)
            chunk = channel.recv(count - len(chunks))
            if not chunk:
                raise ValueError("Private channel ended")
            chunks.extend(chunk)
        return bytes(chunks)

    count = struct.unpack("!I", exact(4))[0]
    if not 0 < count <= limit:
        raise ValueError("Private frame limit")
    return _json(exact(count))


def _send(
    channel: socket.socket, message: object, deadline: float, limit: int, watch: int | None = None
) -> None:
    payload = json.dumps(message, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode(
        "ascii"
    )
    if not 0 < len(payload) <= limit:
        raise ValueError("Private frame limit")
    pending = memoryview(struct.pack("!I", len(payload)) + payload)
    while pending:
        _wait(channel, deadline, watch, writing=True)
        sent = channel.send(pending)
        if sent <= 0:
            raise ValueError("Private channel ended")
        pending = pending[sent:]


def main() -> int:
    """Reserve exit2 for unexpected failures, including final descriptor-close errors."""
    try:
        return _main()
    except Exception:
        # Include final descriptor-close failures: unexpected Python exceptions
        # must never masquerade as the normal child-reconciled error exit1.
        return 2


__all__ = ["OwnedGitRunner", "main"]
