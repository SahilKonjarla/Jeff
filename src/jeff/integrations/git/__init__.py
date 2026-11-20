"""Narrow read-only repository discovery; no shell or Git mutations."""

import os as os
import re as re
import select as select
import selectors as selectors
import shutil as shutil
import subprocess as subprocess
import time as time
from collections.abc import Callable as Callable
from pathlib import Path as Path

from jeff.application.history import GitCategory as GitCategory
from jeff.application.history import GitObservation as GitObservation
from jeff.application.history import GitObserver as GitObserver
from jeff.domain.projects import ProjectError as ProjectError
from jeff.domain.repository import BranchState as BranchState
from jeff.domain.repository import FileChange as FileChange
from jeff.domain.repository import git_exclusions as git_exclusions

from .discovery import GitProjectDiscovery as GitProjectDiscovery
from .limits import GIT_OUTPUT_LIMIT as GIT_OUTPUT_LIMIT
from .limits import GIT_TIMEOUT as GIT_TIMEOUT
from .limits import MAX_CHANGES as MAX_CHANGES
from .limits import ProcessRunner as ProcessRunner
from .parsing import _malformed as _malformed
from .parsing import _parse_status as _parse_status
from .parsing import _path as _path
from .process import _run_git_process as _run_git_process
from .repository import GitRepositoryRead as GitRepositoryRead


class _GitParentGone(Exception):
    """An inherited liveness capability ended; only the current owner reaps its child."""


def _parent_live(fd: int) -> None:
    """Treat a readable watch pipe as lost parent liveness, never as a PID check."""
    if select.select([fd], [], [], 0)[0]:
        os.read(fd, 1)
        raise _GitParentGone


def _run_git(
    executable: str,
    directory: Path,
    *arguments: str,
    deadline: float | None = None,
    observer: GitObserver | None = None,
    category: GitCategory | None = None,
    process_runner: ProcessRunner | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Record fixed start/output/outcome facts after the runner's cleanup path."""
    began = time.monotonic_ns()
    measured = [0, 0]
    code: str = "git_failed"
    try:
        run = process_runner if process_runner is not None else _run_git_process
        result = run(executable, directory, *arguments, deadline=deadline, measured=measured)
        code = (
            "ok"
            if result.returncode == 0 or (category == "filters.inspect" and result.returncode == 1)
            else "git_failed"
        )
        return result
    except ProjectError as error:
        code = (
            error.code
            if error.code in ("git_timeout", "git_output_limit", "git_unavailable")
            else "git_failed"
        )
        raise
    finally:
        if observer is not None:
            if category is None:
                raise ValueError("Observed Git requires a fixed invocation category.")
            observer.record(
                GitObservation.model_validate(
                    {
                        "category": category,
                        "code": code,
                        "process_started": bool(measured[1]),
                        "elapsed_ms": max(0, (time.monotonic_ns() - began) // 1000000),
                        "output_bytes": measured[0],
                    }
                )
            )


__all__ = [
    "GitProjectDiscovery",
    "GitRepositoryRead",
    "ProcessRunner",
    "GIT_TIMEOUT",
    "GIT_OUTPUT_LIMIT",
    "MAX_CHANGES",
]
