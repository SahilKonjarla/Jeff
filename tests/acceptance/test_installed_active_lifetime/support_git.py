"""Unchanged helper/Git child programs and native capability/owner-death window."""

from __future__ import annotations

import array
import hashlib
import json
import os
import select
import socket
import struct
import subprocess
import threading
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from jeff.application.dispatch import DispatchReply, EvidenceQuery, ProjectCall
from jeff.application.runtime import ControlSuccess, RuntimeLayout
from jeff.infrastructure.runtime_ipc import RuntimeProjectClient, peer_identity
from jeff.infrastructure.runtime_process import RuntimeController
from jeff.infrastructure.runtime_state import RuntimeStateStore

_HELPER_BOOT = r"""
import array,json,os,socket,struct,subprocess,sys,time
import jeff.integrations.git.worker as worker
transport=socket.socket(fileno=int(sys.argv[1]));log=sys.argv[2];hold=sys.argv[3]=='hold'
sys.argv=['git-worker',*sys.argv[4:]]
cap,peer=socket.socketpair();peer.settimeout(2)
def send(value):
 body=json.dumps(value).encode();peer.sendall(struct.pack('!I',len(body))+body)
def record(event,process,**extra):
 fd=os.open(log,os.O_WRONLY|os.O_APPEND|os.O_NOFOLLOW)
 try:os.write(fd,(json.dumps({'event':event,'captured_child_pid':process.pid,
                              'owner_pid':os.getpid(),**extra})+'\n').encode())
 finally:os.close(fd)
original=subprocess.Popen
class HeldGit(original):
 def __init__(self,command,**kwargs):
  assert command[0]=='/usr/bin/git' and 'status' in command
  super().__init__(command,**kwargs);self.recorded=False
  try:
   assert self.poll() is None,'Git exited before controlled hold; no retry'
   self.send_signal(__import__('signal').SIGSTOP);record('SIGSTOP',self)
   until=time.monotonic()+1
   while True:
    pid,status=os.waitpid(self.pid,os.WNOHANG|os.WUNTRACED)
    if pid:
     if not os.WIFSTOPPED(status):self.returncode=os.waitstatus_to_exitcode(status)
     assert os.WIFSTOPPED(status)
     break
    assert time.monotonic()<until
    time.sleep(.001)
   send({'event':'held'})
  except BaseException:
   if self.poll() is None:self.kill()
   self.wait()
   raise
 def kill(self):
  assert self.poll() is None;record('SIGKILL',self)
  return super().kill()
 def wait(self,*args,**kwargs):
  code=super().wait(*args,**kwargs)
  if not self.recorded:
   self.recorded=True;record('reaped',self,returncode=code)
   send({'event':'reaped','returncode':code})
   assert peer.recv(1)==b'R'
  return code
try:
 transport.sendmsg([b'cap'],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[cap.fileno()]))])
 cap.close();transport.close()
 assert peer.recv(1)==b'A'
 if hold:subprocess.Popen=HeldGit
 code=worker.main();send({'event':'helper_end','returncode':code})
 raise SystemExit(code)
finally:
 cap.close();peer.close();transport.close()
"""


_GIT_BOOT_TEMPLATE = r"""
import os,socket,subprocess,threading
from pathlib import Path
import jeff.bootstrap.runtime as runtime
root=Path(os.environ['JEFF_TEST_D6_GATE'])
transport=socket.socket(fileno=int(os.environ['JEFF_TEST_D6_OBSERVER_FD']))
mode=os.environ['JEFF_TEST_D6_OBSERVER_MODE'];local=threading.local()
HELPER=__HELPER_CODE__
base_runner=runtime.OwnedGitRunner
class ObservedRunner(base_runner):
 def __call__(self,executable,directory,*args,**kwargs):
  local.observe=(root/'arm').exists() and args[0]=='status'
  try:return super().__call__(executable,directory,*args,**kwargs)
  finally:local.observe=False
runtime.OwnedGitRunner=ObservedRunner
original=subprocess.Popen
def spawn(command,**kwargs):
 if getattr(local,'observe',False):
  assert command[3:5]==['-m','jeff.integrations.git.worker']
  kwargs['pass_fds']=(*kwargs['pass_fds'],transport.fileno())
  command=[*command[:3],'-c',HELPER,str(transport.fileno()),str(root/'child-signals.jsonl'),mode,*command[5:]]
 return original(command,**kwargs)
subprocess.Popen=spawn
raise SystemExit(runtime.main())
"""


def _peer_event(peer: socket.socket) -> dict[str, Any]:
    def exact(size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            chunk = peer.recv(size - len(result))
            assert chunk
            result.extend(chunk)
        return bytes(result)

    size = struct.unpack("!I", exact(4))[0]
    assert 0 < size <= 2048
    result = json.loads(exact(size))
    assert isinstance(result, dict)
    return result


def _git_owner_window(
    tmp_path_factory: pytest.TempPathFactory, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path_factory.mktemp("g").resolve()
    gate = root / "gate"
    gate.mkdir(mode=0o700)
    (gate / "child-signals.jsonl").touch(mode=0o600)
    layout = RuntimeLayout(data_dir=root / "d", runtime_dir=root / "r", uid=os.geteuid())
    transport, inherited = socket.socketpair()
    transport.settimeout(5)
    preflight = os.environ.get("JEFF_TEST_D6_PREFLIGHT") == "1"
    original = subprocess.Popen
    script = _GIT_BOOT_TEMPLATE.replace("__HELPER_CODE__", repr(_HELPER_BOOT))

    def launch(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        assert command[3:5] == ["-m", "jeff.bootstrap.runtime"]
        kwargs["pass_fds"] = (*kwargs["pass_fds"], inherited.fileno())
        kwargs["env"] = {
            **kwargs["env"],
            "JEFF_TEST_D6_GATE": str(gate),
            "JEFF_TEST_D6_OBSERVER_FD": str(inherited.fileno()),
            "JEFF_TEST_D6_OBSERVER_MODE": "observe" if preflight else "hold",
        }
        return original([*command[:3], "-c", script, *command[5:]], **kwargs)

    control = RuntimeController(layout)
    client = RuntimeProjectClient(layout)
    call = ProjectCall(operation="ask", directory=git_repo, question="what branch am I on?")
    outcomes: list[DispatchReply | Exception] = []
    caller: threading.Thread | None = None
    peer: socket.socket | None = None

    def invoke() -> None:
        try:
            outcomes.append(client.execute(call))
        except Exception as error:
            outcomes.append(error)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "Popen", launch)
            started = control.start()
        inherited.close()
        assert isinstance(started, ControlSuccess) and started.data.identity is not None
        captured = control._child
        assert captured is not None and captured.poll() is None
        assert client.execute(ProjectCall(operation="init", directory=git_repo)).result.ok
        anchor = layout.lock.stat().st_ino
        credential = hashlib.sha256(layout.credential.read_bytes()).hexdigest()
        (gate / "arm").touch(mode=0o600)
        caller = threading.Thread(target=invoke)
        caller.start()
        data, ancillary, flags, _ = transport.recvmsg(
            3, socket.CMSG_SPACE(array.array("i").itemsize)
        )
        assert data == b"cap" and flags == 0 and len(ancillary) == 1
        assert ancillary[0][:2] == (socket.SOL_SOCKET, socket.SCM_RIGHTS)
        descriptors = array.array("i")
        descriptors.frombytes(ancillary[0][2])
        assert len(descriptors) == 1
        peer = socket.socket(fileno=descriptors[0])
        peer.set_inheritable(False)
        peer.settimeout(5)
        uid, pid = peer_identity(peer)
        assert uid == os.geteuid() and pid != captured.pid
        with closing(select.kqueue()) as observer:
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
            peer.sendall(b"A")
            if preflight:
                assert _peer_event(peer) == {"event": "helper_end", "returncode": 0}
                assert observer.control([], 1, 5)
                caller.join(10)
                assert not caller.is_alive() and len(outcomes) == 1
                assert isinstance(outcomes[0], DispatchReply) and outcomes[0].result.ok
                assert (gate / "child-signals.jsonl").read_bytes() == b""
                return  # Real daemon→helper capability preflight, no pause/kill.
            assert _peer_event(peer) == {"event": "held"}
            (root / "actor-observation.json").write_text(
                json.dumps(
                    {
                        "window": "git_in_flight",
                        "live_kernel_peer_uid": uid,
                        "observer_registered": True,
                        "actual_git_stopped": True,
                    }
                )
            )
            log = root / "crash-execution.json"
            fact = {
                "role": "daemon",
                "signal": "SIGKILL",
                "window": "git_in_flight",
                "execution": os.environ["JEFF_TEST_D6_EXECUTION"],
                "captured_pid": captured.pid,
            }
            with log.open("x") as out:
                json.dump(fact, out)
            captured.kill()
            assert captured.wait(timeout=5) == -9
            fact["native_exit_code"] = -9
            log.write_text(json.dumps(fact))
            assert _peer_event(peer) == {"event": "reaped", "returncode": -9}
            # The real child is reaped but helper reconciliation is still held.
            candidate = RuntimeStateStore(layout).acquire(create=False)
            if candidate is not None:
                with candidate:
                    pass
            assert candidate is None  # Same existing lease prevents replacement ownership.
            peer.sendall(b"R")
            assert _peer_event(peer) == {"event": "helper_end", "returncode": 0}
            exits = observer.control([], 1, 5)
            assert (
                len(exits) == 1 and exits[0].ident == pid and exits[0].fflags & select.KQ_NOTE_EXIT
            )
        caller.join(10)
        assert not caller.is_alive() and len(outcomes) == 1
        assert isinstance(outcomes[0], DispatchReply)
        assert not outcomes[0].result.ok and outcomes[0].accepted is None
        signals = [
            json.loads(line) for line in (gate / "child-signals.jsonl").read_text().splitlines()
        ]
        assert [row["event"] for row in signals] == ["SIGSTOP", "SIGKILL", "reaped"]
        assert (
            signals[-1]["returncode"] == -9
            and len({row["captured_child_pid"] for row in signals}) == 1
        )
        (gate / "arm").unlink()
        restarted = control.start()
        assert isinstance(restarted, ControlSuccess) and restarted.data.identity is not None
        assert restarted.data.identity.instance_id != started.data.identity.instance_id
        trace = client.inspect_evidence(
            EvidenceQuery(operation="trace", directory=git_repo, request_id=call.request_id)
        )
        assert trace.receipt is not None and trace.receipt.state == "unknown"
        assert trace.receipt.outcome_code == "interrupted"
        duplicate = client.execute(call)
        assert not duplicate.result.ok and duplicate.result.error.code == "duplicate_request"
        assert duplicate.receipt == trace.receipt
        assert layout.lock.stat().st_ino == anchor
        assert hashlib.sha256(layout.credential.read_bytes()).hexdigest() == credential
    finally:
        (gate / "arm").unlink(missing_ok=True)
        if peer is not None:
            try:
                peer.sendall(b"R")  # Release only the captured test gate, never signal a PID.
            except OSError:
                pass
            peer.close()
        inherited.close()
        transport.close()
        if caller is not None:
            caller.join(15)
            assert not caller.is_alive()
        assert isinstance(control.stop(), ControlSuccess)
