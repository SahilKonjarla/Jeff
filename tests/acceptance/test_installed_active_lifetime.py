"""Named6D native actor windows; genuine captured owners, never default/user state."""

from __future__ import annotations

import array
import hashlib
import json
import os
import select
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import pytest

from jeff.application.dispatch import (
    ActivityFault,
    DispatchReply,
    EvidenceQuery,
    ProjectCall,
    SubscriptionQuery,
)
from jeff.application.runtime import ControlFault, ControlSuccess, RuntimeLayout
from jeff.infrastructure.runtime_ipc import OwnedActivityStream, RuntimeProjectClient, peer_identity
from jeff.infrastructure.runtime_process import RuntimeController
from jeff.infrastructure.runtime_state import RuntimeStateStore

pytestmark = pytest.mark.usefixtures("owned_runtime_launches")

_QUEUED_BOOT = r"""
import json,os,threading,time
from pathlib import Path
import jeff.bootstrap.runtime as runtime
from jeff.application.dispatch import ProjectCall
root=Path(os.environ['JEFF_TEST_D6_GATE']);window=os.environ['JEFF_TEST_D6_WINDOW']
runners=[]
def mark(name,value):
 path=root/(name+'.'+str(threading.get_ident())+'.tmp')
 path.write_text(json.dumps(value));path.replace(root/name)
base_runner=runtime.OwnedGitRunner
class ObservedRunner(base_runner):
 def __init__(self,*args,**kwargs):super().__init__(*args,**kwargs);runners.append(self)
runtime.OwnedGitRunner=ObservedRunner
base_storage=runtime.SQLiteStorage
class HeldStorage(base_storage):
 def hold(self,request_id,state):
  if not (root/'arm').exists():return
  mark('claimed.'+str(request_id),{'request_id':str(request_id),'state':state})
  count=7 if window in ('queued','stop_drain','normal_stop')else 4 if window=='running'else 1
  while (root/'arm').exists():
   claims=sorted(root.glob('claimed.*'));admitted=list(root.glob('admitted.*'))
   claims=[p for p in claims if not p.name.endswith('.tmp')]
   admitted=[p for p in admitted if not p.name.endswith('.tmp')]
   expected=4 if window in ('queued','running','stop_drain','normal_stop')else 1
   if len(claims)==expected and len(admitted)==count and all(r.closed for r in runners):
    facts=[json.loads(p.read_text())for p in claims]
    mark('ready',{'claimed':[row['request_id']for row in facts],
                  'states':[row['state']for row in facts],
                  'admitted':count,'helpers_closed':True})
   time.sleep(.01)
 def claim(self,claim,**kwargs):
  result=super().claim(claim,**kwargs)
  if window in ('queued','stop_drain','normal_stop'):
   self.hold(claim.request_id,result.receipt.state)
  return result
 def finish(self,transition,**kwargs):
  result=super().finish(transition,**kwargs)
  if ((window=='running' and transition.state=='running') or
      (window=='terminal_duplicate' and transition.state=='succeeded')):
   self.hold(transition.request_id,result.state)
  return result
runtime.SQLiteStorage=HeldStorage
base_dispatch=runtime.RuntimeDispatcher
class ObservedDispatcher(base_dispatch):
 def begin_drain(self):
  super().begin_drain();mark('draining',{'admission_sealed':True})
 def submit(self,call,deadline):
  result=super().submit(call,deadline)
  if isinstance(call,ProjectCall) and (root/'arm').exists():
   mark('admitted.'+str(call.request_id),{'request_id':str(call.request_id)})
  return result
runtime.RuntimeDispatcher=ObservedDispatcher
raise SystemExit(runtime.main())
"""


@pytest.mark.parametrize(
    "window",
    [
        "queued",
        "running",
        "terminal_duplicate",
        "stop_drain",
        "healthy_stream",
        "backpressured_stream",
        "retirement_before_commit",
        "retirement_after_commit",
        "git_in_flight",
    ],
)
def test_active_owner_death(
    tmp_path_factory: pytest.TempPathFactory,
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    window: str,
) -> None:
    if os.environ.get("JEFF_TEST_D6_AUTHORITY") != "d6-cap40-exact":
        pytest.skip("Named6D ledger authority required; never an acceptance skip")
    if window in (
        "healthy_stream",
        "backpressured_stream",
        "retirement_before_commit",
        "retirement_after_commit",
    ):
        _healthy_stream_window(tmp_path_factory, git_repo, monkeypatch, window)
        return
    if window == "git_in_flight":
        _git_owner_window(tmp_path_factory, git_repo, monkeypatch)
        return
    assert window in ("queued", "running", "terminal_duplicate", "stop_drain")
    root = tmp_path_factory.mktemp("d").resolve()
    for name in ("home", "tmp", "cache", "config", "data", "gate"):
        (root / name).mkdir(mode=0o700)
    layout = RuntimeLayout(data_dir=root / "d", runtime_dir=root / "r", uid=os.geteuid())
    env = {
        "PATH": os.defpath,
        "HOME": str(root / "home"),
        "TMPDIR": str(root / "tmp"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_DATA_HOME": str(root / "data"),
        "JEFF_DATA_DIR": str(layout.data_dir),
        "JEFF_RUNTIME_DIR": str(layout.runtime_dir),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "LC_ALL": "C",
        "NO_COLOR": "1",
    }
    original = subprocess.Popen

    def launch(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        assert command[3:5] == ["-m", "jeff.bootstrap.runtime"]
        kwargs["env"] = {
            **kwargs["env"],
            "JEFF_TEST_D6_GATE": str(root / "gate"),
            "JEFF_TEST_D6_WINDOW": window,
        }
        return original([*command[:3], "-c", _QUEUED_BOOT, *command[5:]], **kwargs)

    control = RuntimeController(layout)
    client = RuntimeProjectClient(layout)
    calls = [
        ProjectCall(operation="ask", directory=git_repo, question="what branch am I on?")
        for _ in range(7 if window in ("queued", "stop_drain") else 4 if window == "running" else 1)
    ]
    outcomes: dict[str, DispatchReply | Exception] = {}
    callers: list[threading.Thread] = []
    stopper: threading.Thread | None = None
    stop_outcomes: list[object] = []

    def request_stop() -> None:
        try:
            stop_outcomes.append(control.stop())
        except Exception as error:
            stop_outcomes.append(error)

    def invoke(call: ProjectCall) -> None:
        try:
            outcomes[str(call.request_id)] = client.execute(call)
        except Exception as error:
            outcomes[str(call.request_id)] = error

    try:
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "Popen", launch)
            started = control.start()
        assert isinstance(started, ControlSuccess) and started.data.identity is not None
        captured = control._child
        assert captured is not None and captured.poll() is None
        assert client.execute(ProjectCall(operation="init", directory=git_repo)).result.ok
        credential = hashlib.sha256(layout.credential.read_bytes()).hexdigest()
        anchor = layout.lock.stat().st_ino
        (root / "gate/arm").touch(mode=0o600)
        for call in calls:
            thread = threading.Thread(target=invoke, args=(call,))
            callers.append(thread)
            thread.start()
        until = time.monotonic() + 10
        while not (root / "gate/ready").exists():
            assert captured.poll() is None and time.monotonic() < until
            time.sleep(0.01)
        barrier = json.loads((root / "gate/ready").read_text())
        claimed = set(barrier["claimed"])
        expected_count = 4 if window in ("queued", "running", "stop_drain") else 1
        state = (
            "accepted"
            if window in ("queued", "stop_drain")
            else "running"
            if window == "running"
            else "succeeded"
        )
        assert len(claimed) == expected_count
        assert barrier["admitted"] == len(calls) and barrier["helpers_closed"]
        assert barrier["states"] == [state] * expected_count
        if window in ("queued", "stop_drain"):
            assert claimed < {str(call.request_id) for call in calls}
        else:
            assert claimed == {str(call.request_id) for call in calls}
        concurrent_duplicate: DispatchReply | None = None
        if window == "terminal_duplicate":
            assert all(thread.is_alive() for thread in callers)
            concurrent_duplicate = client.execute(calls[0])
            assert not concurrent_duplicate.result.ok
            assert concurrent_duplicate.result.error.code == "duplicate_request"
            assert concurrent_duplicate.receipt is not None
            assert concurrent_duplicate.receipt.state == "succeeded"
            barrier["duplicate_received_terminal_fact"] = True
        if window == "stop_drain":
            stopper = threading.Thread(target=request_stop)
            stopper.start()
            until = time.monotonic() + 5
            while not (root / "gate/draining").exists():
                assert captured.poll() is None and time.monotonic() < until
                time.sleep(0.01)
            assert json.loads((root / "gate/draining").read_text()) == {"admission_sealed": True}
            barrier["actual_drain_started"] = True
        (root / "actor-observation.json").write_text(json.dumps({"window": window, **barrier}))
        if os.environ.get("JEFF_TEST_D6_PREFLIGHT") == "1":
            (root / "gate/arm").unlink()
            for thread in callers:
                thread.join(15)
                assert not thread.is_alive()
            assert len(outcomes) == len(calls)
            if window == "stop_drain":
                assert stopper is not None
                stopper.join(15)
                assert not stopper.is_alive() and len(stop_outcomes) == 1
                assert (
                    isinstance(stop_outcomes[0], ControlSuccess) and captured.wait(timeout=5) == 0
                )
                assert (
                    sum(
                        isinstance(reply, DispatchReply) and reply.result.ok
                        for reply in outcomes.values()
                    )
                    == 4
                )
            else:
                assert all(
                    isinstance(reply, DispatchReply) and reply.result.ok
                    for reply in outcomes.values()
                )
            return  # Explicit barrier-only preflight, excluded from acceptance accounting.
        log = root / "crash-execution.json"
        with log.open("x") as out:
            json.dump(
                {
                    "role": "daemon",
                    "signal": "SIGKILL",
                    "window": window,
                    "execution": os.environ["JEFF_TEST_D6_EXECUTION"],
                    "captured_pid": captured.pid,
                },
                out,
            )
        captured.kill()
        assert captured.wait(timeout=5) == -9
        crash_fact = json.loads(log.read_text())
        crash_fact["native_exit_code"] = -9
        log.write_text(json.dumps(crash_fact))
        if stopper is not None:
            stopper.join(10)
            assert not stopper.is_alive() and len(stop_outcomes) == 1
            assert not isinstance(stop_outcomes[0], Exception)
        for thread in callers:
            thread.join(10)
            assert not thread.is_alive()
        assert len(outcomes) == len(calls)
        for reply in outcomes.values():
            assert isinstance(reply, DispatchReply)
            assert not reply.result.ok and reply.result.error.code == "request_outcome_unknown"
            assert reply.accepted is None and reply.identity == started.data.identity
        before = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in layout.data_dir.iterdir()
            if p.is_file()
        }
        profile = root / "doctor.sb"
        profile.write_text(
            "(version 1)\n(allow default)\n(deny file-write*)\n"
            '(deny network*)\n(allow file-write* (literal "/dev/null"))\n'
        )
        doctor = subprocess.run(
            [
                "/usr/bin/sandbox-exec",
                "-f",
                str(profile),
                sys.executable,
                "-I",
                "-B",
                "-m",
                "jeff",
                "doctor",
                "--json",
            ],
            cwd=root,
            env=env,
            capture_output=True,
            timeout=10,
        )
        assert (
            doctor.returncode == 1 and json.loads(doctor.stdout)["data"]["summary"] == "unavailable"
        )
        assert {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in layout.data_dir.iterdir()
            if p.is_file()
        } == before
        (root / "gate/arm").unlink()
        restarted = control.start()
        assert isinstance(restarted, ControlSuccess) and restarted.data.identity is not None
        assert restarted.data.identity.instance_id != started.data.identity.instance_id
        for call in calls:
            trace = client.inspect_evidence(
                EvidenceQuery(operation="trace", directory=git_repo, request_id=call.request_id)
            )
            assert trace.error is None
            if str(call.request_id) in claimed:
                assert trace.receipt is not None and trace.trace is not None
                expected_state = "succeeded" if window == "terminal_duplicate" else "unknown"
                assert trace.receipt.state == expected_state
                assert trace.receipt.outcome_code == (
                    "ok" if expected_state == "succeeded" else "interrupted"
                )
                if concurrent_duplicate is not None:
                    assert concurrent_duplicate.receipt == trace.receipt
                duplicate = client.execute(call)
                assert (
                    not duplicate.result.ok and duplicate.result.error.code == "duplicate_request"
                )
                assert duplicate.receipt == trace.receipt
            else:
                assert trace.receipt is None and trace.trace is None
                assert trace.missing_is_not_nonexecution_proof  # Never resend these IDs.
        assert hashlib.sha256(layout.credential.read_bytes()).hexdigest() == credential
        assert layout.lock.stat().st_ino == anchor
        assert client.execute(
            ProjectCall(operation="ask", directory=git_repo, question="what branch am I on?")
        ).result.ok
        if window == "stop_drain":
            assert isinstance(control.stop(), ControlSuccess)
            _normal_stop_under_load(layout, git_repo, root / "normal", monkeypatch)
    finally:
        (root / "gate/arm").unlink(missing_ok=True)
        for thread in callers:
            thread.join(15)
            assert not thread.is_alive()
        if stopper is not None:
            stopper.join(15)
            assert not stopper.is_alive()
        assert isinstance(control.stop(), ControlSuccess)


def _normal_stop_under_load(
    layout: RuntimeLayout, git_repo: Path, gate: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Distinct graceful lifecycle: same production owner/dispatch, no fault signal."""
    gate.mkdir(mode=0o700)
    original = subprocess.Popen

    def launch(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        assert command[3:5] == ["-m", "jeff.bootstrap.runtime"]
        kwargs["env"] = {
            **kwargs["env"],
            "JEFF_TEST_D6_GATE": str(gate),
            "JEFF_TEST_D6_WINDOW": "normal_stop",
        }
        return original([*command[:3], "-c", _QUEUED_BOOT, *command[5:]], **kwargs)

    control, client = RuntimeController(layout), RuntimeProjectClient(layout)
    calls = [
        ProjectCall(operation="ask", directory=git_repo, question="what branch am I on?")
        for _ in range(7)
    ]
    outcomes: dict[str, DispatchReply | Exception] = {}
    callers: list[threading.Thread] = []
    stops: list[object] = []
    stopper: threading.Thread | None = None

    def invoke(call: ProjectCall) -> None:
        try:
            outcomes[str(call.request_id)] = client.execute(call)
        except Exception as error:
            outcomes[str(call.request_id)] = error

    def request_stop() -> None:
        try:
            stops.append(control.stop())
        except Exception as error:
            stops.append(error)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "Popen", launch)
            started = control.start()
        assert isinstance(started, ControlSuccess)
        captured = control._child
        assert captured is not None
        (gate / "arm").touch(mode=0o600)
        for call in calls:
            thread = threading.Thread(target=invoke, args=(call,))
            callers.append(thread)
            thread.start()
        until = time.monotonic() + 10
        while not (gate / "ready").exists():
            assert time.monotonic() < until and captured.poll() is None
            time.sleep(0.01)
        claimed = set(json.loads((gate / "ready").read_text())["claimed"])
        assert len(claimed) == 4
        stopper = threading.Thread(target=request_stop)
        stopper.start()
        until = time.monotonic() + 5
        while not (gate / "draining").exists():
            assert time.monotonic() < until and captured.poll() is None
            time.sleep(0.01)
        (gate / "arm").unlink()
        for thread in callers:
            thread.join(15)
            assert not thread.is_alive()
        stopper.join(15)
        assert not stopper.is_alive() and len(stops) == 1 and isinstance(stops[0], ControlSuccess)
        assert captured.wait(timeout=5) == 0
        assert len(outcomes) == 7
        for request_id, reply in outcomes.items():
            assert isinstance(reply, DispatchReply)
            if request_id in claimed:
                assert reply.result.ok
            else:
                assert not reply.result.ok and reply.accepted is not True
        assert isinstance(control.start(), ControlSuccess)
        for call in calls:
            trace = client.inspect_evidence(
                EvidenceQuery(operation="trace", directory=git_repo, request_id=call.request_id)
            )
            assert trace.error is None
            if str(call.request_id) in claimed:
                assert trace.receipt is not None and trace.receipt.state == "succeeded"
            else:
                assert trace.receipt is None and trace.missing_is_not_nonexecution_proof
    finally:
        (gate / "arm").unlink(missing_ok=True)
        for thread in callers:
            thread.join(15)
            assert not thread.is_alive()
        if stopper is not None:
            stopper.join(15)
            assert not stopper.is_alive()
        assert isinstance(control.stop(), ControlSuccess)


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


_STREAM_BOOT = r"""
import json,os,threading,time
from pathlib import Path
import jeff.bootstrap.runtime as runtime
root=Path(os.environ['JEFF_TEST_D6_GATE']);mutex=threading.Lock();active=0
runners=[];stop=threading.Event()
base=runtime.OwnedGitRunner
class ObservedRunner(base):
 def __init__(self,*args,**kwargs):super().__init__(*args,**kwargs);runners.append(self)
 def __call__(self,*args,**kwargs):
  global active
  until=time.monotonic()+10
  while True:
   with mutex:
    if not (root/'seal').exists():active+=1;break
   assert time.monotonic()<until,'Owned fixture Git-admission gate expired'
   time.sleep(.01)
  try:return super().__call__(*args,**kwargs)
  finally:
   with mutex:active-=1
runtime.OwnedGitRunner=ObservedRunner
def observe():
 while not stop.wait(.01):
  with mutex:
   safe=(root/'seal').exists()and active==0 and all(r.closed for r in runners)
  if safe and not (root/'quiescent').exists():
   (root/'quiescent.tmp').write_text(json.dumps({'active_git_calls':0,'helpers_closed':True}))
   (root/'quiescent.tmp').replace(root/'quiescent')
watcher=threading.Thread(target=observe,name='owned-d6-observer');watcher.start()
try:code=runtime.main()
finally:stop.set();watcher.join(1);assert not watcher.is_alive()
raise SystemExit(code)
"""


_BACKPRESSURE_HOOK = r"""
import asyncio,json,os,socket
from pathlib import Path
import jeff.infrastructure.runtime_ipc as ipc
root=Path(os.environ['JEFF_TEST_D6_GATE']);slow=os.environ['JEFF_TEST_D6_SLOW_ID']
original_send=ipc._stream_send
async def send(writer,frame):
 if str(frame.request_id)!=slow:return await original_send(writer,frame)
 writer.get_extra_info('socket').setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,1024)
 actual=asyncio.create_task(original_send(writer,frame))
 await asyncio.sleep(0)
 waiting=writer.transport.get_write_buffer_size()>65536 and not actual.done()
 if waiting:(root/'blocked').write_text(json.dumps({'real_write_buffer_above_64k':True}))
 try:await actual
 finally:
  if waiting:(root/'unwound').write_text('actual drain finished')
ipc._stream_send=send
"""


_RETIREMENT_HOOK = r"""
import asyncio,json,os,time
from pathlib import Path
import jeff.infrastructure.runtime_storage as sql
import jeff.infrastructure.runtime_ipc as ipc
root=Path(os.environ['JEFF_TEST_D6_GATE']);window=os.environ['JEFF_TEST_D6_WINDOW']
slow=os.environ['JEFF_TEST_D6_SLOW_ID'];base_store=sql.SQLiteStorage
class ObservedStore(base_store):
 def __init__(self,*args,**kwargs):
  kwargs['retention_clock']=lambda:(time.time_ns()//1000000+
   (32*86400000 if (root/'expire').exists()else 0))
  kwargs['retention_monotonic']=lambda:160.0 if (root/'expire').exists()else 100.0
  super().__init__(*args,**kwargs)
 def _open(self,*args):
  connection,fd=super()._open(*args);connection.execute('PRAGMA cache_size=1')
  return connection,fd
 def hold(self,connection):
  journal=self.owner.store.layout.data_dir/'state.sqlite3-journal'
  header=journal.read_bytes()[:8].hex()if journal.exists()else ''
  facts={'in_transaction':connection.in_transaction,'journal_magic':header,
         'permit_present':self.owner.store.layout.recovery_permit.exists()}
  (root/'retire-ready.tmp').write_text(json.dumps(facts))
  (root/'retire-ready.tmp').replace(root/'retire-ready')
  until=time.monotonic()+8
  while not(root/'release').exists():
   assert time.monotonic()<until,'Owned retirement gate expired'
   time.sleep(.01)
 def _commit_write(self,connection,fd):
  if getattr(self,'retiring',False)and window=='retirement_before_commit':self.hold(connection)
  return super()._commit_write(connection,fd)
 def _retire(self,connection,directory,fd):
  self.retiring=(root/'expire').exists()and not getattr(self,'fired',False)
  result=super()._retire(connection,directory,fd)
  if self.retiring:
   assert result.retired_count>0
   if window=='retirement_after_commit':self.hold(connection)
   self.fired=True
  self.retiring=False
  return result
sql.SQLiteStorage=ObservedStore
original_send=ipc._stream_send
async def send(writer,frame):
 await original_send(writer,frame)
 if str(frame.request_id)==slow and frame.kind=='event'and(root/'buffer').exists():
  (root/'buffered').write_text('real event prefix sent; immutable page still held')
  until=time.monotonic()+8
  while not(root/'release').exists():
   assert time.monotonic()<until,'Owned buffered-page gate expired'
   await asyncio.sleep(.01)
ipc._stream_send=send
"""


@contextmanager
def _live_peer_reader(stream: OwnedActivityStream) -> Iterator[tuple[set[str], threading.Event]]:
    seen: set[str] = set()
    closed, stop = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def read() -> None:
        try:
            while not stop.is_set():
                try:
                    frame = stream.receive(wait_seconds=0.1)
                except ControlFault as error:
                    assert error.code == "activity_stream_closed"
                    closed.set()
                    return
                if frame is not None and frame.kind == "closed":
                    closed.set()
                    return
                if (
                    frame is not None
                    and frame.event is not None
                    and frame.event.kind == "request.succeeded"
                ):
                    seen.add(str(frame.event.request_id))
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=read)
    thread.start()
    try:
        yield seen, closed
    finally:
        stop.set()
        stream.close()
        thread.join(5)
        assert not thread.is_alive() and not errors


def _healthy_stream_window(
    tmp_path_factory: pytest.TempPathFactory,
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    window: str,
) -> None:
    from contextlib import ExitStack

    root = tmp_path_factory.mktemp("s").resolve()
    gate = root / "gate"
    gate.mkdir(mode=0o700)
    layout = RuntimeLayout(data_dir=root / "d", runtime_dir=root / "r", uid=os.geteuid())
    original = subprocess.Popen
    slow_query = SubscriptionQuery(directory=git_repo)
    backpressured = window == "backpressured_stream"
    retiring = window.startswith("retirement_")
    hook = _BACKPRESSURE_HOOK if backpressured else _RETIREMENT_HOOK if retiring else ""
    script = hook + "\n" + _STREAM_BOOT

    def launch(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        assert command[3:5] == ["-m", "jeff.bootstrap.runtime"]
        kwargs["env"] = {
            **kwargs["env"],
            "JEFF_TEST_D6_GATE": str(gate),
            "JEFF_TEST_D6_SLOW_ID": str(slow_query.query_id),
            "JEFF_TEST_D6_WINDOW": window,
        }
        return original([*command[:3], "-c", script, *command[5:]], **kwargs)

    control, client = RuntimeController(layout), RuntimeProjectClient(layout)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "Popen", launch)
            started = control.start()
        assert isinstance(started, ControlSuccess) and started.data.identity is not None
        captured = control._child
        assert captured is not None
        assert client.execute(ProjectCall(operation="init", directory=git_repo)).result.ok
        with (
            client.subscribe(slow_query) as stream,
            client.subscribe(SubscriptionQuery(directory=git_repo)) as second,
            _live_peer_reader(second) as (seen, healthy_closed),
        ):
            if backpressured:
                stream.connection.connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
            if retiring:
                (gate / "buffer").touch(mode=0o600)
            for _ in range(40 if backpressured else 1):
                call = ProjectCall(
                    operation="ask", directory=git_repo, question="what branch am I on?"
                )
                completed = client.execute(call)
                assert completed.result.ok and completed.receipt is not None
                if not backpressured or (gate / "blocked").exists():
                    break
            if backpressured:
                assert (gate / "blocked").exists(), "Actual kernel-backed drain must be pending"
                assert not (gate / "unwound").exists()
            elif retiring:
                until = time.monotonic() + 5
                while not (gate / "buffered").exists():
                    assert time.monotonic() < until
                    time.sleep(0.01)
                frame = stream.receive(wait_seconds=1)
                assert frame is not None and frame.event is not None
            else:
                until = time.monotonic() + 5
                while True:
                    assert time.monotonic() < until
                    frame = stream.receive(wait_seconds=0.5)
                    if frame is not None and frame.event is not None:
                        if (
                            frame.event.request_id == call.request_id
                            and frame.event.kind == "request.succeeded"
                        ):
                            break
            until = time.monotonic() + 3
            while str(call.request_id) not in seen:
                assert time.monotonic() < until and not healthy_closed.is_set()
                time.sleep(0.01)
            cursor = stream.last_received_cursor
            assert isinstance(control.status(), ControlSuccess)
            retirement_facts: dict[str, Any] = {}
            if retiring:
                (gate / "expire").touch(mode=0o600)
                until = time.monotonic() + 5
                while not (gate / "retire-ready").exists():
                    assert captured.poll() is None and time.monotonic() < until
                    time.sleep(0.01)
                retirement_facts = json.loads((gate / "retire-ready").read_text())
                committed = window == "retirement_after_commit"
                assert retirement_facts == {
                    "in_transaction": not committed,
                    "journal_magic": "" if committed else "d9d505f920a163d7",
                    "permit_present": not committed,
                }
            (gate / "seal").touch(mode=0o600)
            until = time.monotonic() + 5
            while not (gate / "quiescent").exists():
                assert captured.poll() is None and time.monotonic() < until
                time.sleep(0.01)
            facts = json.loads((gate / "quiescent").read_text())
            assert facts == {"active_git_calls": 0, "helpers_closed": True}
            (root / "actor-observation.json").write_text(
                json.dumps(
                    {
                        "window": window,
                        "caught_up_terminal": not (backpressured or retiring),
                        "healthy_second_peer": True,
                        "healthy_closed_before_death": healthy_closed.is_set(),
                        "actual_backpressure": backpressured,
                        "buffered_page": retiring,
                        "retirement": retirement_facts,
                        **facts,
                    }
                )
            )
            if os.environ.get("JEFF_TEST_D6_PREFLIGHT") == "1":
                (gate / "release").touch(mode=0o600)
                (gate / "seal").unlink()
                return
            fact = {
                "role": "daemon",
                "signal": "SIGKILL",
                "window": window,
                "execution": os.environ["JEFF_TEST_D6_EXECUTION"],
                "captured_pid": captured.pid,
            }
            log = root / "crash-execution.json"
            with log.open("x") as out:
                json.dump(fact, out)
            if backpressured:
                assert not (gate / "unwound").exists()
            captured.kill()
            assert captured.wait(timeout=5) == -9
            fact["native_exit_code"] = -9
            log.write_text(json.dumps(fact))
            assert healthy_closed.wait(3) and second.connection.connection.fileno() == -1
            # Bytes already sent may remain; drain them honestly before bounded closure.
            for owned in (stream,):
                until = time.monotonic() + 3
                for _ in range(128):
                    assert time.monotonic() < until
                    try:
                        frame = owned.receive(wait_seconds=0.1)
                    except ControlFault as error:
                        assert error.code == "activity_stream_closed"
                        break
                    if frame is not None and frame.kind == "closed":
                        break
                else:
                    pytest.fail("Captured reader did not close under original bound")
                assert owned.connection.connection.fileno() == -1
        (gate / "seal").unlink()
        restarted = control.start()
        assert isinstance(restarted, ControlSuccess) and restarted.data.identity is not None
        assert restarted.data.identity.instance_id != started.data.identity.instance_id
        trace = client.inspect_evidence(
            EvidenceQuery(operation="trace", directory=git_repo, request_id=call.request_id)
        )
        retired = window == "retirement_after_commit"
        if retired:
            assert trace.error is None and trace.receipt is None and trace.trace is None
            assert trace.missing_is_not_nonexecution_proof
        else:
            assert trace.receipt == completed.receipt
        duplicate = client.execute(call)
        assert not duplicate.result.ok
        assert duplicate.result.error.code == (
            "request_retired" if retired else "duplicate_request"
        )
        with ExitStack() as streams:
            if retired:
                with pytest.raises(ActivityFault) as invalid:
                    client.subscribe(SubscriptionQuery(directory=git_repo, cursor=cursor))
                assert invalid.value.code == "activity_invalid_cursor"
                resumed = streams.enter_context(
                    client.subscribe(SubscriptionQuery(directory=git_repo))
                )
            else:
                resumed = streams.enter_context(
                    client.subscribe(SubscriptionQuery(directory=git_repo, cursor=cursor))
                )
                assert resumed.ready.cursor == cursor
            for _ in range(7):
                fresh = streams.enter_context(
                    client.subscribe(SubscriptionQuery(directory=git_repo))
                )
                assert fresh.ready.metadata is not None
            assert isinstance(control.status(), ControlSuccess)
    finally:
        (gate / "release").touch(mode=0o600)
        (gate / "seal").unlink(missing_ok=True)
        assert isinstance(control.stop(), ControlSuccess)
