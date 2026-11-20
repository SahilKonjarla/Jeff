"""Captured fake process outcomes and in-process helper boundaries; never start a child."""

import base64
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from jeff.domain.projects import ProjectError
from jeff.integrations import git
from jeff.integrations.git import worker


class CapturedHelper:
    def __init__(self, code: int = 0, *, timeout: bool = False) -> None:
        self.returncode: int | None = None
        self.code = code
        self.timeout = timeout
        self.waits: list[float] = []

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, *, timeout: float) -> int:
        self.waits.append(timeout)
        if self.timeout:
            raise subprocess.TimeoutExpired("SYNTHETIC_HELPER", timeout)
        self.returncode = self.code
        return self.code


def _fake_transport(
    monkeypatch: pytest.MonkeyPatch, captured: CapturedHelper, messages: list[object]
) -> list[list[str]]:
    launches: list[list[str]] = []
    responses = iter(messages)

    def launch(command: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        launches.append(command)
        assert command[1:5] == ["-I", "-B", "-m", "jeff.integrations.git.worker"]
        inherited = cast(tuple[int, ...], kwargs["pass_fds"])
        assert len(inherited) == len(set(inherited)) == 3
        for fd in inherited:
            os.fstat(fd)
        assert kwargs["close_fds"] is True
        return cast("subprocess.Popen[bytes]", captured)

    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(worker, "_send", lambda *args: None)
    monkeypatch.setattr(worker, "_receive", lambda *args: next(responses))
    return launches


@pytest.mark.parametrize("exit_code", [0, 1, 2])
def test_runner_preserves_command_resources_and_captured_exit_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int
) -> None:
    captured = CapturedHelper(exit_code)
    messages = [
        {"kind": "started"},
        {"kind": "chunk", "stream": "stdout", "data": base64.b64encode(b"data").decode()},
        {"kind": "result", "returncode": 0, "output_bytes": 4},
    ]
    launches = _fake_transport(monkeypatch, captured, messages)
    lease = os.open(tmp_path / "synthetic-lock", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    measured = [0, 0]
    runner = worker.OwnedGitRunner(lambda: os.dup(lease))
    try:
        if exit_code == 0:
            result = runner(
                "/usr/bin/git",
                tmp_path,
                "rev-parse",
                "--show-toplevel",
                deadline=time.monotonic() + 1,
                measured=measured,
            )
            assert result.stdout == b"data" and result.returncode == 0
        else:
            with pytest.raises(ProjectError, match="exit was unconfirmed|cleanup is unconfirmed"):
                runner(
                    "/usr/bin/git",
                    tmp_path,
                    "rev-parse",
                    "--show-toplevel",
                    deadline=time.monotonic() + 1,
                    measured=measured,
                )
        assert len(launches) == 1 and len(captured.waits) == 1
        assert measured == [4, 1]
        assert runner.closed is (exit_code in (0, 1))
        os.fstat(lease)  # Only the duplicate belongs to this invocation.
    finally:
        os.close(lease)


@pytest.mark.parametrize("late_exit", [None, 0, 1, 2, -9])
def test_only_captured_normal_late_exits_resolve_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, late_exit: int | None
) -> None:
    captured = CapturedHelper(timeout=True)
    launches = _fake_transport(
        monkeypatch,
        captured,
        [{"kind": "started"}, {"kind": "result", "returncode": 0, "output_bytes": 0}],
    )
    lease = os.open(tmp_path / "synthetic-lock", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    runner = worker.OwnedGitRunner(lambda: os.dup(lease))
    try:
        with pytest.raises(ProjectError, match="cleanup is unconfirmed"):
            runner(
                "/usr/bin/git",
                tmp_path,
                "rev-parse",
                "--show-toplevel",
                deadline=time.monotonic() + 1,
            )
        assert not runner.closed
        captured.returncode = late_exit
        assert runner.closed is (late_exit in (0, 1))
        if late_exit not in (0, 1):
            with pytest.raises(ProjectError, match="cleanup is unconfirmed"):
                runner(
                    "/usr/bin/git",
                    tmp_path,
                    "rev-parse",
                    "--show-toplevel",
                    deadline=time.monotonic() + 1,
                )
        assert len(launches) == 1
    finally:
        os.close(lease)


@pytest.mark.parametrize(
    "message",
    [
        {"kind": "result", "returncode": 0, "output_bytes": 0},
        {"kind": "started", "extra": True},
        {"kind": "chunk", "stream": "stdout", "data": "??"},
        {"kind": "error", "code": "unexpected", "output_bytes": 0, "started": False},
    ],
)
def test_bad_response_cannot_return_partial_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: object
) -> None:
    captured = CapturedHelper()
    _fake_transport(monkeypatch, captured, [message])
    lease = os.open(tmp_path / "synthetic-lock", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    runner = worker.OwnedGitRunner(lambda: os.dup(lease))
    try:
        with pytest.raises(ProjectError) as failure:
            runner(
                "/usr/bin/git",
                tmp_path,
                "rev-parse",
                "--show-toplevel",
                deadline=time.monotonic() + 1,
            )
        assert failure.value.code == "git_failed" and runner.closed
    finally:
        os.close(lease)


@pytest.mark.parametrize("outcome", ["ok", "git_error", "parent_gone", "unexpected", "close_error"])
def test_helper_core_keeps_root_hooks_and_terminal_close_policy(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    watch, channel_fd, lease = 100001, 100002, 100003
    deadline_ns = time.monotonic_ns() + 1_000_000_000
    sent: list[object] = []
    closed: list[int] = []
    roles: list[tuple[int, int, int]] = []

    class Channel:
        closed = False

        def setblocking(self, flag: bool) -> None:
            assert flag is False

        def close(self) -> None:
            self.closed = True

    channel = Channel()

    def validate(watch: int, channel: int, lease: int) -> None:
        roles.append((watch, channel, lease))

    def run(
        *args: object, measured: list[int], _on_started: Callable[[], None], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        if outcome == "parent_gone":
            raise git._GitParentGone
        if outcome == "unexpected":
            raise RuntimeError("SYNTHETIC_BOUNDARY")
        measured[:] = [4, 1]
        _on_started()
        if outcome == "git_error":
            raise ProjectError("git_timeout", "SYNTHETIC_TIMEOUT")
        return subprocess.CompletedProcess([], 0, b"data", b"")

    original_close = os.close
    original_blocking = os.set_blocking

    def close(fd: int) -> None:
        if fd in (watch, lease):
            closed.append(fd)
            if outcome == "close_error" and fd == lease:
                raise OSError("SYNTHETIC_CLOSE")
        else:
            original_close(fd)

    def blocking(fd: int, flag: bool) -> None:
        if fd != watch:
            original_blocking(fd, flag)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "worker",
            "--watch-fd",
            str(watch),
            "--channel-fd",
            str(channel_fd),
            "--lease-fd",
            str(lease),
            "--deadline-ns",
            str(deadline_ns),
        ],
    )
    monkeypatch.setattr(worker, "_validate_descriptors", validate)
    monkeypatch.setattr(socket, "socket", lambda **kwargs: cast(socket.socket, channel))
    monkeypatch.setattr(os, "close", close)
    monkeypatch.setattr(os, "set_blocking", blocking)
    monkeypatch.setattr(
        worker,
        "_receive",
        lambda *args: {
            "version": 1,
            "operation": "discovery.root",
            "directory": "/synthetic/repo",
            "deadline_ns": deadline_ns,
        },
    )
    monkeypatch.setattr(worker, "_send", lambda channel, message, *args: sent.append(message))
    monkeypatch.setattr(git, "_parent_live", lambda fd: None)
    monkeypatch.setattr(git, "_run_git_process", run)
    assert (
        worker.main()
        == {"ok": 0, "git_error": 1, "parent_gone": 0, "unexpected": 2, "close_error": 2}[outcome]
    )
    assert roles == [(watch, channel_fd, lease)]
    assert channel.closed and closed == [watch, lease]
    if outcome in ("ok", "close_error"):
        assert sent == [
            {"kind": "started"},
            {"kind": "chunk", "stream": "stdout", "data": "ZGF0YQ=="},
            {"kind": "result", "returncode": 0, "output_bytes": 4},
        ]
    elif outcome == "git_error":
        assert sent[-1] == {
            "kind": "error",
            "code": "git_timeout",
            "output_bytes": 4,
            "started": True,
        }
    else:
        assert sent == []
