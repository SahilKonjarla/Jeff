"""One runner's captured helper/lease lifetime, original deadline and cleanup quarantine."""

from __future__ import annotations

import base64
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from jeff.domain.projects import ProjectError
from jeff.integrations import git as adapter

from .. import worker as _worker


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
            request = _worker._request(directory, arguments, end)
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
            _worker._send(parent, request.model_dump(), end, _worker._REQUEST_LIMIT)
            while True:
                message = _worker._receive(parent, end, _worker._FRAME_LIMIT)
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
