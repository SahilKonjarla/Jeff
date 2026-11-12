"""Single-use owned Git helper and private inherited-channel runner; no public service."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import select
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from jeff.domain.projects import ProjectError
from jeff.domain.repository import git_exclusions
from jeff.integrations import git as adapter

_REQUEST_LIMIT = 2 * 1024 * 1024
_FRAME_LIMIT = 96 * 1024
_FILTER = r"^filter\..*\.(clean|process|required)$"


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


def _arguments(operation: str, drivers: list[str]) -> tuple[str, ...]:
    """Expand a finite private operation, not caller-provided command authority."""
    if operation == "discovery.root":
        return ("rev-parse", "--show-toplevel")
    if operation == "discovery.private_tracking":
        return ("ls-files", "--", "jeff.local.yaml")
    if operation == "filters.names":
        return ("config", "--includes", "--null", "--name-only", "--get-regexp", _FILTER)
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
        candidate = _Request(
            version=1,
            operation=operation,
            directory=str(directory),
            deadline_ns=int(deadline * 1e9),
            drivers=drivers if operation == "status.branch" else [],
        )
        if _arguments(operation, candidate.drivers) == arguments:
            return candidate
    raise ValueError("Unsupported Git arguments")


def _validate_descriptors(watch: int, channel: int, lease: int) -> None:
    """Check inherited roles without reopening or reacquiring the existing lock lease."""
    descriptors = (watch, channel, lease)
    if min(descriptors) < 3 or len(set(descriptors)) != 3:
        raise ValueError("Invalid inherited descriptor roles")
    observed = [os.fstat(fd) for fd in descriptors]
    if not stat.S_ISFIFO(observed[0].st_mode) or not stat.S_ISSOCK(observed[1].st_mode):
        raise ValueError("Invalid inherited descriptor types")
    anchor = observed[2]
    if not (
        stat.S_ISREG(anchor.st_mode)
        and anchor.st_uid == os.geteuid()
        and stat.S_IMODE(anchor.st_mode) == 0o600
        and anchor.st_nlink == 1
        and anchor.st_size == 0
    ):
        raise ValueError("Invalid inherited lease")
    # Holding the inherited existing description is not acquiring/reopening an anchor.
    for fd in descriptors:
        os.set_inheritable(fd, False)


class OwnedGitRunner:
    """Daemon-only injected runner; an inherited lease and captured helper, never fallback."""

    def __init__(self, duplicate_lease: Callable[[], int]) -> None:
        self._duplicate_lease = duplicate_lease
        self._mutex = threading.Lock()
        self._helpers: set[subprocess.Popen[bytes]] = set()
        self._cleanup_pending: set[subprocess.Popen[bytes]] = set()

    def _reconcile_locked(self) -> None:
        # Only this runner's captured Popen outcomes can resolve a late cleanup.
        # 0/1 are the helper's normal, child-reconciled success/error exits. An
        # unexpected exit remains quarantined; never infer cleanup from a PID.
        self._cleanup_pending = {p for p in self._cleanup_pending if p.poll() not in (0, 1)}
        self._helpers = {p for p in self._helpers if p.poll() is None}

    @property
    def closed(self) -> bool:
        """Reconcile captured outcomes; abnormal exits keep cleanup unconfirmed."""
        with self._mutex:
            self._reconcile_locked()
            return not self._helpers and not self._cleanup_pending

    def __call__(
        self,
        executable: str,
        directory: Path,
        *arguments: str,
        deadline: float | None = None,
        measured: list[int] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run once inside the caller budget, retaining quarantine for uncertain cleanup."""
        if executable != "/usr/bin/git" or deadline is None:
            raise ProjectError(
                "git_failed", "Owned Git invocation is unavailable; no fallback was run."
            )
        with self._mutex:
            self._reconcile_locked()
            if self._cleanup_pending:
                raise ProjectError(
                    "git_failed", "Owned Git cleanup is unconfirmed; no work was retried."
                )
        end = min(deadline, time.monotonic() + adapter.GIT_TIMEOUT)
        if end <= time.monotonic():
            raise ProjectError("git_timeout", "Git timed out; no command was retried.")
        watch_read = watch_write = lease = -1
        parent: socket.socket | None = None
        child: socket.socket | None = None
        process: subprocess.Popen[bytes] | None = None
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        started = False
        completed = False
        try:
            request = _request(directory, arguments, end)
            watch_read, watch_write = os.pipe()
            parent, child = socket.socketpair()
            lease = self._duplicate_lease()
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-m",
                    "jeff.integrations.git.worker",
                    "--watch-fd",
                    str(watch_read),
                    "--channel-fd",
                    str(child.fileno()),
                    "--lease-fd",
                    str(lease),
                    "--deadline-ns",
                    str(request.deadline_ns),
                ],
                env={
                    "PATH": os.defpath,
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONNOUSERSITE": "1",
                    "LC_ALL": "C",
                },
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=(watch_read, child.fileno(), lease),
            )
            with self._mutex:
                self._helpers.add(process)
            child.close()
            child = None
            os.close(watch_read)
            watch_read = -1
            os.close(lease)
            lease = -1
            parent.setblocking(False)
            _send(parent, request.model_dump(), end, _REQUEST_LIMIT)
            while True:
                message = _receive(parent, end, _FRAME_LIMIT)
                if not isinstance(message, dict):
                    raise ValueError("Invalid private response")
                kind = message.get("kind")
                if kind == "started" and set(message) == {"kind"} and not started:
                    started = True
                    if measured is not None:
                        measured[1] = 1
                elif kind == "chunk" and set(message) == {"kind", "stream", "data"} and started:
                    stream, encoded = message["stream"], message["data"]
                    if (
                        not isinstance(stream, str)
                        or stream not in buffers
                        or not isinstance(encoded, str)
                    ):
                        raise ValueError("Invalid private output")
                    decoded = base64.b64decode(encoded, validate=True)
                    if not 0 < len(decoded) <= 65536:
                        raise ValueError("Invalid private output")
                    buffers[stream].extend(decoded)
                    total = sum(len(v) for v in buffers.values())
                    if total > adapter.GIT_OUTPUT_LIMIT:
                        raise ValueError("Private output overflow")
                    if measured is not None:
                        measured[0] = total
                elif kind in ("result", "error"):
                    reported = message.get("output_bytes")
                    if (
                        type(reported) is not int
                        or not 0 <= reported <= adapter.GIT_OUTPUT_LIMIT + 1
                    ):
                        raise ValueError("Invalid private counters")
                    if kind == "error":
                        if (
                            set(message) != {"kind", "code", "output_bytes", "started"}
                            or type(message["started"]) is not bool
                            or message["started"] != started
                        ):
                            raise ValueError("Invalid private error")
                        code = message["code"]
                        if code not in ("git_failed", "git_timeout", "git_output_limit"):
                            raise ValueError("Invalid private error")
                        if measured is not None:
                            measured[0] = reported
                        raise ProjectError(code, "Owned Git failed; no command was retried.")
                    if (
                        set(message) != {"kind", "returncode", "output_bytes"}
                        or type(message.get("returncode")) is not int
                        or not started
                        or reported != sum(len(v) for v in buffers.values())
                    ):
                        raise ValueError("Invalid private result")
                    completed = True
                    return subprocess.CompletedProcess(
                        [executable, *arguments],
                        message["returncode"],
                        bytes(buffers["stdout"]),
                        bytes(buffers["stderr"]),
                    )
                else:
                    raise ValueError("Invalid private response")
        except ProjectError:
            raise
        except TimeoutError:
            raise ProjectError("git_timeout", "Git timed out; no command was retried.") from None
        except Exception:
            raise ProjectError(
                "git_failed", "Owned Git failed; no fallback or retry was run."
            ) from None
        finally:
            if parent is not None:
                parent.close()
            if child is not None:
                child.close()
            for fd in (watch_read, watch_write, lease):
                if fd >= 0:
                    os.close(fd)
            if process is not None:
                # Cleanup uses only the original caller budget, never a new grace/deadline.
                try:
                    exit_code = process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    with self._mutex:
                        self._cleanup_pending.add(process)
                    # Do not kill the guardian and strand its child. Its lease stays held.
                    raise ProjectError("git_failed", "Owned Git cleanup is unconfirmed.") from None
                else:
                    with self._mutex:
                        self._helpers.discard(process)
                        if exit_code not in (0, 1):
                            self._cleanup_pending.add(process)
                    if exit_code not in (0, 1):
                        raise ProjectError("git_failed", "Owned Git cleanup is unconfirmed.")
                    if completed and exit_code != 0:
                        raise ProjectError("git_failed", "Owned Git helper exit was unconfirmed.")


def _main() -> int:
    parser = argparse.ArgumentParser(description="Internal owned Git helper")
    parser.add_argument("--watch-fd", type=int, required=True)
    parser.add_argument("--channel-fd", type=int, required=True)
    parser.add_argument("--lease-fd", type=int, required=True)
    parser.add_argument("--deadline-ns", type=int, required=True)
    args = parser.parse_args()
    try:
        _validate_descriptors(args.watch_fd, args.channel_fd, args.lease_fd)
        now = time.monotonic_ns()
        if not 0 < args.deadline_ns <= now + 5_000_000_000:
            raise ValueError("Invalid deadline")
        if args.deadline_ns <= now:
            return 1  # Expired before any Git existed: normal reconciled error exit.
    except (OSError, ValueError):
        return 2
    channel = socket.socket(fileno=args.channel_fd)
    channel.setblocking(False)
    os.set_blocking(args.watch_fd, False)
    deadline = args.deadline_ns / 1e9
    measured = [0, 0]
    try:
        adapter._parent_live(args.watch_fd)
        request = _Request.model_validate(
            _receive(channel, deadline, _REQUEST_LIMIT, args.watch_fd)
        )
        if request.deadline_ns != args.deadline_ns:
            raise ValueError("Deadline mismatch")
        adapter._parent_live(args.watch_fd)
        result = adapter._run_git_process(
            "/usr/bin/git",
            Path(request.directory),
            *_arguments(request.operation, request.drivers),
            deadline=deadline,
            measured=measured,
            _watch_fd=args.watch_fd,
            _on_started=lambda: _send(
                channel, {"kind": "started"}, deadline, _FRAME_LIMIT, args.watch_fd
            ),
        )
        for stream, payload in (("stdout", result.stdout), ("stderr", result.stderr)):
            for offset in range(0, len(payload), 65536):
                _send(
                    channel,
                    {
                        "kind": "chunk",
                        "stream": stream,
                        "data": base64.b64encode(payload[offset : offset + 65536]).decode("ascii"),
                    },
                    deadline,
                    _FRAME_LIMIT,
                    args.watch_fd,
                )
        _send(
            channel,
            {"kind": "result", "returncode": result.returncode, "output_bytes": measured[0]},
            deadline,
            _FRAME_LIMIT,
            args.watch_fd,
        )
        return 0
    except adapter._GitParentGone:
        return 0  # Actual captured child was reconciled by its owning core's finally.
    except ProjectError as error:
        try:
            _send(
                channel,
                {
                    "kind": "error",
                    "code": error.code,
                    "output_bytes": measured[0],
                    "started": bool(measured[1]),
                },
                deadline,
                _FRAME_LIMIT,
                args.watch_fd,
            )
        except (OSError, ValueError, TimeoutError, adapter._GitParentGone):
            pass
        return 1
    except Exception:
        # Reserve exit1 for handled Git errors after the owning core's finally.
        # Unknown failures are not late-cleanup authority for the parent.
        return 2
    finally:
        channel.close()
        os.close(args.watch_fd)
        os.close(args.lease_fd)


def main() -> int:
    """Reserve exit2 for unexpected failures, including final descriptor-close errors."""
    try:
        return _main()
    except Exception:
        # Include final descriptor-close failures: unexpected Python exceptions
        # must never masquerade as the normal child-reconciled error exit1.
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
