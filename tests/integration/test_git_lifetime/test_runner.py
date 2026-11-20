"""Actual descriptor admission, fixed Git operations/filters and guarded runner cleanup."""

import fcntl
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest


def test_invalid_or_aliased_descriptor_roles_refuse_before_git(tmp_path: Path) -> None:
    read, write = os.pipe()
    try:
        for descriptors in ((-1, -1, -1), (0, 1, 2), (read, read, read)):
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-m",
                    "jeff.integrations.git.worker",
                    "--watch-fd",
                    str(descriptors[0]),
                    "--channel-fd",
                    str(descriptors[1]),
                    "--lease-fd",
                    str(descriptors[2]),
                    "--deadline-ns",
                    str(time.monotonic_ns() + 4_000_000_000),
                ],
                cwd=tmp_path,
                env={
                    "PATH": os.defpath,
                    "HOME": str(tmp_path),
                    "TMPDIR": str(tmp_path),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONNOUSERSITE": "1",
                },
                pass_fds=(read,),
                capture_output=True,
                timeout=5,
            )
            assert result.returncode == 2
            assert result.stdout == result.stderr == b""
        assert list(tmp_path.iterdir()) == []
    finally:
        os.close(read)
        os.close(write)


def test_owned_git_matches_fixed_operations_and_preserves_filters(
    git_repo: Path, tmp_path: Path, git_run: Callable[..., subprocess.CompletedProcess[bytes]]
) -> None:
    from jeff.domain.projects import ProjectError
    from jeff.domain.repository import git_exclusions
    from jeff.integrations.git.worker import OwnedGitRunner

    lease = os.open(tmp_path / "lifetime.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    runner = OwnedGitRunner(lambda: os.dup(lease))
    try:
        measured = [0, 0]
        root = runner(
            "/usr/bin/git",
            git_repo,
            "rev-parse",
            "--show-toplevel",
            deadline=time.monotonic() + 10,
            measured=measured,
        )
        assert root.returncode == 0 and root.stdout.rstrip(b"\n") == os.fsencode(git_repo)
        assert measured == [len(root.stdout) + len(root.stderr), 1]
        tracked = runner(
            "/usr/bin/git",
            git_repo,
            "ls-files",
            "--",
            "jeff.local.yaml",
            deadline=time.monotonic() + 10,
        )
        assert tracked.returncode == 0 and tracked.stdout == b""
        filters = runner(
            "/usr/bin/git",
            git_repo,
            "config",
            "--includes",
            "--null",
            "--name-only",
            "--get-regexp",
            r"^filter\..*\.(clean|process|required)$",
            deadline=time.monotonic() + 10,
        )
        assert filters.returncode == 1 and filters.stdout == b""
        from jeff.integrations.git import GitProjectDiscovery, GitRepositoryRead

        discovery = GitProjectDiscovery(process_runner=runner)
        assert discovery.find_root(git_repo, deadline=time.monotonic() + 10) == git_repo
        repository = GitRepositoryRead(process_runner=runner)
        assert repository.read_branch(git_repo, deadline=time.monotonic() + 10).name == "main"
        for branch_only in (True, False):
            result = runner(
                "/usr/bin/git",
                git_repo,
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
                *((":(glob,exclude)**",) if branch_only else git_exclusions()),
                deadline=time.monotonic() + 10,
            )
            assert result.returncode == 0 and b"# branch.head main\0" in result.stdout
        marker = tmp_path / "filter-must-not-run"
        git_run("config", "filter.blocked.clean", f'/usr/bin/touch "{marker}"')
        assert repository.read_branch(git_repo, deadline=time.monotonic() + 10).name == "main"
        with pytest.raises(ProjectError) as refused:
            repository.read_changes(git_repo, deadline=time.monotonic() + 10)
        assert refused.value.code == "git_filters_unsupported" and not marker.exists()
        assert runner.closed
    finally:
        os.close(lease)


def test_owned_runner_refuses_unsafe_admission_without_fallback(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jeff.domain.projects import ProjectError
    from jeff.integrations.git import GitProjectDiscovery
    from jeff.integrations.git.worker import OwnedGitRunner

    def refused() -> int:
        raise OSError("Synthetic existing-lease duplication refusal")

    before = len(os.listdir("/dev/fd"))
    runner = OwnedGitRunner(refused)
    with pytest.raises(ProjectError) as failure:
        GitProjectDiscovery(process_runner=runner).find_root(
            git_repo, deadline=time.monotonic() + 5
        )
    assert failure.value.code == "git_failed"
    assert runner.closed and len(os.listdir("/dev/fd")) == before

    class ObservedHelper:
        """Unit-only OS boundary outcomes; native lifecycle proof stays in real tests."""

        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, *, timeout: float) -> int:
            raise subprocess.TimeoutExpired("controlled-helper-boundary", timeout)

    for index, code in enumerate((None, 0, 1, 2, -9)):
        lease = os.open(tmp_path / f"unit-lease-{index}", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        observed = ObservedHelper()

        def duplicate(owned: int = lease) -> int:
            return os.dup(owned)

        guarded = OwnedGitRunner(duplicate)
        try:
            with monkeypatch.context() as patch:
                patch.setattr(
                    subprocess, "Popen", lambda *args, observed=observed, **kwargs: observed
                )
                with pytest.raises(ProjectError, match="cleanup is unconfirmed"):
                    guarded(
                        "/usr/bin/git",
                        git_repo,
                        "rev-parse",
                        "--show-toplevel",
                        deadline=time.monotonic() + 5,
                    )
            observed.returncode = code
            if code in (0, 1):
                assert guarded.closed
                assert (
                    GitProjectDiscovery(process_runner=guarded).find_root(
                        git_repo, deadline=time.monotonic() + 5
                    )
                    == git_repo
                )
            else:
                assert not guarded.closed
                with pytest.raises(ProjectError, match="cleanup is unconfirmed"):
                    GitProjectDiscovery(process_runner=guarded).find_root(
                        git_repo, deadline=time.monotonic() + 5
                    )
        finally:
            os.close(lease)
        assert len(os.listdir("/dev/fd")) == before
