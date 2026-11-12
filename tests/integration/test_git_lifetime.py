"""Approved private helper seam; real inherited resources, never user/default state."""

import array
import fcntl
import json
import os
import select
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
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


@dataclass
class _Invocation:
    process: subprocess.Popen[bytes]
    channel: socket.socket
    watch: int
    lease: int
    deadline_ns: int
    observer: socket.socket | None = None

    def close_watch(self) -> None:
        if self.watch >= 0:
            os.close(self.watch)
            self.watch = -1


@contextmanager
def _invocation(
    root: Path, *, hold_git: bool = False, seconds: int = 4, observe: bool = False
) -> Iterator[_Invocation]:
    """Separate parent liveness from the helper's inherited lifetime lease.

    Closing the writer exercises EOF cleanup; closing the parent's lease copy
    cannot release the helper's lock. Teardown waits without forced termination.
    """
    anchor = root / "helper.lock"
    lease = os.open(anchor, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    read, write = os.pipe()
    parent, child = socket.socketpair()
    deadline_ns = time.monotonic_ns() + seconds * 1_000_000_000
    command = [
        sys.executable,
        "-I",
        "-B",
        "-m",
        "jeff.integrations.git.worker",
        "--watch-fd",
        str(read),
        "--channel-fd",
        str(child.fileno()),
        "--lease-fd",
        str(lease),
        "--deadline-ns",
        str(deadline_ns),
    ]
    if hold_git:
        signal_log = os.environ["JEFF_TEST_D6_SIGNAL_LOG"]
        command = [sys.executable, "-I", "-B", "-c", _HELD_GIT, signal_log, *command[5:]]
    observer_parent = observer_child = None
    descriptors: tuple[int, ...] = (read, child.fileno(), lease)
    if observe:
        assert not hold_git
        observer_parent, observer_child = socket.socketpair()
        observer_parent.settimeout(5)
        descriptors += (observer_child.fileno(),)
        command = [
            sys.executable,
            "-I",
            "-B",
            "-c",
            _OBSERVED_HELPER,
            str(observer_child.fileno()),
            *command[5:],
        ]
    process = subprocess.Popen(
        command,
        cwd=root,
        env={
            "PATH": os.defpath,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "LC_ALL": "C",
        },
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        pass_fds=descriptors,
    )
    if observer_child is not None:
        observer_child.close()
    os.close(read)
    child.close()
    parent.settimeout(5)
    invocation = _Invocation(process, parent, write, lease, deadline_ns, observer_parent)
    try:
        yield invocation
    finally:
        invocation.close_watch()
        parent.close()
        if observer_parent is not None:
            observer_parent.close()
        if invocation.lease >= 0:
            os.close(invocation.lease)
        process.communicate(timeout=5)


def _request_for(call: _Invocation, root: Path) -> dict[str, object]:
    return {
        "version": 1,
        "operation": "discovery.root",
        "directory": str(root),
        "deadline_ns": call.deadline_ns,
        "drivers": [],
    }


def _frame(call: _Invocation, payload: bytes) -> None:
    call.channel.sendall(struct.pack("!I", len(payload)) + payload)


def _message(call: _Invocation) -> dict[str, object]:
    def exact(size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            chunk = call.channel.recv(size - len(result))
            assert chunk
            result.extend(chunk)
        return bytes(result)

    size = struct.unpack("!I", exact(4))[0]
    assert 0 < size <= 96 * 1024
    message = json.loads(exact(size))
    assert isinstance(message, dict)
    return message


def _responses(call: _Invocation) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []
    while True:
        message = _message(call)
        messages.append(message)
        if message["kind"] in ("result", "error"):
            return messages


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


_OBSERVED_HELPER = r"""
import array,socket,sys
import jeff.integrations.git.worker as worker
transport=socket.socket(fileno=int(sys.argv[1]));sys.argv=['git-worker',*sys.argv[2:]]
capability,peer=socket.socketpair()
try:
 transport.sendmsg([b'cap'],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[capability.fileno()]))])
 capability.close();transport.close()
 raise SystemExit(worker.main())
finally:
 capability.close();peer.close();transport.close()
"""


_HELD_GIT = r"""
import json,os,signal,subprocess,sys,time
from pathlib import Path
import jeff.integrations.git.worker as worker
log=Path(sys.argv[1]);sys.argv=['git-worker',*sys.argv[2:]]
original=subprocess.Popen
def record(event,process,**extra):
 with log.open('a') as out:
  out.write(json.dumps({'event':event,'captured_child_pid':process.pid,
                        'owner_pid':os.getpid(),**extra})+'\n')
class HeldGit(original):
 def __init__(self,command,**kwargs):
  assert command[0]=='/usr/bin/git'
  super().__init__(command,**kwargs)
  self.recorded=False
  try:
   assert self.poll() is None,'Git exited before controlled hold; do not retry'
   self.send_signal(signal.SIGSTOP);record('SIGSTOP',self)
   until=time.monotonic()+1
   while True:
    pid,status=os.waitpid(self.pid,os.WNOHANG|os.WUNTRACED)
    if pid:
     if not os.WIFSTOPPED(status):self.returncode=os.waitstatus_to_exitcode(status)
     assert os.WIFSTOPPED(status),'Actual Git hold not established'
     break
    assert time.monotonic()<until,'Actual Git hold not observed'
    time.sleep(.001)
  except BaseException:
   if self.poll() is None:self.kill()
   self.wait()
   raise
 def kill(self):
  assert self.poll() is None
  record('SIGKILL',self)
  return super().kill()
 def wait(self,*args,**kwargs):
  code=super().wait(*args,**kwargs)
  if not self.recorded:record('reaped',self,returncode=code);self.recorded=True
  return code
subprocess.Popen=HeldGit
raise SystemExit(worker.main())
"""


def _signal_events() -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in Path(os.environ["JEFF_TEST_D6_SIGNAL_LOG"]).read_text().splitlines()
    ]


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
