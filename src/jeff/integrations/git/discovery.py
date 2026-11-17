"""Canonical repository discovery and private-tracking refusal via the runner seam."""

import os
import shutil
import subprocess
from pathlib import Path

from jeff.domain.projects import ProjectError

from .. import git as adapter
from .limits import ProcessRunner


class GitProjectDiscovery:
    """Resolve canonical roots through the injected runner without registering a project."""

    def __init__(self, *, process_runner: ProcessRunner | None = None) -> None:
        self._process_runner = process_runner

    def find_root(self, directory: Path, *, deadline: float | None = None) -> Path:
        executable = shutil.which("git", path=os.defpath)
        if executable is None:
            raise ProjectError("git_unavailable", "Install Git before initializing a project.")
        try:
            directory = directory.resolve(strict=True)
            if not directory.is_dir():
                raise ProjectError(
                    "invalid_root", "Run Jeff from an existing repository directory."
                )
            root_text = self._query(
                executable,
                directory,
                "rev-parse",
                "--show-toplevel",
                **({"deadline": deadline} if deadline is not None else {}),
            )
            if root_text.returncode != 0:
                raise ProjectError("not_a_repository", "Run Jeff inside a non-bare Git repository.")
            reported_root = Path(root_text.stdout.removesuffix("\n"))
            if not reported_root.is_absolute():
                raise ProjectError("invalid_root", "Git returned an invalid repository root.")
            root = reported_root.resolve(strict=True)
            if not root.is_dir() or not directory.is_relative_to(root):
                raise ProjectError("invalid_root", "Git returned an invalid repository root.")
            tracked = self._query(
                executable,
                root,
                "ls-files",
                "--",
                "jeff.local.yaml",
                **({"deadline": deadline} if deadline is not None else {}),
            )
            if tracked.returncode != 0:
                raise ProjectError(
                    "git_failed", "Git could not inspect private configuration tracking."
                )
            if tracked.stdout:
                raise ProjectError(
                    "private_config_tracked",
                    "jeff.local.yaml is tracked by Git. Review its tracking manually; "
                    "Jeff will not change the index.",
                )
            return root
        except (OSError, ValueError, RuntimeError, UnicodeError) as error:
            raise ProjectError(
                "discovery_failed", "Repository discovery failed; check Git and directory access."
            ) from error
        except subprocess.TimeoutExpired as error:
            raise ProjectError(
                "git_timeout", "Git discovery timed out; no command was retried."
            ) from error

    def _query(
        self, executable: str, directory: Path, *arguments: str, deadline: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        result = adapter._run_git(
            executable,
            directory,
            *arguments,
            deadline=deadline,
            process_runner=self._process_runner,
        )
        return subprocess.CompletedProcess(
            result.args,
            result.returncode,
            result.stdout.decode("utf-8"),
            result.stderr.decode("utf-8", errors="replace"),
        )
