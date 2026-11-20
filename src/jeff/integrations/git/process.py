"""One bounded captured Git process; unchanged watch/kill/reap lifetime."""

import os
import select
import selectors
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from jeff.domain.projects import ProjectError

from .. import git as adapter


def _run_git_process(
    executable: str,
    directory: Path,
    *arguments: str,
    deadline: float | None = None,
    measured: list[int] | None = None,
    _watch_fd: int | None = None,
    _on_started: Callable[[], None] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Bound both streams while reading; kill/reap only this owned process on failure."""
    command = [
        executable,
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "status.submoduleSummary=false",
        "-C",
        str(directory),
        *arguments,
    ]
    environment = {
        "PATH": os.defpath,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_ALLOW_PROTOCOL": "",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ATTR_NOSYSTEM": "1",
        "LC_ALL": "C",
    }
    if deadline is not None and deadline <= time.monotonic():
        raise ProjectError("git_timeout", "Git timed out; no command was retried.")
    if _watch_fd is not None:
        adapter._parent_live(_watch_fd)
    try:
        with subprocess.Popen(
            command,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        ) as process:
            if measured is not None:
                measured[1] = 1
            try:
                if _on_started is not None:
                    _on_started()
                assert process.stdout is not None and process.stderr is not None
                buffers = {"stdout": bytearray(), "stderr": bytearray()}
                total = 0
                deadline = min(
                    deadline if deadline is not None else float("inf"),
                    time.monotonic() + adapter.GIT_TIMEOUT,
                )
                with selectors.DefaultSelector() as selector:
                    for label, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                        os.set_blocking(stream.fileno(), False)
                        selector.register(stream, selectors.EVENT_READ, label)
                    if _watch_fd is not None:
                        selector.register(_watch_fd, selectors.EVENT_READ, "parent")
                    open_streams = 2
                    while open_streams:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(command, adapter.GIT_TIMEOUT)
                        for key, _ in selector.select(min(remaining, 0.1)):
                            if key.data == "parent":
                                adapter._parent_live(key.fd)
                            chunk = os.read(
                                key.fd, min(65536, adapter.GIT_OUTPUT_LIMIT - total + 1)
                            )
                            if not chunk:
                                selector.unregister(key.fileobj)
                                open_streams -= 1
                                continue
                            total += len(chunk)
                            if measured is not None:
                                measured[0] = total
                            if total > adapter.GIT_OUTPUT_LIMIT:
                                raise ProjectError(
                                    "git_output_limit",
                                    "Git output exceeded the safety limit. "
                                    "No partial answer is available; reduce the visible changes.",
                                )
                            buffers[str(key.data)].extend(chunk)
                if _watch_fd is not None:
                    while process.poll() is None:
                        adapter._parent_live(_watch_fd)
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(command, adapter.GIT_TIMEOUT)
                        select.select([_watch_fd], [], [], min(remaining, 0.01))
                returncode = process.wait(timeout=max(0.001, deadline - time.monotonic()))
                return subprocess.CompletedProcess(
                    command, returncode, bytes(buffers["stdout"]), bytes(buffers["stderr"])
                )
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
    except subprocess.TimeoutExpired as error:
        raise ProjectError("git_timeout", "Git timed out; no command was retried.") from error
    except OSError as error:
        raise ProjectError(
            "git_failed", "Git could not run; check installation and repository access."
        ) from error
