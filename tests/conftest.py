"""Safe disposable repositories; no mutations to the Jeff repository."""

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    executable = shutil.which("git", path=os.defpath)
    assert executable is not None, "These checks require an installed system Git"
    root = tmp_path / "sample project"
    root.mkdir()
    subprocess.run(
        [executable, "-c", "init.templateDir=", "init", "--initial-branch=main", str(root)],
        cwd=tmp_path,
        env={
            "PATH": os.defpath,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return root.resolve()


@pytest.fixture
def git_run(git_repo: Path) -> Callable[..., subprocess.CompletedProcess[bytes]]:
    """Every mutation is bound to this fixture's disposable repository."""
    executable = shutil.which("git", path=os.defpath)
    assert executable is not None

    def run(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [
                executable,
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=Jeff Tests",
                "-c",
                "user.email=tests@example.invalid",
                "-C",
                str(git_repo),
                *arguments,
            ],
            env={"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
            capture_output=True,
            check=check,
            timeout=10,
        )

    return run
