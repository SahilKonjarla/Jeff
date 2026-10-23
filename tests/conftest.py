"""Safe disposable repositories; no mutations to the Jeff repository."""

import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def owned_runtime_launches(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Capture genuine handles; emergency cleanup is evidence of a failing fixture."""
    original = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def tracked(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child = original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", tracked)
    yield
    forced = False
    for child in children:
        if child.poll() is None:
            forced = True
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
    assert not forced, "Owned runtime fixture required forced child cleanup"


@pytest.fixture
def cli_environment(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> dict[str, str]:
    root = tmp_path_factory.mktemp("cli").resolve()
    for name in ("home", "tmp", "config", "cache", "data"):
        (root / name).mkdir(mode=0o700)
    env = {
        "HOME": str(root / "home"),
        "TMPDIR": str(root / "tmp"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_DATA_HOME": str(root / "data"),
        "JEFF_DATA_DIR": str(root / "d"),
        "JEFF_RUNTIME_DIR": str(root / "r"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return env


@pytest.fixture
def running_cli(
    cli_environment: dict[str, str], owned_runtime_launches: None
) -> Iterator[dict[str, str]]:
    from jeff.application.runtime import ControlSuccess
    from jeff.bootstrap.runtime import create_runtime_controller

    control = create_runtime_controller()
    try:
        started = control.start()
        assert isinstance(started, ControlSuccess) and started.data.application_ready, started
        yield cli_environment
    finally:
        assert isinstance(control.stop(), ControlSuccess)
        (Path(cli_environment["JEFF_DATA_DIR"]) / "ipc.key").unlink(missing_ok=True)


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
