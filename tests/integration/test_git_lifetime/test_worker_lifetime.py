"""Actual private helper protocol, inherited lease, native observer and paused-child cleanup."""

import array
import fcntl
import json
import os
import select
import socket
from contextlib import closing
from pathlib import Path

import pytest

from .support_worker import _frame, _invocation, _message, _request_for, _responses, _signal_events


def test_private_protocol_refuses_invalid_requests_without_git(tmp_path: Path) -> None:
    for index, mutation in enumerate(
        ("version", "extra", "operation", "duplicate", "deep", "utf16")
    ):
        root = tmp_path / str(index)
        root.mkdir(mode=0o700)
        with _invocation(root) as call:
            message = _request_for(call, root)
            if mutation in ("version", "extra", "operation"):
                message[
                    {"version": "version", "extra": "unexpected", "operation": "operation"}[
                        mutation
                    ]
                ] = {"version": 2, "extra": True, "operation": "shell"}[mutation]
            payload = json.dumps(message).encode()
            if mutation == "duplicate":
                payload = b'{"version":1,' + payload[1:]
            elif mutation == "deep":
                payload = b"[" * 1100 + b"0" + b"]" * 1100
            elif mutation == "utf16":
                payload = json.dumps(message).encode("utf-16")
            _frame(call, payload)
            assert call.process.wait(timeout=5) == 2
            assert call.channel.recv(1) == b""  # No actual-Git-start frame.
            assert call.process.stdout is not None and call.process.stderr is not None
            assert call.process.stdout.read() == call.process.stderr.read() == b""


def test_liveness_eof_before_work_never_starts_git(tmp_path: Path) -> None:
    with _invocation(tmp_path) as call:
        call.close_watch()
        assert call.process.wait(timeout=5) == 0
        assert call.channel.recv(1) == b""
        assert call.process.stdout is not None and call.process.stderr is not None
        assert call.process.stdout.read() == call.process.stderr.read() == b""


def test_lifetime_lease_blocks_replacement_until_real_exit(git_repo: Path, tmp_path: Path) -> None:
    with _invocation(tmp_path) as call:
        os.close(call.lease)
        call.lease = -1
        probe = os.open(tmp_path / "helper.lock", os.O_RDONLY | os.O_NOFOLLOW)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _frame(call, json.dumps(_request_for(call, git_repo)).encode())
            messages = _responses(call)
            assert messages[0] == {"kind": "started"}
            assert messages[-1]["kind"] == "result" and messages[-1]["returncode"] == 0
            assert call.process.wait(timeout=5) == 0
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(probe, fcntl.LOCK_UN)
        finally:
            os.close(probe)


def test_normal_exit_and_observer_fd_cleanup(git_repo: Path, tmp_path: Path) -> None:
    before = len(os.listdir("/dev/fd"))
    from jeff.infrastructure.runtime_ipc import peer_identity

    with _invocation(tmp_path, observe=True) as call:
        assert call.observer is not None
        payload, ancillary, flags, _ = call.observer.recvmsg(
            3, socket.CMSG_SPACE(array.array("i").itemsize)
        )
        assert payload == b"cap" and flags == 0 and len(ancillary) == 1
        level, kind, data = ancillary[0]
        assert (level, kind) == (socket.SOL_SOCKET, socket.SCM_RIGHTS)
        received = array.array("i")
        received.frombytes(data)
        assert len(received) == 1
        # Authenticate the transferred socket's native peer, then register exit locally.
        # An FD number or serialized PID alone is not equivalent ownership evidence.
        with socket.socket(fileno=received[0]) as capability, closing(select.kqueue()) as observer:
            uid, pid = peer_identity(capability)
            assert (uid, pid) == (os.geteuid(), call.process.pid)
            observer.control(
                [
                    select.kevent(
                        pid,
                        filter=select.KQ_FILTER_PROC,
                        flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_ONESHOT,
                        fflags=select.KQ_NOTE_EXIT,
                    )
                ],
                0,
                0,
            )
            _frame(call, json.dumps(_request_for(call, git_repo)).encode())
            assert _responses(call)[-1]["kind"] == "result"
            events = observer.control([], 1, 5)
            assert len(events) == 1 and events[0].ident == pid
            assert events[0].fflags & select.KQ_NOTE_EXIT
            assert call.process.wait(timeout=5) == 0
    assert len(os.listdir("/dev/fd")) == before


def test_parent_eof_reaps_owned_paused_git(git_repo: Path, tmp_path: Path) -> None:
    with _invocation(tmp_path, hold_git=True) as call:
        _frame(call, json.dumps(_request_for(call, git_repo)).encode())
        assert _message(call) == {"kind": "started"}
        call.close_watch()  # Controlled capability EOF, not a daemon-death claim.
        assert call.process.wait(timeout=5) == 0
        assert call.channel.recv(1) == b""
    events = _signal_events()
    assert [e["event"] for e in events] == ["SIGSTOP", "SIGKILL", "reaped"]
    assert events[-1]["returncode"] == -9
    assert len({e["captured_child_pid"] for e in events}) == 1


def test_original_deadline_reaps_owned_paused_git(git_repo: Path, tmp_path: Path) -> None:
    with _invocation(tmp_path, hold_git=True, seconds=2) as call:
        _frame(call, json.dumps(_request_for(call, git_repo)).encode())
        assert _message(call) == {"kind": "started"}
        assert call.process.wait(timeout=5) == 1
        assert call.channel.recv(1) == b""
    events = _signal_events()
    assert [e["event"] for e in events] == ["SIGSTOP", "SIGKILL", "reaped"]
    assert events[-1]["returncode"] == -9
    assert len({e["captured_child_pid"] for e in events}) == 1
