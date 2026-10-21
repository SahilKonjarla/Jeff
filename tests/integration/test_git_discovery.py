"""Fixed-argv Git discovery, canonical targeting, and safe failures."""

import shutil
import subprocess
from pathlib import Path

import pytest

from jeff.domain.projects import ProjectError
from jeff.integrations.git import GitProjectDiscovery


def test_canonical_root_through_symlink(git_repo: Path, tmp_path: Path) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(git_repo, target_is_directory=True)
    assert GitProjectDiscovery().find_root(alias) == git_repo


def test_missing_git_is_actionable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: None)
    with pytest.raises(ProjectError) as caught:
        GitProjectDiscovery().find_root(tmp_path)
    assert caught.value.code == "git_unavailable"


def test_timeout_is_not_retried_or_leaked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = 0

    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        raise subprocess.TimeoutExpired("FAKE_PRIVATE_VALUE", 5)

    monkeypatch.setattr(GitProjectDiscovery, "_query", staticmethod(timeout))
    with pytest.raises(ProjectError) as caught:
        GitProjectDiscovery().find_root(tmp_path)
    assert caught.value.code == "git_timeout" and calls == 1
    assert "FAKE_PRIVATE_VALUE" not in caught.value.message


def test_tracked_private_config_is_rejected_without_index_mutation(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def query(
        executable: str, directory: Path, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        output = str(git_repo) + "\n" if arguments[0] == "rev-parse" else "jeff.local.yaml\n"
        return subprocess.CompletedProcess(arguments, 0, stdout=output, stderr="")

    monkeypatch.setattr(GitProjectDiscovery, "_query", staticmethod(query))
    with pytest.raises(ProjectError) as caught:
        GitProjectDiscovery().find_root(git_repo)
    assert caught.value.code == "private_config_tracked"
