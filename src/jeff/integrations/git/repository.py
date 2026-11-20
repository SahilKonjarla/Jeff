"""Protected metadata and filter-safe fixed-argv branch/change inspection."""

import os
import re
import shutil
from pathlib import Path

from jeff.application.history import GitObserver
from jeff.domain.projects import ProjectError
from jeff.domain.repository import BranchState, FileChange, git_exclusions

from .. import git as adapter
from .limits import ProcessRunner


class GitRepositoryRead:
    """Read protected metadata; filter-backed changes fail rather than execute drivers."""

    def __init__(self, *, process_runner: ProcessRunner | None = None) -> None:
        self._process_runner = process_runner

    def read_branch(
        self, root: Path, *, deadline: float | None = None, observer: GitObserver | None = None
    ) -> BranchState:
        branch, _ = adapter._parse_status(
            self._status(root, branch_only=True, deadline=deadline, observer=observer)
        )
        return branch

    def read_changes(
        self, root: Path, *, deadline: float | None = None, observer: GitObserver | None = None
    ) -> tuple[FileChange, ...]:
        _, changes = adapter._parse_status(
            self._status(root, branch_only=False, deadline=deadline, observer=observer)
        )
        return changes

    def _status(
        self,
        root: Path,
        *,
        branch_only: bool,
        deadline: float | None = None,
        observer: GitObserver | None = None,
    ) -> bytes:
        executable = shutil.which("git", path=os.defpath)
        if executable is None:
            raise ProjectError(
                "git_unavailable", "Install system Git to inspect repository metadata."
            )
        # Status can invoke clean/process drivers while hashing changed files.
        # Discover only effective key names, never command values, then disable drivers.
        filters = adapter._run_git(
            executable,
            root,
            "config",
            "--includes",
            "--null",
            "--name-only",
            "--get-regexp",
            r"^filter\..*\.(clean|process|required)$",
            deadline=deadline,
            observer=observer,
            category="filters.inspect",
            process_runner=self._process_runner,
        )
        if filters.returncode not in {0, 1}:
            raise ProjectError(
                "git_failed", "Git filter configuration could not be checked safely."
            )
        drivers: set[str] = set()
        for key in filters.stdout.split(b"\0"):
            if not key:
                continue
            match = re.fullmatch(rb"filter\.([A-Za-z0-9_.-]+)\.(?:clean|process|required)", key)
            if match is None:
                raise ProjectError(
                    "git_unsafe_config", "Unsupported Git filter name; no status was run."
                )
            drivers.add(match[1].decode("ascii"))
        if len(drivers) > 1000:
            raise ProjectError("git_output_limit", "Too many Git filters to inspect safely.")
        if drivers and not branch_only:
            raise ProjectError(
                "git_filters_unsupported",
                "Repository clean/process filters make status unsafe or misleading to compute "
                "without executing user commands. No change answer was produced.",
            )
        overrides = [
            item
            for driver in sorted(drivers)
            for setting in (
                f"filter.{driver}.clean=",
                f"filter.{driver}.process=",
                f"filter.{driver}.required=false",
            )
            for item in ("-c", setting)
        ]
        exclusions = (":(glob,exclude)**",) if branch_only else git_exclusions()
        result = adapter._run_git(
            executable,
            root,
            *overrides,
            "status",
            "--porcelain=v2",
            "--branch",
            "-z",
            "--untracked-files=no" if branch_only else "--untracked-files=all",
            "--ignore-submodules=all",
            "--no-ahead-behind",
            "--no-renames" if branch_only else "--renames",
            "--",
            ".",
            *exclusions,
            deadline=deadline,
            observer=observer,
            category="status.branch" if branch_only else "status.changes",
            process_runner=self._process_runner,
        )
        if result.returncode != 0:
            raise ProjectError(
                "git_failed", "Git could not inspect this repository; no answer was inferred."
            )
        return result.stdout
