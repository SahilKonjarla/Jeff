"""Inherited-role validation and one guarded Git invocation with original terminal exits."""

from __future__ import annotations

import argparse
import base64
import os
import socket
import stat
import time
from pathlib import Path

from jeff.domain.projects import ProjectError
from jeff.integrations import git as adapter

from .. import worker as _worker


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


def _main() -> int:
    parser = argparse.ArgumentParser(description="Internal owned Git helper")
    parser.add_argument("--watch-fd", type=int, required=True)
    parser.add_argument("--channel-fd", type=int, required=True)
    parser.add_argument("--lease-fd", type=int, required=True)
    parser.add_argument("--deadline-ns", type=int, required=True)
    args = parser.parse_args()
    try:
        _worker._validate_descriptors(args.watch_fd, args.channel_fd, args.lease_fd)
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
        request = _worker._Request.model_validate(
            _worker._receive(channel, deadline, _worker._REQUEST_LIMIT, args.watch_fd)
        )
        if request.deadline_ns != args.deadline_ns:
            raise ValueError("Deadline mismatch")
        adapter._parent_live(args.watch_fd)
        result = adapter._run_git_process(
            "/usr/bin/git",
            Path(request.directory),
            *_worker._arguments(request.operation, request.drivers),
            deadline=deadline,
            measured=measured,
            _watch_fd=args.watch_fd,
            _on_started=lambda: _worker._send(
                channel, {"kind": "started"}, deadline, _worker._FRAME_LIMIT, args.watch_fd
            ),
        )
        for stream, payload in (("stdout", result.stdout), ("stderr", result.stderr)):
            for offset in range(0, len(payload), 65536):
                _worker._send(
                    channel,
                    {
                        "kind": "chunk",
                        "stream": stream,
                        "data": base64.b64encode(payload[offset : offset + 65536]).decode("ascii"),
                    },
                    deadline,
                    _worker._FRAME_LIMIT,
                    args.watch_fd,
                )
        _worker._send(
            channel,
            {"kind": "result", "returncode": result.returncode, "output_bytes": measured[0]},
            deadline,
            _worker._FRAME_LIMIT,
            args.watch_fd,
        )
        return 0
    except adapter._GitParentGone:
        return 0  # Actual captured child was reconciled by its owning core's finally.
    except ProjectError as error:
        try:
            _worker._send(
                channel,
                {
                    "kind": "error",
                    "code": error.code,
                    "output_bytes": measured[0],
                    "started": bool(measured[1]),
                },
                deadline,
                _worker._FRAME_LIMIT,
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
