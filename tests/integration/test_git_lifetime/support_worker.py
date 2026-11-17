"""Canonical owned-helper protocol/resources and unchanged observer/paused-Git programs."""

import fcntl
import json
import os
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


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
