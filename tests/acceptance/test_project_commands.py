"""Installed project commands against disposable repositories only."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from jeff.application.projects import Failure, Success


def invoke(directory: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "jeff", *arguments],
        cwd=directory,
        env={
            "PATH": os.defpath,
            "NO_COLOR": "1",
            "TERM": "dumb",
            # Inherited Git targeting/config must not redirect Jeff's adapter.
            "GIT_DIR": "/nonexistent/FAKE_PRIVATE_VALUE",
            "GIT_WORK_TREE": "/nonexistent/FAKE_PRIVATE_VALUE",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.bare",
            "GIT_CONFIG_VALUE_0": "true",
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_installed_initialize_inspect_repeat_and_actual_git_ignores(git_repo: Path) -> None:
    nested = git_repo / "nested"
    nested.mkdir()
    result = invoke(nested, "init", "--json")
    assert result.returncode == 0, result.stderr
    first = Success.model_validate_json(result.stdout)
    assert first.data.created and first.data.root == str(git_repo)
    config = git_repo / "jeff.local.yaml"
    original = config.read_bytes()
    for command in ("config", "status", "init"):
        checked = invoke(nested, command, "--json")
        assert checked.returncode == 0, checked.stderr
        current = Success.model_validate_json(checked.stdout)
        assert current.data.project_id == first.data.project_id
        assert not current.data.created
    assert config.read_bytes() == original
    executable = shutil.which("git", path=os.defpath)
    assert executable is not None
    ignored = subprocess.run(
        [executable, "-C", str(git_repo), "check-ignore", "--", "jeff.local.yaml", ".jeff/file"],
        env={"PATH": os.defpath, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert ignored.stdout.splitlines() == ["jeff.local.yaml", ".jeff/file"]
    assert not (git_repo / ".jeff").exists()


def test_outside_repository_returns_safe_json_error(tmp_path: Path) -> None:
    result = invoke(tmp_path, "init", "--json")
    assert result.returncode == 1
    failure = Failure.model_validate_json(result.stdout)
    assert failure.error.code == "not_a_repository"
    assert "FAKE_PRIVATE_VALUE" not in result.stdout + result.stderr
    assert not list(tmp_path.iterdir())


def test_unregistered_status_and_human_readable_error(git_repo: Path) -> None:
    result = invoke(git_repo, "status", "--json")
    assert result.returncode == 1
    assert Failure.model_validate_json(result.stdout).error.code == "project_not_initialized"
    human = invoke(git_repo, "config")
    assert human.returncode == 1 and "Run jeff init" in human.stderr
    assert not human.stdout
    assert not (git_repo / "jeff.local.yaml").exists()


def test_malformed_settings_do_not_leak_in_installed_errors(git_repo: Path) -> None:
    config = git_repo / "jeff.local.yaml"
    config.write_text("token: FAKE_PRIVATE_VALUE\nproject: [broken")
    result = invoke(git_repo, "config", "--json")
    assert result.returncode == 1
    assert Failure.model_validate_json(result.stdout).error.code == "config_invalid"
    assert "FAKE_PRIVATE_VALUE" not in result.stdout + result.stderr
