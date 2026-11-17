"""Bounded process core with captured fake handles and real local pipes, never Popen."""

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO, Self, cast

import pytest

from jeff.domain.projects import ProjectError
from jeff.integrations import git


class CapturedProcess:
    def __init__(self) -> None:
        self.stdout = self._pipe(b"data")
        self.stderr = self._pipe(b"err")
        self.returncode: int | None = None
        self.kills = 0
        self.waits: list[float | None] = []

    @staticmethod
    def _pipe(body: bytes) -> BinaryIO:
        read, write = os.pipe()
        try:
            os.write(write, body)
        finally:
            os.close(write)
        return os.fdopen(read, "rb")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.stdout.close()
        self.stderr.close()

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.kills += 1
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.waits.append(timeout)
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[CapturedProcess]]:
    children: list[CapturedProcess] = []

    def launch(command: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        assert command[:3] == ["/usr/bin/git", "-c", "core.fsmonitor=false"]
        assert kwargs["close_fds"] is True and "pass_fds" not in kwargs
        env = cast(dict[str, str], kwargs["env"])
        assert env["GIT_OPTIONAL_LOCKS"] == "0" and env["GIT_ALLOW_PROTOCOL"] == ""
        child = CapturedProcess()
        children.append(child)
        return cast("subprocess.Popen[bytes]", child)

    monkeypatch.setattr(subprocess, "Popen", launch)
    yield children
    for child in children:
        child.stdout.close()
        child.stderr.close()


def test_captured_process_streams_are_bounded_measured_and_reaped(
    captured: list[CapturedProcess],
) -> None:
    measured = [0, 0]
    result = git._run_git_process("/usr/bin/git", Path("/synthetic"), "status", measured=measured)
    assert (result.stdout, result.stderr, result.returncode) == (b"data", b"err", 0)
    assert measured == [7, 1] and len(captured) == 1
    child = captured[0]
    assert child.kills == 0 and len(child.waits) == 2
    assert child.stdout.closed and child.stderr.closed


def test_output_cap_patch_kills_only_captured_process(
    captured: list[CapturedProcess], monkeypatch: pytest.MonkeyPatch
) -> None:
    measured = [0, 0]
    monkeypatch.setattr(git, "GIT_OUTPUT_LIMIT", 3)
    with pytest.raises(ProjectError) as error:
        git._run_git_process("/usr/bin/git", Path("/synthetic"), "status", measured=measured)
    assert error.value.code == "git_output_limit" and measured == [4, 1]
    assert len(captured) == 1 and captured[0].kills == 1 and captured[0].waits == [None]
    assert captured[0].stdout.closed and captured[0].stderr.closed


def test_timeout_patch_keeps_captured_cleanup_and_no_retry(
    captured: list[CapturedProcess], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(git, "GIT_TIMEOUT", 0.0)
    with pytest.raises(ProjectError) as error:
        git._run_git_process("/usr/bin/git", Path("/synthetic"), "status")
    assert error.value.code == "git_timeout"
    assert len(captured) == 1 and captured[0].kills == 1 and captured[0].waits == [None]


def test_expired_budget_or_dead_parent_cannot_spawn(captured: list[CapturedProcess]) -> None:
    with pytest.raises(ProjectError) as error:
        git._run_git_process("/usr/bin/git", Path("/synthetic"), "status", deadline=0)
    assert error.value.code == "git_timeout" and not captured
    read, write = os.pipe()
    os.close(write)
    try:
        with pytest.raises(git._GitParentGone):
            git._run_git_process("/usr/bin/git", Path("/synthetic"), "status", _watch_fd=read)
        assert not captured
    finally:
        os.close(read)


def test_started_callback_failure_reaps_captured_child(captured: list[CapturedProcess]) -> None:
    def fail() -> None:
        raise ValueError("SYNTHETIC_CALLBACK")

    with pytest.raises(ValueError, match="SYNTHETIC_CALLBACK"):
        git._run_git_process("/usr/bin/git", Path("/synthetic"), "status", _on_started=fail)
    assert len(captured) == 1 and captured[0].kills == 1 and captured[0].waits == [None]
